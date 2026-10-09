"""FFmpeg ``idet`` runner and parser (WP-A).

Parsing rule: ffmpeg prints TWO summary blocks (an all-zero block from the
probing graph first); take the LAST occurrence of each of
``Repeated Fields:``, ``Single frame detection:``, ``Multi frame detection:``.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import IdetCounts, IdetWindow, RepeatCounts

IDET_WINDOW_FRAMES = 300  # frames per sampled window
IDET_WINDOWS = 5  # windows spread over the file (skipping the first/last 2 %)
IDET_FULL_SCAN_MAX_FRAMES = 1500  # files up to this size are scanned completely in one pass


def plan_windows(nb_frames: int | None, duration_s: float, fps: float) -> list[tuple[int, float, int]]:
    """Return [(start_frame, start_s, frames)] windows for a file.

    Short files (<= IDET_FULL_SCAN_MAX_FRAMES or unknown length under 60 s) -> one
    window covering everything.  Otherwise IDET_WINDOWS windows of
    IDET_WINDOW_FRAMES evenly spaced between 2 % and 98 % of the duration.
    """
    raise NotImplementedError


def parse_idet_summary(stderr: str) -> tuple[IdetCounts, IdetCounts, RepeatCounts]:
    """(single, multi, repeated) from the LAST summary block in ffmpeg stderr."""
    raise NotImplementedError


def run_idet_window(
    path: str,
    start_s: float,
    frames: int,
    *,
    fieldmatch: bool = False,
    ffmpeg: str = "ffmpeg",
    extra_filters: Sequence[str] = (),
) -> tuple[IdetCounts, IdetCounts, RepeatCounts]:
    """``ffmpeg -nostats -ss START -i PATH -vf [fieldmatch=order=auto:combmatch=full,]idet -frames:v N -an -f null -``.

    With ``fieldmatch=True`` this is the telecine confirmation pass: telecined
    content comes out with (almost) no interlaced frames and no repeated fields,
    truly interlaced content stays interlaced.
    """
    raise NotImplementedError


def analyze_windows(path: str, windows: list[tuple[int, float, int]], ffmpeg: str = "ffmpeg") -> list[IdetWindow]:
    """Run the plain idet pass for every window (no classification yet)."""
    raise NotImplementedError
