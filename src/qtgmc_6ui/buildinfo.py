"""build-info.json reader (WP-B).  The writer lives in packaging/write-build-info.py (WP-G).

Development mode: without an installed build-info.json, ``load_or_dev()`` synthesises one
from the running interpreter and the contents of ``QTGMC6UI_PLUGIN_DIR`` so the working
tree can run against freshly built plugins (fixtures/plugins).
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from pathlib import Path

from . import osdep, paths
from .errors import ComponentError, ErrorCode
from .models import BuildInfo, PluginInfo, from_dict

# Component name -> (namespace, identifier, required).  Used to label plugin files found in
# a development plugin dir; the packaged build-info.json carries the real table.
KNOWN_COMPONENTS: dict[str, tuple[str, str, bool]] = {
    "akarin": ("akarin", "info.akarin.vsplugin", True),
    "resize2": ("resize2", "dev.setsugen.resize2", True),
    "mvutensils": ("mvu", "com.vapoursynth.mvutensils", True),
    "znedi3": ("znedi3", "xxx.abc.znedi3", True),
    "zsmooth": ("zsmooth", "com.adub.zsmooth", True),
    "vivtc": ("vivtc", "org.ivtc.v", False),
    "dfttest2": ("dfttest2_cpu", "io.github.amusementclub.dfttest2_cpu", False),
    "vszip": ("vszip", "com.julek.vszip", False),
}

# mvutensils builds three variants with one plugin id; only the baseline is loaded.
_VARIANT_SUFFIX = osdep.variant_suffix_re()


def load(path: Path | None = None) -> BuildInfo:
    """Read paths.build_info_path() (or ``path``); raise ComponentError(BUILD_INFO_MISSING)."""
    p = path or paths.build_info_path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ComponentError(
            ErrorCode.BUILD_INFO_MISSING,
            f"build-info.json not found: {p}",
            "reinstall qtgmc-6ui; for a development checkout set QTGMC6UI_PLUGIN_DIR (and optionally QTGMC6UI_BUILD_INFO)",
            {"path": str(p)},
        ) from None
    except OSError as e:
        raise ComponentError(ErrorCode.BUILD_INFO_MISSING, f"cannot read {p}: {e}", "reinstall qtgmc-6ui", {"path": str(p)}) from e
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        bi = from_dict(BuildInfo, data)
    except (ValueError, TypeError) as e:
        raise ComponentError(
            ErrorCode.BUILD_INFO_MISSING, f"build-info.json is malformed ({p}): {e}", "reinstall qtgmc-6ui", {"path": str(p)}
        ) from e
    return bi


def load_or_dev(path: Path | None = None) -> BuildInfo:
    """Like load(), but when the file is absent and QTGMC6UI_PLUGIN_DIR is set, synthesise a
    BuildInfo from the running interpreter and the plugin dir contents (development mode)."""
    try:
        return load(path)
    except ComponentError as e:
        if e.code != ErrorCode.BUILD_INFO_MISSING or "QTGMC6UI_PLUGIN_DIR" not in os.environ:
            raise
    return dev_build_info(Path(os.environ["QTGMC6UI_PLUGIN_DIR"]))


def component_for_file(name: str) -> str | None:
    stem = name.lower()
    for comp in KNOWN_COMPONENTS:
        if comp in stem:
            return comp
    return None


def scan_plugin_dir(plugin_dir: Path) -> list[PluginInfo]:
    """Plugin files in a development dir, variants of the same component skipped."""
    out: list[PluginInfo] = []
    seen: set[str] = set()
    if not plugin_dir.is_dir():
        return out
    for f in sorted(plugin_dir.rglob(osdep.plugin_glob())):
        if _VARIANT_SUFFIX.search(f.name):
            continue
        comp = component_for_file(f.name) or osdep.lib_component(f.stem)
        if comp in seen:
            continue
        seen.add(comp)
        ns, ident, req = KNOWN_COMPONENTS.get(comp, ("", "", False))
        out.append(PluginInfo(comp, ns, ident, "dev", str(f.relative_to(plugin_dir)), req, "bundled"))
    return out


def dev_build_info(plugin_dir: Path) -> BuildInfo:
    from . import __version__

    pkgs: dict[str, str] = {}
    for name in ("vsjetpack", "jetpytools"):
        try:
            pkgs[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    bi = BuildInfo(
        pkgver=__version__,
        pkgrel="dev",
        built_at="",
        march="dev",
        python_version=".".join(map(str, sys.version_info[:3])),
        python_minor=list(sys.version_info[:2]),
        python_executable=sys.executable,
        plugins=scan_plugin_dir(plugin_dir),
        python_packages=pkgs,
    )
    try:
        import vapoursynth as vs  # noqa: F401  (module attributes only, no core)

        bi.vapoursynth_core = int(vs.__version__.release_major)
        bi.vapoursynth_api = [int(vs.__api_version__.api_major), int(vs.__api_version__.api_minor)]
        bi.vapoursynth_pkgver = f"R{bi.vapoursynth_core}"
    except Exception:
        pass
    return bi
