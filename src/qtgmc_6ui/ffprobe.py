"""ffprobe wrapper (WP-A).  Container/stream facts only; never trusted for field order.

Also hosts the tiny subprocess helper shared by the analysis modules
(``run_tool``): arguments are passed as UTF-8 bytes (surrogateescape) so
non-ASCII paths survive a ``LANG=C`` process, children get ``LC_CTYPE=C.UTF-8``
and ``AV_LOG_FORCE_NOCOLOR=1``, and output is decoded as UTF-8 with
replacement.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from fractions import Fraction
from typing import Any

from . import osdep
from .errors import ComponentError, ErrorCode, InputError
from .models import AudioProbe, Colorimetry, ProbeInfo, VideoProbe

STDERR_TAIL_LINES = 40


# --------------------------------------------------------------------------- subprocess helper
def tool_env() -> dict[str, str]:
    """os.environ plus a UTF-8 ctype for children and colour-free ffmpeg logs (osdep.tool_env)."""
    return osdep.tool_env()


def _argv_bytes(cmd: Sequence[str]) -> list[bytes]:
    return [a.encode("utf-8", "surrogateescape") if isinstance(a, str) else bytes(a) for a in cmd]


def run_tool(cmd: Sequence[str], *, timeout: float | None = None) -> tuple[int, str, str]:
    """Run ``cmd`` to completion; returns (returncode, stdout, stderr) as text.

    Raises ComponentError(TOOL_MISSING) when the executable cannot be started.
    """
    from . import runtime

    try:
        r = runtime.run_capture(_argv_bytes(cmd), timeout=timeout, env=tool_env())
    except ComponentError as e:
        raise ComponentError(
            ErrorCode.TOOL_MISSING,
            f"{cmd[0]} is not installed or not in PATH",
            "Install the ffmpeg package (it provides ffmpeg and ffprobe).",
            {"tool": str(cmd[0]), "error": e.message},
        ) from e
    if r.timed_out:
        raise InputError(
            ErrorCode.ANALYSIS_FAILED,
            f"{cmd[0]} did not finish within {timeout} s",
            "",
            {"cmd": [str(c) for c in cmd]},
        )
    return r.returncode, r.stdout, r.stderr


def stderr_tail(text: str, n: int = STDERR_TAIL_LINES) -> list[str]:
    lines = [ln for ln in text.replace("\r", "\n").split("\n") if ln.strip()]
    return lines[-n:]


def input_path(path: str) -> str:
    """Absolute path for ffmpeg/ffprobe (never mistaken for an option or a protocol)."""
    return os.path.abspath(path)


def check_input_exists(path: str) -> None:
    if not os.path.exists(path):
        raise InputError(ErrorCode.INPUT_NOT_FOUND, f"input file not found: {path}", "", {"path": path})
    if not os.path.isfile(path):
        raise InputError(ErrorCode.INPUT_UNREADABLE, f"input is not a regular file: {path}", "", {"path": path})


# --------------------------------------------------------------------------- parsing helpers
def parse_fraction(text: Any, default: Fraction | None = None) -> Fraction | None:
    """'30000/1001', '16:15', '25' -> Fraction; 0/0, N/A, empty -> default."""
    if text is None:
        return default
    s = str(text).strip().replace(":", "/")
    if not s or s.upper() == "N/A":
        return default
    try:
        f = Fraction(s)
    except (ValueError, ZeroDivisionError):
        return default
    if f <= 0:
        return default
    return f


def parse_duration(text: Any) -> float | None:
    """'10.960000' or Matroska tag '00:00:04.004000000' -> seconds."""
    if text is None:
        return None
    s = str(text).strip()
    if not s or s.upper() == "N/A":
        return None
    if ":" in s:
        try:
            parts = [float(p) for p in s.split(":")]
        except ValueError:
            return None
        secs = 0.0
        for p in parts:
            secs = secs * 60 + p
        return secs
    try:
        return float(s)
    except ValueError:
        return None


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


_PIXFMT_DEPTH = re.compile(r"(?:p|a)?(\d{1,2})(?:le|be)?$")


def bit_depth_from_pix_fmt(pix_fmt: str) -> int:
    """yuv420p10le -> 10, yuv422p -> 8, gray16le -> 16, rgb48le -> 16 per sample."""
    if not pix_fmt:
        return 8
    m = _PIXFMT_DEPTH.search(pix_fmt)
    if not m:
        return 8
    d = int(m.group(1))
    if pix_fmt.startswith(("rgb", "bgr", "gbr")) and d in (24, 48, 64, 32):
        return {24: 8, 32: 8, 48: 16, 64: 16}[d]
    return d if 8 <= d <= 16 else 8


def _tag(stream: dict[str, Any], *names: str) -> str | None:
    tags = stream.get("tags") or {}
    lowered = {str(k).lower(): v for k, v in tags.items()}
    for n in names:
        if n.lower() in lowered:
            return str(lowered[n.lower()])
    return None


def _stream_duration(stream: dict[str, Any], fmt: dict[str, Any]) -> float:
    for cand in (stream.get("duration"), _tag(stream, "DURATION"), fmt.get("duration")):
        d = parse_duration(cand)
        if d is not None and d > 0:
            return d
    return 0.0


def _video_from_stream(stream: dict[str, Any], fmt: dict[str, Any]) -> VideoProbe:
    fps = parse_fraction(stream.get("r_frame_rate")) or parse_fraction(stream.get("avg_frame_rate")) or Fraction(25, 1)
    sar = parse_fraction(stream.get("sample_aspect_ratio"), Fraction(1, 1)) or Fraction(1, 1)
    dar = parse_fraction(stream.get("display_aspect_ratio"), None)
    width, height = _int(stream.get("width")), _int(stream.get("height"))
    if dar is None and width and height:
        dar = Fraction(width, height) * sar
    pix_fmt = str(stream.get("pix_fmt") or "")
    depth = _int(stream.get("bits_per_raw_sample")) or bit_depth_from_pix_fmt(pix_fmt)
    duration = _stream_duration(stream, fmt)

    nb = _int(stream.get("nb_frames"))
    exact = nb > 0
    if not exact:
        nb = _int(_tag(stream, "NUMBER_OF_FRAMES", "NUMBER_OF_FRAMES-eng"))
        exact = nb > 0
    nb_frames: int | None = nb if nb > 0 else None
    if nb_frames is None and duration > 0:
        nb_frames = int(round(duration * fps))
        exact = False

    raw = [str(stream.get(k) or "unknown") for k in ("color_space", "color_primaries", "color_transfer", "color_range")]
    tags = Colorimetry(
        matrix=raw[0],
        primaries=raw[1],
        transfer=raw[2],
        range=raw[3],
        source="tags" if any(x.lower() not in ("unknown", "unspecified", "reserved") for x in raw) else "unknown",
    )
    return VideoProbe(
        codec=str(stream.get("codec_name") or ""),
        codec_long=str(stream.get("codec_long_name") or ""),
        width=width,
        height=height,
        pix_fmt=pix_fmt,
        bit_depth=depth,
        fps=fps,
        avg_fps=parse_fraction(stream.get("avg_frame_rate")),
        start_time_s=parse_duration(stream.get("start_time")) or 0.0,
        sar=sar,
        dar=dar,
        nb_frames=nb_frames,
        nb_frames_exact=exact,
        duration_s=duration,
        container_field_order=str(stream.get("field_order") or "unknown"),
        color_tags=tags,
    )


def _audio_from_stream(stream: dict[str, Any], fmt: dict[str, Any]) -> AudioProbe:
    depth = _int(stream.get("bits_per_raw_sample")) or _int(stream.get("bits_per_sample"))
    return AudioProbe(
        index=_int(stream.get("index")),
        codec=str(stream.get("codec_name") or ""),
        sample_rate=_int(stream.get("sample_rate")),
        channels=_int(stream.get("channels")),
        channel_layout=str(stream.get("channel_layout") or ""),
        bit_depth=depth or None,
        duration_s=_stream_duration(stream, fmt),
        start_time_s=parse_duration(stream.get("start_time")) or 0.0,
        language=_tag(stream, "language") or "",
        sample_fmt=str(stream.get("sample_fmt") or ""),
    )


def _is_attached_pic(stream: dict[str, Any]) -> bool:
    disp = stream.get("disposition") or {}
    return bool(_int(disp.get("attached_pic")) or _int(disp.get("timed_thumbnails")))


def probe_from_json(data: dict[str, Any], path: str) -> ProbeInfo:
    """Map an ffprobe JSON document (``-show_format -show_streams``) to ProbeInfo."""
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    videos = [s for s in streams if s.get("codec_type") == "video" and not _is_attached_pic(s)]
    if not videos:
        videos = [s for s in streams if s.get("codec_type") == "video"]
    if not videos:
        raise InputError(
            ErrorCode.INPUT_NO_VIDEO,
            f"no video stream in {path}",
            "qtgmc-6ui only processes files with a video stream.",
            {"path": path, "streams": len(streams)},
        )
    v = videos[0]
    size = _int(fmt.get("size"))
    if not size:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
    return ProbeInfo(
        path=path,
        container=str(fmt.get("format_name") or ""),
        size_bytes=size,
        duration_s=parse_duration(fmt.get("duration")) or 0.0,
        start_time_s=parse_duration(fmt.get("start_time")) or 0.0,
        video=_video_from_stream(v, fmt),
        audio=[_audio_from_stream(s, fmt) for s in streams if s.get("codec_type") == "audio"],
        video_stream_index=_int(v.get("index")),
    )


# --------------------------------------------------------------------------- public API
def probe(path: str, ffprobe: str = "ffprobe") -> ProbeInfo:
    """Run ``ffprobe -v error -print_format json -show_format -show_streams`` and map it.

    Raises InputError(INPUT_NOT_FOUND / INPUT_UNREADABLE / INPUT_NO_VIDEO).
    ``nb_frames`` is exact when the container reports it, else estimated from
    duration * fps with ``nb_frames_exact=False``.
    """
    check_input_exists(path)
    apath = input_path(path)
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-i", apath]
    rc, out, err = run_tool(cmd)
    data: dict[str, Any] | None = None
    if out.strip():
        try:
            data = json.loads(out)
        except ValueError:
            data = None
    if rc != 0 or not data or not data.get("streams"):
        raise InputError(
            ErrorCode.INPUT_UNREADABLE,
            f"ffprobe cannot read {path}",
            "The file is not a video FFmpeg understands, or it is damaged.",
            {"path": path, "returncode": rc, "stderr": stderr_tail(err)},
        )
    return probe_from_json(data, apath)


def count_frames(path: str, ffprobe: str = "ffprobe") -> int:
    """Exact frame count (``-count_frames``); slow on long files, used by tests/verify."""
    check_input_exists(path)
    cmd = [
        ffprobe, "-v", "error", "-count_frames", "-select_streams", "V:0",
        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", "-i", input_path(path),
    ]
    rc, out, err = run_tool(cmd)
    m = re.search(r"\d+", out)
    if rc != 0 or not m:
        raise InputError(
            ErrorCode.INPUT_UNREADABLE,
            f"ffprobe could not count the frames of {path}",
            "",
            {"path": path, "returncode": rc, "stderr": stderr_tail(err)},
        )
    return int(m.group(0))


_VERSION_RE = re.compile(r"ffmpeg version\s+(\S+)")


def ffmpeg_version(ffmpeg: str = "ffmpeg") -> str:
    """First token after 'ffmpeg version' (e.g. 'n9.0.2')."""
    rc, out, err = run_tool([ffmpeg, "-version"])
    m = _VERSION_RE.search(out) or _VERSION_RE.search(err)
    if not m:
        raise ComponentError(
            ErrorCode.TOOL_MISSING,
            f"{ffmpeg} -version printed no version line",
            "Install the ffmpeg package.",
            {"returncode": rc, "stdout": stderr_tail(out, 5), "stderr": stderr_tail(err, 5)},
        )
    return m.group(1)
