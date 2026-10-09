"""FFmpeg ``idet`` runner and parser (WP-A).

Parsing rule: ffmpeg prints TWO summary blocks (an all-zero block from the
probing graph first); take the LAST occurrence of each of
``Repeated Fields:``, ``Single frame detection:``, ``Multi frame detection:``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from .errors import ErrorCode, InputError
from .ffprobe import input_path, run_tool, stderr_tail
from .models import IdetCounts, IdetWindow, RepeatCounts

IDET_WINDOW_FRAMES = 300  # frames per sampled window
IDET_WINDOWS = 5  # windows spread over the file (skipping the first/last 2 %)
IDET_FULL_SCAN_MAX_FRAMES = 1500  # files up to this size are scanned completely in one pass
IDET_UNKNOWN_LENGTH_FULL_SCAN_S = 60.0  # unknown frame count: full scan when shorter than this
SAMPLE_LO, SAMPLE_HI = 0.02, 0.98  # sampled span of the duration

FIELDMATCH_FILTER = "fieldmatch=order=auto:combmatch=full"

_RE_REPEATED = re.compile(r"Repeated Fields:\s*Neither:\s*(\d+)\s+Top:\s*(\d+)\s+Bottom:\s*(\d+)")
_RE_SINGLE = re.compile(
    r"Single frame detection:\s*TFF:\s*(\d+)\s+BFF:\s*(\d+)\s+Progressive:\s*(\d+)\s+Undetermined:\s*(\d+)"
)
_RE_COMBED = re.compile(r"Frame #\d+ at [\d.]+ is still interlaced")
_RE_STATS = re.compile(r"frame=\s*(\d+).*?time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_RE_MULTI = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s+BFF:\s*(\d+)\s+Progressive:\s*(\d+)\s+Undetermined:\s*(\d+)"
)


def plan_windows(
    nb_frames: int | None,
    duration_s: float,
    fps: float,
    *,
    windows: int | None = None,
    window_frames: int | None = None,
    full_scan: bool = False,
) -> list[tuple[int, float, int]]:
    """Return [(start_frame, start_s, frames)] windows for a file.

    Short files (<= IDET_FULL_SCAN_MAX_FRAMES or unknown length under 60 s) -> one
    window covering everything (frames = 0 means "no limit").  Otherwise IDET_WINDOWS
    windows of IDET_WINDOW_FRAMES evenly spaced between 2 % and 98 % of the duration.
    ``windows`` / ``window_frames`` override the defaults (and sample even short files
    when they fit); ``full_scan`` forces one window.
    """
    n = max(1, int(windows or IDET_WINDOWS))
    w = max(1, int(window_frames or IDET_WINDOW_FRAMES))
    fps = float(fps) if fps and fps > 0 else 25.0
    total = nb_frames if nb_frames and nb_frames > 0 else None
    if total is None and duration_s and duration_s > 0:
        total = int(round(duration_s * fps))
    explicit = windows is not None or window_frames is not None
    if full_scan or total is None:
        return [(0, 0.0, 0)]
    if not explicit and (
        total <= IDET_FULL_SCAN_MAX_FRAMES or (nb_frames is None and duration_s < IDET_UNKNOWN_LENGTH_FULL_SCAN_S)
    ):
        return [(0, 0.0, 0)]
    if n * w >= total:
        return [(0, 0.0, 0)]
    lo = int(round(total * SAMPLE_LO))
    hi = max(lo, int(round(total * SAMPLE_HI)) - w)
    out: list[tuple[int, float, int]] = []
    for i in range(n):
        start = lo + (hi - lo) * i // (n - 1) if n > 1 else (lo + hi) // 2
        out.append((start, round(start / fps, 3), w))
    return out


def plan_sections(total_frames: int, fps: float, sections: int = IDET_WINDOWS, min_frames: int = 50) -> list[tuple[int, float, int]]:
    """Split a short file into consecutive sections (used to separate mixed content)."""
    fps = float(fps) if fps and fps > 0 else 25.0
    n = max(2, min(int(sections), total_frames // min_frames))
    if total_frames < 2 * min_frames:
        return [(0, 0.0, 0)]
    size = total_frames // n
    out: list[tuple[int, float, int]] = []
    for i in range(n):
        start = i * size
        frames = size if i < n - 1 else total_frames - start
        out.append((start, round(start / fps, 3), frames))
    return out


def parse_idet_summary(stderr: str) -> tuple[IdetCounts, IdetCounts, RepeatCounts]:
    """(single, multi, repeated) from the LAST summary block in ffmpeg stderr."""
    reps = _RE_REPEATED.findall(stderr)
    singles = _RE_SINGLE.findall(stderr)
    multis = _RE_MULTI.findall(stderr)
    if not multis:
        raise InputError(
            ErrorCode.ANALYSIS_FAILED,
            "ffmpeg printed no idet summary",
            "The input could not be decoded; run `ffmpeg -i FILE -vf idet -f null -` to see why.",
            {"stderr": stderr_tail(stderr)},
        )
    m = [int(x) for x in multis[-1]]
    s = [int(x) for x in singles[-1]] if singles else [0, 0, 0, 0]
    r = [int(x) for x in reps[-1]] if reps else [0, 0, 0]
    return (
        IdetCounts(tff=s[0], bff=s[1], progressive=s[2], undetermined=s[3]),
        IdetCounts(tff=m[0], bff=m[1], progressive=m[2], undetermined=m[3]),
        RepeatCounts(neither=r[0], top=r[1], bottom=r[2]),
    )


def parse_ffmpeg_stats(stderr: str) -> dict[str, float] | None:
    """{'frames', 'time_s'} from ffmpeg's final ``frame=... time=HH:MM:SS.ss`` line, else None."""
    m = None
    for m in _RE_STATS.finditer(stderr):
        pass
    if not m:
        return None
    h, mi, sec = int(m.group(2)), int(m.group(3)), float(m.group(4))
    return {"frames": float(m.group(1)), "time_s": h * 3600 + mi * 60 + sec}


