"""VapourSynth environment (WP-B): autoload disabled, explicit plugin loading, ABI checks.

Rules (verified in the sandbox, see plans/architecture.md §1.2/§1.4):
  * ``vapoursynth`` is imported lazily and ``vs.core`` is never touched before
    ``open_core()`` registered an EnvironmentPolicy whose environment was created
    with ``vs.DISABLE_AUTO_LOADING`` (core.flags == 2, plugins == std/resize/text).
  * Repo plugins (bestsource) are loaded from paths.system_vs_plugin_dir();
    bundled ones from paths.plugin_dir() in the order of build-info.json.
  * ``LoadPlugin`` errors are raised (not swallowed like autoload) and turned
    into ComponentError(PLUGIN_LOAD_FAILED / PLUGIN_CONFLICT).
  * ``destroy_environment`` is called on close() to avoid the R80 RuntimeWarning.
  * Only one VSCore per process; the GUI never creates one (it uses the CLI).
    After ``close()`` a fresh environment (and core) can be opened again, which
    the tests rely on.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import buildinfo, osdep, paths
from .errors import ComponentError, ErrorCode
from .models import BuildInfo, PluginInfo

REQUIRED_NAMESPACES: tuple[str, ...] = ("bs", "akarin", "resize2", "mvu", "znedi3", "zsmooth")
OPTIONAL_NAMESPACES: tuple[str, ...] = ("dfttest2_cpu", "vivtc", "vszip", "ffms2")

DEFAULT_CACHE_MB_SD = 2048
DEFAULT_CACHE_MB_HD = 6144

def repo_plugins() -> tuple[PluginInfo, ...]:
    """Plugins from the system VapourSynth installation (file names are the host's)."""
    return (PluginInfo("bestsource", "bs", "com.vapoursynth.bestsource", "R22", osdep.shared_lib_name("bestsource"), True, "repo"),)


def __getattr__(name: str) -> object:  # REPO_PLUGINS, computed per call (HOST may be swapped)
    if name == "REPO_PLUGINS":
        return repo_plugins()
    raise AttributeError(name)

PYTHON_PACKAGES: tuple[str, ...] = ("vsjetpack", "jetpytools")
OPTIONAL_PYTHON_PACKAGES: tuple[str, ...] = ("dfttest2",)

_ALREADY_LOADED = re.compile(r"already loaded \((?P<id>[^)]+)\) from (?P<path>.+)$")


def _make_policy(vs: Any) -> Any:
    """EnvironmentPolicy whose environments are created with DISABLE_AUTO_LOADING.

    The base class lives in the vapoursynth module, so the class is built lazily;
    one instance per process, environments come and go (open_core / close).
    """

    class QtgmcPolicy(vs.EnvironmentPolicy):
        api: Any = None
        current: Any = None

        def on_policy_registered(self, api: Any) -> None:
            self.api = api

        def on_policy_cleared(self) -> None:
            self.current = None

        def get_current_environment(self) -> Any:
            return self.current

        def set_environment(self, environment: Any) -> Any:
            old = self.current
            self.current = environment
            return old

    return QtgmcPolicy()


_lock = threading.RLock()
_policy_instance: Any = None
_open: VSCore | None = None


def import_vs() -> Any:
    try:
        return importlib.import_module("vapoursynth")
    except Exception as e:  # ImportError or a loader failure inside the extension
        raise ComponentError(
            ErrorCode.VS_IMPORT_FAILED,
            f"the vapoursynth Python module cannot be imported: {e}",
            f"install {osdep.package_ref('vapoursynth')} (the venv must see system site-packages)",
            {"python": sys.executable},
        ) from e


def _policy() -> Any:
    global _policy_instance
    if _policy_instance is None:
        vs = import_vs()
        if vs.has_policy():
            raise ComponentError(
                ErrorCode.VS_IMPORT_FAILED,
                "a VapourSynth environment policy is already registered in this process",
                "vs.core was used before vsenv.open_core(); autoload can no longer be disabled",
            )
        _policy_instance = _make_policy(vs)
        vs.register_policy(_policy_instance)
    return _policy_instance


@dataclass
class VSCore:
    """Handle around the single VapourSynth core of this process."""

    core: Any  # vapoursynth.Core
    loaded: dict[str, str] = field(default_factory=dict)  # namespace -> plugin path
    build_info: BuildInfo | None = None
    environment: Any = None
    skipped: dict[str, str] = field(default_factory=dict)  # optional plugin name -> why not loaded

    def has(self, namespace: str) -> bool:
        return namespace in self.loaded

    def close(self) -> None:
        global _open
        with _lock:
            if self.environment is not None:
                pol = _policy()
                if pol.current is self.environment:
                    pol.current = None
                pol.api.destroy_environment(self.environment)
                self.environment = None
            self.core = None
            if _open is self:
                _open = None


def current() -> VSCore | None:
    """The VSCore opened by this process, if any."""
    return _open


def open_core(
    *,
    threads: int = 0,
    cache_mb: int = 0,
    plugin_dir: Path | None = None,
    build_info: BuildInfo | None = None,
    load_optional: bool = True,
    check_abi: bool = True,
) -> VSCore:
    """Register the no-autoload policy, create the core, run ``check_abi``, load plugins.

    Raises ComponentError with a code from errors.ErrorCode (never a bare traceback).
    """
    global _open
    with _lock:
        if _open is not None:
            raise ComponentError(
                ErrorCode.INTERNAL,
                "a VapourSynth core is already open in this process",
                "call VSCore.close() first",
            )
        vs = import_vs()
        if build_info is None:
            build_info = buildinfo.load_or_dev()
        if check_abi:
            problems = _check_abi(build_info)
            if problems:
                code = ErrorCode.PYTHON_VERSION_MISMATCH if "python" in problems[0] else ErrorCode.VS_VERSION_MISMATCH
                raise ComponentError(code, "; ".join(problems), "reinstall/rebuild qtgmc-6ui", {"problems": problems})
        pol = _policy()
        env = pol.api.create_environment(vs.DISABLE_AUTO_LOADING)
        pol.current = env
        core = vs.core.core
        handle = VSCore(core=core, build_info=build_info, environment=env)
        try:
            if not core.flags & vs.DISABLE_AUTO_LOADING:
                raise ComponentError(ErrorCode.INTERNAL, "VapourSynth core was created with autoload enabled")
            if threads > 0:
                core.num_threads = threads
            if cache_mb > 0:
                core.max_cache_size = cache_mb
            _load_all(handle, plugin_dir or paths.plugin_dir(), load_optional)
        except BaseException:
            handle.close()
            raise
        _open = handle
        return handle


def _load_all(handle: VSCore, plugin_dir: Path, load_optional: bool) -> None:
    bi = handle.build_info
    plugins = list(bi.plugins) if bi and bi.plugins else []
    if not any(p.origin == "repo" for p in plugins):
        plugins = list(repo_plugins()) + plugins
    bundled = [p for p in plugins if p.origin != "repo"]
    if bundled and not plugin_dir.is_dir():
        raise ComponentError(
            ErrorCode.PLUGIN_DIR_MISSING,
            f"bundled plugin directory not found: {plugin_dir}",
            "reinstall qtgmc-6ui, or set QTGMC6UI_PLUGIN_DIR for a development checkout",
            {"plugin_dir": str(plugin_dir)},
        )
    sysdir = paths.system_vs_plugin_dir()
    for p in plugins:
        if not p.required and not load_optional:
            handle.skipped[p.name] = "optional plugins not requested"
            continue
        if p.origin == "repo":
            path = Path(p.file) if Path(p.file).is_absolute() else (sysdir / p.file if sysdir else Path(p.file))
            hint = f"install {osdep.package_ref('vapoursynth-plugin-' + p.name)}"
        else:
            path = Path(p.file) if Path(p.file).is_absolute() else plugin_dir / p.file
            hint = "reinstall/rebuild qtgmc-6ui (the bundled plugin file is missing or broken)"
        if not path.is_file():
            if p.required:
                raise ComponentError(
                    ErrorCode.PLUGIN_LOAD_FAILED,
                    f"required plugin '{p.name}' not found at {path}",
                    hint,
                    {"plugin": p.name, "path": str(path)},
                )
            handle.skipped[p.name] = f"file not found: {path}"
            continue
        try:
            ns = load_plugin(handle.core, path, p)
        except ComponentError as e:
            if p.required:
                if not e.hint:
                    e.hint = hint
                raise
            handle.skipped[p.name] = e.message
            continue
        handle.loaded[ns] = str(path)


def check_abi(build_info: BuildInfo) -> list[str]:
    """Compare running python minor / VS core / API versions with build-info; return problems
    as user-facing lines ("Arch updated python to 3.15; reinstall/rebuild qtgmc-6ui")."""
    problems: list[str] = []
    run_py = list(sys.version_info[:2])
    if build_info.python_minor and run_py != list(build_info.python_minor):
        built = ".".join(map(str, build_info.python_minor))
        problems.append(
            f"Arch updated python to {run_py[0]}.{run_py[1]} (qtgmc-6ui was built for {built}); reinstall/rebuild qtgmc-6ui"
        )
    vs = import_vs()
    core_major = int(vs.__version__.release_major)
    if build_info.vapoursynth_core and core_major != build_info.vapoursynth_core:
        problems.append(
            f"Arch updated vapoursynth to R{core_major} (qtgmc-6ui was built for R{build_info.vapoursynth_core}); reinstall/rebuild qtgmc-6ui"
        )
    api = [int(vs.__api_version__.api_major), int(vs.__api_version__.api_minor)]
    if build_info.vapoursynth_api and api[0] != build_info.vapoursynth_api[0]:
        built = ".".join(map(str, build_info.vapoursynth_api))
        problems.append(
            f"VapourSynth API changed to R{api[0]}.{api[1]} (plugins were built for R{built}); reinstall/rebuild qtgmc-6ui"
        )
    return problems


_check_abi = check_abi  # open_core() has a parameter of the same name


def check_python_packages(build_info: BuildInfo) -> list[str]:
    """Installed vsjetpack/jetpytools versions vs build-info (doctor; not a startup blocker)."""
    problems: list[str] = []
    for name, want in (build_info.python_packages or {}).items():
        try:
            have = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            if name in PYTHON_PACKAGES:
                problems.append(f"python package {name} {want} is not installed; reinstall qtgmc-6ui")
            continue
        if want and have != want:
            problems.append(f"python package {name} is {have}, qtgmc-6ui was built with {want}; reinstall qtgmc-6ui")
    return problems


def load_plugin(core: Any, path: Path, expect: PluginInfo | None = None) -> str:
    """LoadPlugin one file; return its namespace.  Already-loaded same identifier -> PLUGIN_CONFLICT
    unless it comes from the same path (then no-op)."""
    vs = import_vs()
    spath = str(path)
    try:
        core.std.LoadPlugin(path=spath)
    except vs.Error as e:
        msg = str(e)
        m = _ALREADY_LOADED.search(msg)
        if m:
            other = m.group("path").strip()
            if Path(other).resolve() == Path(spath).resolve():
                pass  # same file twice: no-op
            else:
                raise ComponentError(
                    ErrorCode.PLUGIN_CONFLICT,
                    f"plugin {m.group('id')} from {spath} is already loaded from {other}",
                    "another copy of this plugin is loaded (autoload is disabled, so this is a bundling error)",
                    {"identifier": m.group("id"), "path": spath, "loaded_from": other},
                ) from e
        else:
            raise ComponentError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"cannot load plugin {spath}: {msg}",
                "",
                {"path": spath, "error": msg},
            ) from e
    rp = Path(spath).resolve()
    found = None
    for pl in core.plugins():
        if pl.plugin_path and Path(pl.plugin_path).resolve() == rp:
            found = pl
            break
    if found is None:
        raise ComponentError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            f"plugin {spath} loaded but did not register (no namespace found for this path)",
            "",
            {"path": spath},
        )
    if expect is not None:
        if expect.namespace and found.namespace != expect.namespace:
            raise ComponentError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"plugin '{expect.name}' at {spath} registered namespace '{found.namespace}', expected '{expect.namespace}'",
                "the plugin file does not match build-info.json; reinstall/rebuild qtgmc-6ui",
                {"path": spath, "namespace": found.namespace, "expected": expect.namespace},
            )
        if expect.identifier and found.identifier != expect.identifier:
            raise ComponentError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                f"plugin '{expect.name}' at {spath} has identifier '{found.identifier}', expected '{expect.identifier}'",
                "the plugin file does not match build-info.json; reinstall/rebuild qtgmc-6ui",
                {"path": spath, "identifier": found.identifier, "expected": expect.identifier},
            )
    return str(found.namespace)


