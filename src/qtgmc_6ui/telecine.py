"""Telecine (3:2 pulldown) detection (WP-A).

Method (verified on synthetic clips, see plans/architecture.md §1.8):
  1. Signature: ``idet`` "Repeated Fields" ratio (top+bottom)/total per window.
     Pure 3:2 pulldown gives 0.40 (2 repeated fields per 5 frames); true
     interlace and progressive give ~0.
  2. Confirmation: rerun the suspect windows with ``fieldmatch=order=auto:combmatch=full,idet``.
     Telecined: interlaced share drops to ~0 and repeats vanish.  Interlaced:
     interlaced share stays high.
"""

from __future__ import annotations

from collections.abc import Callable

from .idet import run_idet_window
from .models import IdetCounts, IdetWindow, RepeatCounts

REPEAT_RATIO_SUSPECT = 0.15  # (top+bottom)/total >= this -> run the confirmation pass
REPEAT_RATIO_STRONG = 0.30  # near the theoretical 0.40 of pure 3:2
FIELDMATCH_INTERLACED_MAX = 0.30  # after fieldmatch: (tff+bff)/determined <= this confirms telecine


def _ratio(rep: RepeatCounts | None) -> float:
    if rep is None or rep.total <= 0:
        return 0.0
    return (rep.top + rep.bottom) / rep.total


def repeat_ratio(w: IdetWindow) -> float:
    """Share of frames with a repeated top or bottom field (0.40 for pure 3:2 pulldown)."""
    return _ratio(w.repeated)


def repeat_ratio_after_fieldmatch(w: IdetWindow) -> float:
    return _ratio(w.repeated_after_fieldmatch)


def interlaced_share(counts: IdetCounts | None) -> float:
    """(tff+bff)/determined; 0 when nothing was determined (all undetermined = no combing)."""
    if counts is None or counts.determined <= 0:
        return 0.0
    return (counts.tff + counts.bff) / counts.determined


def is_suspect(w: IdetWindow) -> bool:
    return w.repeated.total > 0 and repeat_ratio(w) >= REPEAT_RATIO_SUSPECT


def is_strong(w: IdetWindow) -> bool:
    return repeat_ratio(w) >= REPEAT_RATIO_STRONG


def confirm_windows(
    path: str,
    windows: list[IdetWindow],
    ffmpeg: str = "ffmpeg",
    *,
    progress: Callable[[int, int], None] | None = None,
) -> list[IdetWindow]:
    """Fill ``after_fieldmatch`` / ``repeated_after_fieldmatch`` on suspect windows (in place), return them."""
    suspects = [w for w in windows if is_suspect(w)]
    for i, w in enumerate(suspects):
        _single, multi, rep = run_idet_window(path, w.start_s, _request_frames(w), fieldmatch=True, ffmpeg=ffmpeg)
        w.after_fieldmatch = multi
        w.repeated_after_fieldmatch = rep
        if progress:
            progress(i + 1, len(suspects))
    return suspects


def _request_frames(w: IdetWindow) -> int:
    """Frames to request for the confirmation pass: whole file for a full-scan window
    (start 0, counted everything), else the window length."""
    return 0 if (w.start_frame == 0 and w.start_s == 0.0) else w.frames


def window_is_telecined(w: IdetWindow) -> bool:
    """True when the signature is present and (if run) the confirmation pass agrees."""
    if not is_suspect(w):
        return False
    if w.after_fieldmatch is None:
        return True
    return interlaced_share(w.after_fieldmatch) <= FIELDMATCH_INTERLACED_MAX
