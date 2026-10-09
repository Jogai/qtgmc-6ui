"""FFmpeg encoder/muxer discovery and optional tool detection (WP-C).

All lists come from the installed ffmpeg at runtime (``-muxers``, ``-encoders``,
``-h encoder=NAME``, ``-h muxer=NAME``); results are cached per ffmpeg version
string in paths.mux_matrix_cache_dir()/encoders-<version>.json.
Parsing notes: research/arch-facts.md §6 (legend block before data, 6 flag chars,
``preset``/``tune``/``profile`` of libx264/libx265 are plain <string> options, so
their choices are hard-coded in X264_PRESETS/X264_TUNES/X265_PRESETS/X265_TUNES).
Quirks handled: ``-h encoder=unknown`` prints "Codec 'x' is not recognized" with rc 0;
a trailing "Exiting with exit code 0" line; enum rows (5-space indent) belong to the
preceding option, flag rows have no numeric value column.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from . import paths
from .errors import ComponentError, ErrorCode, UsageError
from .models import EncoderInfo, EncoderOption, MuxerInfo, ToolInfo, from_dict, to_dict

X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo")
X264_TUNES = ("", "film", "animation", "grain", "stillimage", "fastdecode", "zerolatency")
X264_PROFILES = ("", "baseline", "main", "high", "high10", "high422", "high444")
X265_PRESETS = X264_PRESETS
X265_TUNES = ("", "psnr", "ssim", "grain", "fastdecode", "zerolatency", "animation")
X265_PROFILES = ("", "main", "main10", "main12", "main422-10", "main422-12", "main444-8", "main444-10", "main444-12")
KVAZAAR_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo")

OPTIONAL_TOOLS = ("x264", "x265", "kvazaar", "sox")
REQUIRED_TOOLS = ("ffmpeg", "ffprobe")
# hardware encoders need a GPU/driver path (hwupload, device init) that this app never sets up
# (brief §3: CPU path only); they are left out of list_encoders().
HW_ENCODER_SUFFIXES = ("_vaapi", "_nvenc", "_qsv", "_amf", "_vulkan", "_v4l2m2m", "_mediacodec", "_videotoolbox", "_mf", "_omx", "_cuvid", "_d3d11va", "_d3d12va")

# Hard-coded choices for <string> options that ffmpeg does not enumerate.
_STRING_CHOICES: dict[str, dict[str, tuple[str, ...]]] = {
    "libx264": {"preset": X264_PRESETS, "tune": X264_TUNES, "profile": X264_PROFILES},
    "libx264rgb": {"preset": X264_PRESETS, "tune": X264_TUNES},
    "libx265": {"preset": X265_PRESETS, "tune": X265_TUNES, "profile": X265_PROFILES},
}

_CAPTURE_TIMEOUT_S = 30.0

_lock = threading.RLock()
_mem: dict[str, Any] = {}  # per-process memo: version, muxers, encoders, details, muxer_details


# --------------------------------------------------------------------------- subprocess helpers
def subprocess_env() -> dict[str, str]:
    """runtime.subprocess_env() once WP-D lands; the same defaults meanwhile."""
    try:
        from . import runtime

        return runtime.subprocess_env()
    except NotImplementedError:
        env = dict(os.environ)
        env.setdefault("LC_CTYPE", "C.UTF-8")
        env["AV_LOG_FORCE_NOCOLOR"] = "1"
        return env


def child_kwargs() -> dict[str, Any]:
    """Popen keyword arguments for every child: env, own session, dies with its parent
    (runtime.pdeathsig_preexec) so a killed CLI leaves no orphan."""
    import signal

    from . import runtime

    return {"env": subprocess_env(), "start_new_session": True, "preexec_fn": runtime.pdeathsig_preexec(signal.SIGKILL)}


def _capture(cmd: list[str], timeout: float = _CAPTURE_TIMEOUT_S) -> tuple[int, str, str]:
    """rc, stdout, stderr (text, utf-8/replace).  Missing executable -> ComponentError."""
    if shutil.which(cmd[0]) is None:
        raise ComponentError(
            ErrorCode.TOOL_MISSING,
            f"{cmd[0]} not found in PATH",
            "install ffmpeg (pacman -S ffmpeg)" if cmd[0] in REQUIRED_TOOLS else f"install the optional package providing {cmd[0]}",
            {"tool": cmd[0]},
        )
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **child_kwargs())
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or b"").decode("utf-8", "replace"), (e.stderr or b"").decode("utf-8", "replace")
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def _ffmpeg_text(args: list[str], ffmpeg: str = "ffmpeg") -> str:
    rc, out, err = _capture([ffmpeg, "-hide_banner", *args])
    text = out if out.strip() else err
    if not text.strip() and rc != 0:
        raise ComponentError(ErrorCode.TOOL_MISSING, f"{ffmpeg} {' '.join(args)} failed (rc {rc})", "", {"stderr": err[-2000:]})
    return text


def _version_info(ffmpeg: str) -> tuple[str, str]:
    """(version string, build id).  The build id hashes the full ``-version`` text (configure
    line, library versions) and the binary's mtime, so a rebuilt ffmpeg with the same version
    string gets fresh caches."""
    key = f"version:{ffmpeg}"
    with _lock:
        if key in _mem:
            return _mem[key]
    _rc, out, err = _capture([ffmpeg, "-version"], timeout=15)
    text = out or err
    v = parse_version(text)
    mtime = ""
    exe = shutil.which(ffmpeg)
    if exe:
        try:
            mtime = str(int(os.stat(exe).st_mtime))
        except OSError:
            pass
    h = hashlib.sha1((text + "\n" + mtime).encode("utf-8", "replace")).hexdigest()[:12]
    info = (v, f"{v}-{h}")
    with _lock:
        _mem[key] = info
    return info


def ffmpeg_version(ffmpeg: str = "ffmpeg") -> str:
    """'n9.0.2' from the first line of ``ffmpeg -version`` (memoised per ffmpeg path)."""
    return _version_info(ffmpeg)[0]


def ffmpeg_build_id(ffmpeg: str = "ffmpeg") -> str:
    """Version string plus a hash of the build (cache key for everything derived from ffmpeg)."""
    return _version_info(ffmpeg)[1]


def parse_version(text: str) -> str:
    m = re.search(r"^ffmpeg version (\S+)", text, re.M)
    return m.group(1) if m else "unknown"


# --------------------------------------------------------------------------- parsers (pure)
_ENC_ROW = re.compile(r"^\s*([VAS])([A-Z.]{5})\s+(\S+)\s*(.*)$")
_MUX_ROW = re.compile(r"^ ([D ])([E ])([d ])\s+(\S+)\s*(.*)$")
_LEGEND_END = re.compile(r"^\s*-{3,}\s*$")
_CODEC_SUFFIX = re.compile(r"\s*\(codec ([^)]+)\)\s*$")


def parse_encoders(text: str) -> list[EncoderInfo]:
    """``ffmpeg -encoders``: rows after the ``------`` legend end; subtitle encoders dropped."""
    out: list[EncoderInfo] = []
    seen_legend_end = False
    for line in text.splitlines():
        if not seen_legend_end:
            if _LEGEND_END.match(line):
                seen_legend_end = True
            continue
        m = _ENC_ROW.match(line)
        if not m:
            continue
        kind_c, _flags, name, desc = m.groups()
        if kind_c == "S":
            continue
        codec = name
        cm = _CODEC_SUFFIX.search(desc)
        if cm:
            codec = cm.group(1)
            desc = desc[: cm.start()].rstrip()
        out.append(EncoderInfo(name=name, kind="video" if kind_c == "V" else "audio", description=desc.strip(), codec=codec))
    return out


def parse_muxers(text: str) -> list[MuxerInfo]:
    """``ffmpeg -muxers``: rows after the ``---`` legend end with the E flag."""
    out: list[MuxerInfo] = []
    seen_legend_end = False
    for line in text.splitlines():
        if not seen_legend_end:
            if _LEGEND_END.match(line):
                seen_legend_end = True
            continue
        m = _MUX_ROW.match(line)
        if not m:
            continue
        _d, e, _dev, name, desc = m.groups()
        if e != "E":
            continue
        out.append(MuxerInfo(name=name, description=desc.strip()))
    return out


_OPT_ROW = re.compile(r"^  -(\S+)\s+<(\w+)>\s+([A-Za-z.]{11})(?:\s+(.*))?$")
_ENUM_ROW = re.compile(r"^     (\S+)\s+(?:(-?[\w.]+)\s+)?([A-Za-z.]{11})(?:\s+(.*))?$")
_RANGE = re.compile(r"\s*\(from (\S+) to (\S+)\)")
_DEFAULT = re.compile(r"\s*\(default (.*)\)\s*$")


def _split_help(rest: str) -> tuple[str, str | None, str | None, str | None]:
    rest = rest or ""
    default = minimum = maximum = None
    m = _DEFAULT.search(rest)
    if m:
        default = m.group(1).strip('"')
        rest = rest[: m.start()]
    m = _RANGE.search(rest)
    if m:
        minimum, maximum = m.group(1), m.group(2)
        rest = rest[: m.start()] + rest[m.end():]
    return rest.strip(), default, minimum, maximum


def is_unknown_encoder_text(text: str) -> bool:
    return "is not recognized by FFmpeg" in text or "Unknown encoder" in text


def parse_encoder_help(text: str, name: str = "") -> EncoderInfo:
    """``ffmpeg -h encoder=NAME`` -> EncoderInfo with pix_fmts/sample info and AVOptions."""
    if is_unknown_encoder_text(text):
        raise UsageError(f"encoder '{name}' is not recognized by ffmpeg", "run `qtgmc-6ui encoders` for the list", encoder=name)
    info = EncoderInfo(name=name, kind="video")
    in_options = False
    current: EncoderOption | None = None
    for line in text.splitlines():
        if line.startswith("Exiting with exit code"):
            continue
        if not in_options:
            m = re.match(r"^Encoder (\S+) \[(.*)\]:\s*$", line)
            if m:
                info.name = info.name or m.group(1)
                info.description = m.group(2)
                continue
            s = line.strip()
            if s.startswith("Supported pixel formats:"):
                info.pix_fmts = s.split(":", 1)[1].split()
                info.kind = "video"
            elif s.startswith("Supported sample formats:"):
                info.sample_fmts = s.split(":", 1)[1].split()
                info.kind = "audio"
            elif s.startswith("Supported sample rates:"):
                info.sample_rates = [int(x) for x in s.split(":", 1)[1].split() if x.isdigit()]
                info.kind = "audio"
            elif s.endswith("AVOptions:"):
                in_options = True
            continue
        m = _OPT_ROW.match(line)
        if m:
            oname, otype, flags, rest = m.groups()
            help_, default, mn, mx = _split_help(rest or "")
            current = EncoderOption(name=oname, type=otype, help=help_, default=default, minimum=mn, maximum=mx)
            if "A" in flags[3:5] and "V" not in flags[3:5] and not info.sample_fmts and not info.pix_fmts:
                info.kind = "audio"
            info.options.append(current)
            continue
        m = _ENUM_ROW.match(line)
        if m and current is not None:
            current.choices.append(m.group(1))
            continue
        if line.strip() == "":
            continue
        if line.endswith("AVOptions:"):
            continue  # a second options block (rare); keep collecting
    for oname, choices in _STRING_CHOICES.get(info.name, {}).items():
        for opt in info.options:
            if opt.name == oname and not opt.choices:
                opt.choices = [c for c in choices if c]
    return info


def parse_muxer_help(text: str, name: str = "") -> MuxerInfo:
    info = MuxerInfo(name=name)
    if "is not recognized by FFmpeg" in text or "Unknown format" in text:
        raise UsageError(f"muxer '{name}' is not recognized by ffmpeg", "run `qtgmc-6ui encoders` for the list", muxer=name)
    for line in text.splitlines():
        m = re.match(r"^Muxer (\S+) \[(.*)\]:\s*$", line)
        if m:
            info.name = info.name or m.group(1)
            info.description = m.group(2)
            continue
        s = line.strip().rstrip(".")
        if s.startswith("Common extensions:"):
            info.extensions = [x.strip() for x in s.split(":", 1)[1].split(",") if x.strip()]
        elif s.startswith("Default video codec:"):
            info.default_video = s.split(":", 1)[1].strip()
        elif s.startswith("Default audio codec:"):
            info.default_audio = s.split(":", 1)[1].strip()
    return info


# --------------------------------------------------------------------------- cache
def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def _cache_path(ffmpeg: str) -> Path:
    return paths.mux_matrix_cache_dir() / f"encoders-{safe_name(ffmpeg_build_id(ffmpeg))}.json"


def _load_disk(ffmpeg: str) -> dict[str, Any]:
    p = _cache_path(ffmpeg)
    try:
        d = json.loads(p.read_text("utf-8"))
        if isinstance(d, dict) and d.get("schema") == 1:
            return d
    except (OSError, ValueError):
        pass
    return {"schema": 1, "muxers": None, "encoders": None, "details": {}, "muxer_details": {}}


def _save_disk(ffmpeg: str, d: dict[str, Any]) -> None:
    p = _cache_path(ffmpeg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}-{threading.get_ident()}.tmp")
        with _lock:
            text = json.dumps(d, indent=0, sort_keys=True)
        tmp.write_text(text, "utf-8")
        os.replace(tmp, p)
    except OSError:
        pass  # cache is an optimisation only


def _disk(ffmpeg: str) -> dict[str, Any]:
    key = f"disk:{ffmpeg}"
    with _lock:
        if key not in _mem:
            _mem[key] = _load_disk(ffmpeg)
        return _mem[key]


def invalidate_cache() -> None:
    with _lock:
        _mem.clear()
    try:
        for p in paths.mux_matrix_cache_dir().glob("encoders-*.json"):
            p.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- public API
def list_muxers(ffmpeg: str = "ffmpeg") -> list[MuxerInfo]:
    d = _disk(ffmpeg)
    if d.get("muxers") is None:
        muxers = parse_muxers(_ffmpeg_text(["-muxers"], ffmpeg))
        with _lock:
            d["muxers"] = [to_dict(m) for m in muxers]
        _save_disk(ffmpeg, d)
    return [from_dict(MuxerInfo, m) for m in d["muxers"]]


def list_encoders(ffmpeg: str = "ffmpeg") -> list[EncoderInfo]:
    """Video + audio encoders with the E flag; hardware encoders are kept (availability is
    only known by probing)."""
    d = _disk(ffmpeg)
    if d.get("encoders") is None:
        encs = parse_encoders(_ffmpeg_text(["-encoders"], ffmpeg))
        with _lock:
            d["encoders"] = [to_dict(e) for e in encs]
        _save_disk(ffmpeg, d)
    return [from_dict(EncoderInfo, e) for e in d["encoders"] if not is_hw_encoder(e["name"])]


def is_hw_encoder(name: str) -> bool:
    return name.endswith(HW_ENCODER_SUFFIXES)


def encoder_names(kind: str | None = None, ffmpeg: str = "ffmpeg") -> set[str]:
    return {e.name for e in list_encoders(ffmpeg) if kind is None or e.kind == kind}


def muxer_names(ffmpeg: str = "ffmpeg") -> set[str]:
    return {m.name for m in list_muxers(ffmpeg)}


def encoder_details(name: str, ffmpeg: str = "ffmpeg") -> EncoderInfo:
    """``-h encoder=NAME``: pix_fmts, sample formats/rates and AVOptions with enum choices.
    Unknown encoder prints 'Codec ... is not recognized' with rc 0: detect by text."""
    d = _disk(ffmpeg)
    with _lock:
        details = d.setdefault("details", {})
        cached = details.get(name)
    if cached is None:
        info = parse_encoder_help(_ffmpeg_text(["-h", f"encoder={name}"], ffmpeg), name)
        # kind from the listing is authoritative (pcm encoders print no sample info)
        for e in parse_encoders_cached(ffmpeg):
            if e.name == name:
                info.kind = e.kind
                info.codec = e.codec
                info.description = info.description or e.description
                break
        cached = to_dict(info)
        with _lock:
            details[name] = cached
        _save_disk(ffmpeg, d)
    return from_dict(EncoderInfo, cached)


def parse_encoders_cached(ffmpeg: str = "ffmpeg") -> list[EncoderInfo]:
    """Every encoder of the listing including hardware ones (list_encoders hides those)."""
    list_encoders(ffmpeg)
    return [from_dict(EncoderInfo, e) for e in _disk(ffmpeg)["encoders"]]


def muxer_details(name: str, ffmpeg: str = "ffmpeg") -> MuxerInfo:
    d = _disk(ffmpeg)
    with _lock:
        details = d.setdefault("muxer_details", {})
        cached = details.get(name)
    if cached is None:
        info = parse_muxer_help(_ffmpeg_text(["-h", f"muxer={name}"], ffmpeg), name)
        cached = to_dict(info)
        with _lock:
            details[name] = cached
        _save_disk(ffmpeg, d)
    return from_dict(MuxerInfo, cached)


# --------------------------------------------------------------------------- tools
_VERSION_PATTERNS: dict[str, tuple[list[str], str]] = {
    # tool: (argv suffix, regex with group 1 = version)
    "ffmpeg": (["-version"], r"^ffmpeg version (\S+)"),
    "ffprobe": (["-version"], r"^ffprobe version (\S+)"),
    "x264": (["--version"], r"^x264 (\S+(?: \S+)?)"),
    "x265": (["--version"], r"HEVC encoder version (\S+)"),
    "kvazaar": (["--version"], r"^Kvazaar v?(\S+)"),
    "sox": (["--version"], r"^sox:\s+(.*\S)"),
}


def tool_info(name: str) -> ToolInfo:
    """Memoised per process and PATH (a changed PATH re-detects)."""
    key = f"tool:{name}:{os.environ.get('PATH', '')}"
    with _lock:
        if key in _mem:
            return _mem[key]
    info = _tool_info(name)
    with _lock:
        _mem[key] = info
    return info


def _tool_info(name: str) -> ToolInfo:
    path = shutil.which(name)
    if not path:
        note = "required" if name in REQUIRED_TOOLS else f"optional (pacman -S {name})"
        return ToolInfo(name=name, available=False, note=note)
    args, pattern = _VERSION_PATTERNS.get(name, (["--version"], r"(\d+(?:\.\d+)+)"))
    try:
        rc, out, err = _capture([path, *args], timeout=15)
    except ComponentError as e:
        return ToolInfo(name=name, path=path, available=False, note=e.message)
    text = (out or "") + "\n" + (err or "")
    m = re.search(pattern, text, re.M)
    version = m.group(1).strip() if m else None
    note = ""
    if name == "x265":
        bm = re.search(r"build info .*?\]\s*(\d+bit(?:\+\d+bit)*)", text)
        if bm:
            note = bm.group(1)
    if name == "sox":
        note = "audio processing (resample/dither/gain)"
    available = version is not None or rc == 0
    if not available:
        note = f"{name} --version failed (rc {rc})"
    return ToolInfo(name=name, path=path, version=version, available=available, note=note)


def detect_tools() -> list[ToolInfo]:
    """ffmpeg/ffprobe (required) and OPTIONAL_TOOLS via shutil.which + ``--version`` parsing."""
    return [tool_info(n) for n in (*REQUIRED_TOOLS, *OPTIONAL_TOOLS)]


def tool_available(name: str) -> bool:
    return tool_info(name).available