def plugin_table(core: Any) -> list[dict[str, str]]:
    """[{namespace, identifier, name, path, version}] of everything loaded (for doctor)."""
    rows = []
    for pl in core.plugins():
        ver = pl.version
        rows.append(
            {
                "namespace": str(pl.namespace),
                "identifier": str(pl.identifier),
                "name": str(pl.name),
                "path": str(pl.plugin_path or ""),
                "version": f"{ver.major}.{ver.minor}" if ver is not None else "",
            }
        )
    return sorted(rows, key=lambda r: r["namespace"])


def runtime_versions() -> dict[str, str]:
    """{'python': '3.14.7', 'vapoursynth': 'R80', 'api': 'R4.3', 'vsjetpack': '2.2.6', 'jetpytools': ...}
    without creating a core."""
    out: dict[str, str] = {"python": ".".join(map(str, sys.version_info[:3])), "python_executable": sys.executable}
    try:
        vs = import_vs()
        out["vapoursynth"] = f"R{vs.__version__.release_major}"
        out["api"] = f"R{vs.__api_version__.api_major}.{vs.__api_version__.api_minor}"
        out["vapoursynth_module"] = str(Path(vs.__file__))
    except ComponentError as e:
        out["vapoursynth"] = f"missing ({e.message})"
    for name in PYTHON_PACKAGES + OPTIONAL_PYTHON_PACKAGES + ("numpy",):
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = "missing"
    return out


def default_cache_mb(width: int, height: int) -> int:
    return DEFAULT_CACHE_MB_HD if height > 625 or width > 1100 else DEFAULT_CACHE_MB_SD
