"""``qtgmc-6ui doctor`` (WP-B): the first thing a user runs when something breaks.

Checks (id -> title), each Status ok/warn/fail/skip with a hint:
  build_info        build-info.json present and readable
  python_abi        running python minor == build-info python minor
  vapoursynth_abi   running core/api == build-info
  vapoursynth_core  core creates with autoload disabled
  plugin:<name>     each bundled/repo plugin loads (namespace, identifier, version)
  pypkg:<name>      vsjetpack / jetpytools importable and version == build-info
  tool:ffmpeg       ffmpeg + ffprobe present (fail), x264/x265/kvazaar/sox (skip when absent)
  qtgmc_smoke       QTempGaussMC on a 64x64 BlankClip, 4 frames (proves the whole stack)
  locale            LC_CTYPE/PYTHONUTF8 give a UTF-8 filesystem encoding
Output is suitable for pasting into a bug report (``--json`` for the DoctorReport).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import platform
import re
import shutil
import sys
import time
from typing import Any

from . import __version__, buildinfo, osdep, paths, qtgmc, runtime, vsenv
from .errors import ComponentError, ErrorCode
from .models import (
    BuildInfo,
    DoctorCheck,
    DoctorReport,
    FieldOrder,
    MasterSettings,
    Preset,
    Status,
    ToolInfo,
)

REQUIRED_TOOLS = ("ffmpeg", "ffprobe")
OPTIONAL_TOOLS = ("x264", "x265", "kvazaar", "sox")
_VERSION_ARGS = {"ffmpeg": ["-version"], "ffprobe": ["-version"], "x264": ["--version"], "x265": ["--version"],
                 "kvazaar": ["--version"], "sox": ["--version"]}


def _tool(name: str) -> ToolInfo:
    path = shutil.which(name)
    if not path:
        return ToolInfo(name, None, None, False, "not installed")
    cmd = [path, *_VERSION_ARGS.get(name, ["--version"])]
    try:
        r = runtime.run_capture(cmd, timeout=10)
    except ComponentError as e:
        return ToolInfo(name, path, None, False, f"cannot run: {e.message}")
    if r.timed_out:
        return ToolInfo(name, path, None, False, f"cannot run: Command '{cmd}' timed out after 10 seconds")
    text = (r.stdout or "") + (r.stderr or "")
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    m = re.search(r"(?:version|v)?\s*([0-9][0-9A-Za-z.+\-]*)", first)
    return ToolInfo(name, path, m.group(1) if m else first[:40], True, "")


def detect_tools() -> list[ToolInfo]:
    try:
        from .encoders import detect_tools as enc_detect

        tools = enc_detect()
        if tools:
            return list(tools)
    except Exception:
        pass
    return [_tool(n) for n in REQUIRED_TOOLS + OPTIONAL_TOOLS]


def _environment() -> dict[str, str]:
    env = {k: os.environ[k] for k in osdep.diagnostic_env_keys() if k in os.environ}
    env["filesystem_encoding"] = sys.getfilesystemencoding()
    env["platform"] = platform.platform()
    env["cpu"] = platform.processor() or platform.machine()
    env["cpu_count"] = str(os.cpu_count() or 0)
    env["plugin_dir"] = str(paths.plugin_dir())
    env["build_info_path"] = str(paths.build_info_path())
    env["venv_python"] = sys.executable
    return env


def _smoke(vc: vsenv.VSCore) -> str:
    """QTGMC bob on a tiny clip; returns a one-line description or raises."""
    import vapoursynth as vs

    core = vc.core
    clip = core.std.BlankClip(width=64, height=64, format=vs.YUV420P8, length=8, fpsnum=25, fpsden=1, color=[80, 128, 128])
    noise = core.std.AddNoise if hasattr(core, "noise") else None  # not bundled; constant clip is fine
    del noise
    clip = core.std.SetFieldBased(clip, 1)
    t0 = time.monotonic()
    out = qtgmc.deinterlace(clip, MasterSettings(preset=Preset.FAST), FieldOrder.BFF, Preset.FAST)
    for i in range(out.num_frames):
        out.get_frame(i)
    return f"{out.num_frames} frames 64x64 bobbed with preset fast in {time.monotonic() - t0:.2f} s"


def run(*, load_plugins: bool = True, smoke: bool = True) -> DoctorReport:
    rep = DoctorReport(environment=_environment(), runtime=vsenv.runtime_versions())
    rep.runtime["qtgmc-6ui"] = __version__
    checks = rep.checks

    # build-info
    bi: BuildInfo | None = None
    try:
        bi = buildinfo.load()
        checks.append(DoctorCheck("build_info", "build-info.json", Status.OK,
                                  f"{paths.build_info_path()} (pkg {bi.pkgver}-{bi.pkgrel}, built {bi.built_at or '?'}, march {bi.march})"))
    except ComponentError as e:
        try:
            bi = buildinfo.load_or_dev()
            checks.append(DoctorCheck("build_info", "build-info.json", Status.WARN,
                                      f"not installed; development mode from QTGMC6UI_PLUGIN_DIR={paths.plugin_dir()}", e.hint))
        except ComponentError:
            checks.append(DoctorCheck("build_info", "build-info.json", Status.FAIL, e.message, e.hint))
    rep.build_info = bi

    # ABI
    vs_ok = "vapoursynth" in rep.runtime and not rep.runtime["vapoursynth"].startswith("missing")
    if not vs_ok:
        checks.append(DoctorCheck("vapoursynth_import", "vapoursynth module", Status.FAIL, rep.runtime.get("vapoursynth", ""),
                                  f"install {osdep.package_ref('vapoursynth')}"))
    if bi is not None and vs_ok:
        problems = vsenv.check_abi(bi)
        py = [p for p in problems if "python" in p]
        vsp = [p for p in problems if "python" not in p]
        checks.append(DoctorCheck("python_abi", f"python {rep.runtime['python']} vs build {bi.python_version or '?'}",
                                  Status.FAIL if py else Status.OK, "; ".join(py), "reinstall/rebuild qtgmc-6ui" if py else ""))
        checks.append(DoctorCheck("vapoursynth_abi", f"vapoursynth {rep.runtime.get('vapoursynth')} {rep.runtime.get('api')} vs build R{bi.vapoursynth_core or '?'}",
                                  Status.FAIL if vsp else Status.OK, "; ".join(vsp), "reinstall/rebuild qtgmc-6ui" if vsp else ""))
        for p in vsenv.check_python_packages(bi):
            checks.append(DoctorCheck("pypkg", "python package versions", Status.WARN, p, "reinstall qtgmc-6ui"))

    # python packages
    for name in vsenv.PYTHON_PACKAGES:
        ver = rep.runtime.get(name, "missing")
        ok = ver != "missing" and importlib.util.find_spec(name if name != "vsjetpack" else "vsdeinterlace") is not None
        checks.append(DoctorCheck(f"pypkg:{name}", f"python package {name}", Status.OK if ok else Status.FAIL, ver,
                                  "" if ok else "reinstall qtgmc-6ui (bundled venv is incomplete)"))
    ver = rep.runtime.get("dfttest2", "missing")
    checks.append(DoctorCheck("pypkg:dfttest2", "python package dfttest2 (optional denoise)",
                              Status.OK if ver != "missing" else Status.SKIP, ver, "" if ver != "missing" else "denoise (--denoise) is unavailable"))

    # core + plugins
    plugin_dir = paths.plugin_dir()
    if bi is not None and any(p.origin != "repo" for p in bi.plugins) and not plugin_dir.is_dir():
        checks.append(DoctorCheck("plugin_dir", "bundled plugin directory", Status.FAIL, f"missing: {plugin_dir}", "reinstall qtgmc-6ui"))
    if load_plugins and vs_ok and bi is not None:
        vc: vsenv.VSCore | None = None
        try:
            if vsenv.current() is not None:
                raise ComponentError(ErrorCode.INTERNAL, "a core is already open in this process")
            vc = vsenv.open_core(build_info=bi, check_abi=False)
            checks.append(DoctorCheck("vapoursynth_core", "VapourSynth core (autoload disabled)", Status.OK,
                                      f"flags {vc.core.flags}, {vc.core.num_threads} threads, cache {vc.core.max_cache_size} MB"))
        except ComponentError as e:
            checks.append(DoctorCheck("vapoursynth_core", "VapourSynth core / plugin loading", Status.FAIL, e.message, e.hint))
            rep.plugins.append({"name": e.details.get("plugin", "?"), "loaded": False, "error": e.message, "path": e.details.get("path", ""), "namespace": ""})
        if vc is not None:
            try:
                table = {row["namespace"]: row for row in vsenv.plugin_table(vc.core)}
                plugins = list(bi.plugins)
                if not any(p.origin == "repo" for p in plugins):
                    plugins = list(vsenv.repo_plugins()) + plugins
                for p in plugins:
                    ns = p.namespace or next((n for n, r in table.items() if r["path"] == vc.loaded.get(n, "")), "")
                    loaded = ns in vc.loaded if ns else False
                    row = table.get(ns, {}) if loaded else {}
                    entry = {"name": p.name, "loaded": loaded, "error": vc.skipped.get(p.name, ""), "path": vc.loaded.get(ns, ""),
                             "namespace": ns, "identifier": row.get("identifier", ""), "version": row.get("version", ""),
                             "pinned": p.version, "required": p.required, "origin": p.origin}
                    rep.plugins.append(entry)
                    if loaded:
                        st, detail = Status.OK, f"{ns} {row.get('identifier', '')} {row.get('version', '')} ({p.origin} {p.version}) {entry['path']}"
                    elif p.required:
                        st, detail = Status.FAIL, vc.skipped.get(p.name, "not loaded")
                    else:
                        st, detail = Status.SKIP, vc.skipped.get(p.name, "optional, not loaded")
                    checks.append(DoctorCheck(f"plugin:{p.name}", f"plugin {p.name}", st, detail,
                                              "reinstall/rebuild qtgmc-6ui" if st == Status.FAIL else ""))
                extra = sorted(set(table) - set(vc.loaded) - {"std", "resize", "text"})
                if extra:
                    checks.append(DoctorCheck("plugin_extra", "unexpected plugins in the core", Status.WARN, ", ".join(extra),
                                              "autoload should be disabled; report this"))
                if smoke:
                    required_ok = all(vc.has(ns) for ns in vsenv.REQUIRED_NAMESPACES)
                    if not required_ok:
                        checks.append(DoctorCheck("qtgmc_smoke", "QTGMC smoke run", Status.SKIP, "required plugins missing"))
                    else:
                        try:
                            checks.append(DoctorCheck("qtgmc_smoke", "QTGMC smoke run", Status.OK, _smoke(vc)))
                        except Exception as e:  # anything from the filter stack
                            checks.append(DoctorCheck("qtgmc_smoke", "QTGMC smoke run", Status.FAIL, f"{type(e).__name__}: {e}",
                                                      "a plugin or python package is broken; paste this report into a bug report"))
            finally:
                vc.close()
    elif not load_plugins:
        checks.append(DoctorCheck("vapoursynth_core", "VapourSynth core / plugins", Status.SKIP, "--no-load"))

    # tools
    rep.tools = detect_tools()
    by_name = {t.name: t for t in rep.tools}
    for name in REQUIRED_TOOLS:
        t = by_name.get(name) or _tool(name)
        checks.append(DoctorCheck(f"tool:{name}", f"{name}", Status.OK if t.available else Status.FAIL,
                                  f"{t.version or ''} {t.path or ''}".strip() or t.note, "" if t.available else f"install {osdep.package_ref('ffmpeg')}"))
    for name in OPTIONAL_TOOLS:
        t = by_name.get(name) or _tool(name)
        checks.append(DoctorCheck(f"tool:{name}", f"{name} (optional)", Status.OK if t.available else Status.SKIP,
                                  f"{t.version or ''} {t.path or ''}".strip() if t.available else t.note or "not installed"))

    # locale
    fse = sys.getfilesystemencoding().lower().replace("-", "")
    if fse == "utf8":
        checks.append(DoctorCheck("locale", "filesystem encoding", Status.OK, f"utf-8 (LANG={os.environ.get('LANG', '')!r}, PYTHONUTF8={os.environ.get('PYTHONUTF8', '')!r})"))
    else:
        checks.append(DoctorCheck("locale", "filesystem encoding", Status.FAIL, f"{fse}; non-ASCII paths will break",
                                  "set PYTHONUTF8=1 or LC_CTYPE=C.UTF-8 (the launcher does this)"))

    fails = [c for c in checks if c.status == Status.FAIL]
    warns = [c for c in checks if c.status == Status.WARN]
    rep.ok = not fails
    rep.summary = ("all checks passed" if not fails else f"{len(fails)} check(s) FAILED: " + ", ".join(c.id for c in fails)) + (
        f"; {len(warns)} warning(s)" if warns else "")
    return rep


_MARK = {Status.OK: " ok ", Status.WARN: "WARN", Status.FAIL: "FAIL", Status.SKIP: "skip"}


def render_text(report: DoctorReport) -> str:
    rt = report.runtime
    lines = [
        f"qtgmc-6UI doctor  (qtgmc-6ui {rt.get('qtgmc-6ui', __version__)})",
        f"python {rt.get('python', '?')} ({rt.get('python_executable', '')})  vapoursynth {rt.get('vapoursynth', '?')} api {rt.get('api', '?')}",
        f"vsjetpack {rt.get('vsjetpack', '?')}  jetpytools {rt.get('jetpytools', '?')}  numpy {rt.get('numpy', '?')}  dfttest2 {rt.get('dfttest2', '?')}",
    ]
    if report.build_info:
        bi = report.build_info
        lines.append(f"build-info: {bi.pkgver}-{bi.pkgrel} built {bi.built_at or '?'} for python {bi.python_version or '?'} "
                     f"vapoursynth R{bi.vapoursynth_core or '?'} api {'.'.join(map(str, bi.vapoursynth_api)) or '?'} march {bi.march}")
    lines.append("")
    for c in report.checks:
        lines.append(f"[{_MARK[c.status]}] {c.id:<22} {c.title}" + (f": {c.detail}" if c.detail else ""))
        if c.hint and c.status in (Status.FAIL, Status.WARN):
            lines.append(f"       hint: {c.hint}")
    lines.append("")
    lines.append("environment:")
    for k, v in report.environment.items():
        lines.append(f"  {k}={v}")
    lines.append("")
    lines.append(("OK: " if report.ok else "PROBLEMS: ") + report.summary)
    return "\n".join(lines) + "\n"


def startup_check() -> list[str]:
    """Cheap subset for app startup (no core creation): build-info, python/VS ABI, plugin dir.
    Returns problem lines; empty means fine."""
    problems: list[str] = []
    try:
        bi = buildinfo.load_or_dev()
    except ComponentError as e:
        return [f"{e.message} ({e.hint})"]
    try:
        problems += vsenv.check_abi(bi)
    except ComponentError as e:
        problems.append(f"{e.message} ({e.hint})")
        return problems
    plugin_dir = paths.plugin_dir()
    bundled = [p for p in bi.plugins if p.origin != "repo"]
    if bundled and not plugin_dir.is_dir():
        problems.append(f"bundled plugin directory missing: {plugin_dir}; reinstall qtgmc-6ui")
    else:
        for p in bundled:
            path = plugin_dir / p.file if not os.path.isabs(p.file) else p.file
            if p.required and not os.path.isfile(path):
                problems.append(f"required plugin {p.name} missing: {path}; reinstall/rebuild qtgmc-6ui")
    sysdir = paths.system_vs_plugin_dir()
    if sysdir is None:
        problems.append(f"the vapoursynth Python module is not importable; install {osdep.package_ref('vapoursynth')}")
    elif osdep.find_plugin(sysdir, "bestsource") is None:
        problems.append(f"bestsource plugin missing; install {osdep.package_ref('vapoursynth-plugin-bestsource')}")
    for name in vsenv.PYTHON_PACKAGES:
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"python package {name} missing; reinstall qtgmc-6ui")
    for tool in REQUIRED_TOOLS:
        if not shutil.which(tool):
            problems.append(f"{tool} not found; install {osdep.package_ref('ffmpeg')}")
    return problems


def report_dict(report: DoctorReport) -> dict[str, Any]:
    return report.to_dict()
