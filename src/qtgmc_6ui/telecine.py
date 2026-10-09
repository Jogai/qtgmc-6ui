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

from .models import IdetWindow

REPEAT_RATIO_SUSPECT = 0.15  # (top+bottom)/total >= this -> run the confirmation pass
REPEAT_RATIO_STRONG = 0.30  # near the theoretical 0.40 of pure 3:2
FIELDMATCH_INTERLACED_MAX = 0.30  # after fieldmatch: (tff+bff)/determined <= this confirms telecine


def repeat_ratio(w: IdetWindow) -> float:
    raise NotImplementedError


def is_suspect(w: IdetWindow) -> bool:
    raise NotImplementedError


def confirm_windows(path: str, windows: list[IdetWindow], ffmpeg: str = "ffmpeg") -> list[IdetWindow]:
    """Fill ``after_fieldmatch`` / ``repeated_after_fieldmatch`` on suspect windows (in place), return them."""
    raise NotImplementedError


def window_is_telecined(w: IdetWindow) -> bool:
    """True when the signature is present and (if run) the confirmation pass agrees."""
    raise NotImplementedError