def idet_command(
    path: str,
    start_s: float,
    frames: int,
    *,
    fieldmatch: bool = False,
    ffmpeg: str = "ffmpeg",
    extra_filters: Sequence[str] = (),
) -> list[str]:
    """argv for one idet pass (``frames`` <= 0 means the whole file from ``start_s``)."""
    filters = list(extra_filters)
    if fieldmatch:
        filters.append(FIELDMATCH_FILTER)
    filters.append("idet")
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-loglevel", "info"]
    if start_s and start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", input_path(path), "-map", "0:V:0", "-vf", ",".join(filters)]
    if frames and frames > 0:
        cmd += ["-frames:v", str(int(frames))]
    cmd += ["-an", "-sn", "-dn", "-f", "null", "-"]
    return cmd


def run_idet_window(
    path: str,
    start_s: float,
    frames: int,
    *,
    fieldmatch: bool = False,
    ffmpeg: str = "ffmpeg",
    extra_filters: Sequence[str] = (),
    stats: dict[str, float] | None = None,
) -> tuple[IdetCounts, IdetCounts, RepeatCounts]:
    """``ffmpeg -nostats -ss START -i PATH -vf [fieldmatch=order=auto:combmatch=full,]idet -frames:v N -an -f null -``.

    With ``fieldmatch=True`` this is the telecine confirmation pass: telecined
    content comes out with (almost) no interlaced frames and no repeated fields,
    truly interlaced content stays interlaced.  ``stats`` (a dict) receives
    ffmpeg's final frame count and timeline span (see ``parse_ffmpeg_stats``) and,
    for the fieldmatch pass, ``combed``: the frames fieldmatch itself reports as
    "still interlaced" (its luma comb detector; independent of idet).
    """
    cmd = idet_command(path, start_s, frames, fieldmatch=fieldmatch, ffmpeg=ffmpeg, extra_filters=extra_filters)
    rc, _out, err = run_tool(cmd)
    if rc != 0:
        raise InputError(
            ErrorCode.ANALYSIS_FAILED,
            f"ffmpeg idet pass failed (exit {rc}) on {path}",
            "The input could not be decoded by FFmpeg.",
            {"path": path, "returncode": rc, "start_s": start_s, "stderr": stderr_tail(err)},
        )
    if stats is not None:
        stats.update(parse_ffmpeg_stats(err) or {})
        if fieldmatch:
            stats["combed"] = float(len(_RE_COMBED.findall(err)))
    return parse_idet_summary(err)


def analyze_windows(
    path: str,
    windows: list[tuple[int, float, int]],
    ffmpeg: str = "ffmpeg",
    *,
    progress: Callable[[int, int], None] | None = None,
) -> list[IdetWindow]:
    """Run the plain idet pass for every window (no classification yet).

    ``frames`` of each result is the number of frames idet actually counted
    (ffmpeg reports one more than ``-frames:v`` on seeked windows).
    """
    out: list[IdetWindow] = []
    total = len(windows)
    for i, (start_frame, start_s, frames) in enumerate(windows):
        st: dict[str, float] = {}
        single, multi, rep = run_idet_window(path, start_s, frames, ffmpeg=ffmpeg, stats=st)
        out.append(
            IdetWindow(
                start_frame=start_frame,
                start_s=start_s,
                frames=multi.total,
                single=single,
                multi=multi,
                repeated=rep,
                span_frames=int(st["frames"]) if "frames" in st else None,
                span_s=st.get("time_s"),
            )
        )
        if progress:
            progress(i + 1, total)
    return out
