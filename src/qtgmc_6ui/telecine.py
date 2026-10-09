"""Telecine (3:2 pulldown) detection (WP-A).

Method (verified on synthetic clips, see plans/architecture.md §1.8 and the matrix in
tests/test_analysis.py):
  1. Signature: ``idet`` "Repeated Fields" ratio (top+bottom) over the frames with
     detectable content (max(determined, repeats)).  Pure 3:2 pulldown gives 0.40
     (2 repeated fields per 5 frames); true interlace and progressive give ~0.
     Static stretches count neither as repeats nor as determined, so they do not
     dilute the signature.
  2. Confirmation: rerun the suspect windows with ``fieldmatch=order=auto:combmatch=full,idet``.
     Telecined: fieldmatch reassembles the film frames and reports (almost) no frame as
     "still interlaced"; true interlace stays combed.  idet's own counts after fieldmatch
     are only a fallback: with chroma smeared across fields it still sees combing, and on
     fine-detail content it misreads progressive frames anyway.
A suspect window is never classified interlaced: either the confirmation agrees
(telecined), the signature is strong (>= 0.30: telecined with damaged fields), or the
verdict stays undetermined and the user must decide.
"""

from __future__ import annotations

from collections.abc import Callable

from .idet import run_idet_window
from .models import IdetCounts, IdetWindow, RepeatCounts

REPEAT_RATIO_SUSPECT = 0.15  # repeat ratio >= this -> run the confirmation pass, never "interlaced"
REPEAT_RATIO_STRONG = 0.30  # near the theoretical 0.40 of pure 3:2: telecined even if fieldmatch cannot clean it
MIN_REPEATED_FIELDS = 4  # absolute minimum of repeated fields before a window is suspect
FIELDMATCH_COMBED_MAX = 0.30  # share of frames fieldmatch still calls interlaced: <= this confirms telecine
FIELDMATCH_INTERLACED_MAX = 0.30  # fallback (combed count unknown): idet (tff+bff)/determined after fieldmatch


def _ratio(rep: RepeatCounts | None) -> float:
    if rep is None or rep.total <= 0:
        return 0.0
    return (rep.top + rep.bottom) / rep.total


def repeat_ratio_raw(w: IdetWindow) -> float:
    """(top+bottom)/all frames."""
    return _ratio(w.repeated)


def repeat_ratio(w: IdetWindow) -> float:
    """Repeated fields over the frames with detectable content: (top+bottom)/max(determined, top+bottom).

    0.40 for pure 3:2 pulldown; static frames (undetermined, no repeat) do not dilute it.
    """
    rep = w.repeated
    repeats = rep.top + rep.bottom
    if repeats <= 0:
        return 0.0
    den = max(w.multi.determined, repeats, 1)
    return min(1.0, repeats / den)


def repeat_ratio_after_fieldmatch(w: IdetWindow) -> float:
    return _ratio(w.repeated_after_fieldmatch)


def interlaced_share(counts: IdetCounts | None) -> float:
    """(tff+bff)/determined; 0 when nothing was determined (all undetermined = no combing)."""
    if counts is None or counts.determined <= 0:
        return 0.0
    return (counts.tff + counts.bff) / counts.determined


def combed_share(w: IdetWindow) -> float | None:
    """Share of frames fieldmatch reported as still interlaced in the confirmation pass (None if not run)."""
    if w.after_fieldmatch is None or w.combed_after_fieldmatch is None:
        return None
    n = w.after_fieldmatch.total or w.frames
    if n <= 0:
        return None
    return min(1.0, w.combed_after_fieldmatch / n)


def is_suspect(w: IdetWindow) -> bool:
    rep = w.repeated
    return rep.top + rep.bottom >= MIN_REPEATED_FIELDS and repeat_ratio(w) >= REPEAT_RATIO_SUSPECT


def is_strong(w: IdetWindow) -> bool:
    return is_suspect(w) and repeat_ratio(w) >= REPEAT_RATIO_STRONG


def confirmation_clean(w: IdetWindow) -> bool | None:
    """Did fieldmatch reassemble clean frames?  None when the confirmation pass was not run."""
    if w.after_fieldmatch is None:
        return None
    cs = combed_share(w)
    if cs is not None:
        return cs <= FIELDMATCH_COMBED_MAX
    return interlaced_share(w.after_fieldmatch) <= FIELDMATCH_INTERLACED_MAX


def confirm_windows(
    path: str,
    windows: list[IdetWindow],
    ffmpeg: str = "ffmpeg",
    *,
    progress: Callable[[int, int], None] | None = None,
) -> list[IdetWindow]:
    """Fill ``after_fieldmatch`` / ``repeated_after_fieldmatch`` / ``combed_after_fieldmatch``
    on suspect windows (in place), return them."""
    suspects = [w for w in windows if is_suspect(w)]
    for i, w in enumerate(suspects):
        st: dict[str, float] = {}
        _single, multi, rep = run_idet_window(path, w.start_s, w.frames, fieldmatch=True, ffmpeg=ffmpeg, stats=st)
        w.after_fieldmatch = multi
        w.repeated_after_fieldmatch = rep
        w.combed_after_fieldmatch = int(st["combed"]) if "combed" in st else None
        if progress:
            progress(i + 1, len(suspects))
    return suspects


def window_is_telecined(w: IdetWindow) -> bool:
    """True when the signature is present and the confirmation pass agrees (or was not run),
    or when the signature is strong (>= REPEAT_RATIO_STRONG) regardless of the confirmation."""
    if not is_suspect(w):
        return False
    clean = confirmation_clean(w)
    if clean is None or clean:
        return True
    return is_strong(w)
