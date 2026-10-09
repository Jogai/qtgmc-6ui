"""Per-window and whole-file classification (WP-A).

Per window (n = frames, det = tff+bff+prog of the MULTI counts, rep = repeat ratio):
  telecined     rep >= REPEAT_RATIO_SUSPECT and telecine.window_is_telecined()
  interlaced    rep < 0.15, det/n >= MIN_DETERMINED_SHARE, (tff+bff)/det >= INTERLACED_SHARE
                field order: tff if tff >= FIELD_ORDER_SHARE*(tff+bff), bff likewise, else None
  progressive   rep < 0.15, det/n >= MIN_DETERMINED_SHARE, prog/det >= PROGRESSIVE_SHARE
  undetermined  otherwise (mostly static/undetermined frames, or no clear majority)

Whole file:
  all windows interlaced, same order          -> INTERLACED_TFF/BFF, action DEINTERLACE
  interlaced windows with conflicting order   -> UNDETERMINED, CONFIRM_FIELD_ORDER
  any telecined window and any interlaced one -> MIXED, BLOCK_MIXED
  telecined (+ progressive/undetermined only) -> TELECINED, BLOCK_TELECINED
  progressive (+ undetermined only)           -> PROGRESSIVE, SKIP_PROGRESSIVE
  interlaced + progressive windows            -> INTERLACED_x with warning "sections look progressive"
  everything undetermined                     -> UNDETERMINED, CONFIRM_FIELD_ORDER
                                                 (warning "no interlacing detected; may be progressive")
Confidence = share of frames supporting the verdict, in [0, 1].
"""

from __future__ import annotations

from .models import Classification, ContentClass, IdetWindow, ProbeInfo, Recommendation

MIN_DETERMINED_SHARE = 0.20
INTERLACED_SHARE = 0.80
PROGRESSIVE_SHARE = 0.80
FIELD_ORDER_SHARE = 0.80

IVTC_MESSAGE = (
    "This video is telecined (3:2 pulldown): it is progressive film content packed into "
    "interlaced frames. Deinterlacing it with QTGMC gives inferior results. It needs inverse "
    "telecine (IVTC) with another tool (for example FFmpeg: -vf fieldmatch,decimate). "
    "Use --force / the override only if you know the detection is wrong."
)


def classify_window(w: IdetWindow) -> ContentClass:
    raise NotImplementedError


def classify_file(windows: list[IdetWindow], probe: ProbeInfo) -> tuple[Classification, Recommendation]:
    raise NotImplementedError
