"""Per-window and whole-file classification (WP-A).

Per window (n = frames, det = tff+bff+prog of the MULTI counts, rep = repeat ratio):
  telecined     rep >= REPEAT_RATIO_SUSPECT and telecine.window_is_telecined()
  interlaced    rep < 0.15, det/n >= MIN_DETERMINED_SHARE, (tff+bff)/det >= INTERLACED_SHARE
                field order: tff if tff >= FIELD_ORDER_SHARE*(tff+bff), bff likewise, else None
                (a suspect window whose confirmation pass says "still interlaced" counts
                here too, judged on the after-fieldmatch counts)
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
  fewer than MIN_DETERMINED_FRAMES determined -> UNDETERMINED, CONFIRM_FIELD_ORDER with the lean as a
  frames in the whole file (tiny clips)          hint; confidence scaled down by the sample size
Confidence = share of frames supporting the verdict, in [0, 1].
"""

from __future__ import annotations

from dataclasses import dataclass

from . import telecine
from .models import (
    Classification,
    ContentClass,
    FieldOrder,
    IdetCounts,
    IdetWindow,
    ProbeInfo,
    Recommendation,
    RecommendedAction,
)

MIN_DETERMINED_SHARE = 0.20
MIN_DETERMINED_FRAMES = 10  # fewer determined frames in the whole file -> no verdict, only a lean
INTERLACED_SHARE = 0.80
PROGRESSIVE_SHARE = 0.80
FIELD_ORDER_SHARE = 0.80

IVTC_MESSAGE = (
    "This video is telecined (3:2 pulldown): it is progressive film content packed into "
    "interlaced frames. Deinterlacing it with QTGMC gives inferior results. It needs inverse "
    "telecine (IVTC) with another tool (for example FFmpeg: -vf fieldmatch,decimate). "
    "Use --force / the override only if you know the detection is wrong."
)

MIXED_MESSAGE = (
    "This video mixes telecined (3:2 pulldown) sections with truly interlaced sections. "
    "The telecined parts need inverse telecine (IVTC) with another tool, the interlaced parts "
    "need deinterlacing; one QTGMC pass over the whole file degrades the telecined parts. "
    "Split the file or process it with an IVTC-capable tool first. "
    "Use --force / the override only if you know the detection is wrong."
)

PROGRESSIVE_MESSAGE = (
    "This video looks progressive (no interlacing found in the sampled frames). "
    "Deinterlacing it is not recommended: QTGMC would only resample the image. "
    "Use --force / the override if you know it is interlaced."
)

NO_INTERLACING_WARNING = "no interlacing detected; may be progressive or static"
CONFLICT_WARNING = "field order conflicts between sampled sections; confirm tff or bff manually"
PROGRESSIVE_SECTIONS_WARNING = "some sampled sections look progressive; deinterlacing applies to all frames"
UNDETERMINED_SECTIONS_REASON = "some sampled sections are undetermined (static or very smooth content)"
TOO_FEW_FRAMES_WARNING = "too few frames to judge the content; confirm the field order manually"

_ORDER_CLASS = {FieldOrder.TFF: ContentClass.INTERLACED_TFF, FieldOrder.BFF: ContentClass.INTERLACED_BFF}


@dataclass
class WindowVerdict:
    kind: str  # telecined | interlaced | progressive | undetermined
    order: FieldOrder | None = None  # for interlaced: None when neither side reaches FIELD_ORDER_SHARE
    counts: IdetCounts | None = None  # the counts the verdict was taken from


def field_order_of(counts: IdetCounts) -> FieldOrder | None:
    inter = counts.tff + counts.bff
    if inter <= 0:
        return None
    if counts.tff >= FIELD_ORDER_SHARE * inter:
        return FieldOrder.TFF
    if counts.bff >= FIELD_ORDER_SHARE * inter:
        return FieldOrder.BFF
    return None


def _counts_verdict(c: IdetCounts) -> WindowVerdict:
    n, det = c.total, c.determined
    if n <= 0 or det / n < MIN_DETERMINED_SHARE:
        return WindowVerdict("undetermined", None, c)
    if (c.tff + c.bff) / det >= INTERLACED_SHARE:
        return WindowVerdict("interlaced", field_order_of(c), c)
    if c.progressive / det >= PROGRESSIVE_SHARE:
        return WindowVerdict("progressive", None, c)
    return WindowVerdict("undetermined", None, c)


def window_verdict(w: IdetWindow) -> WindowVerdict:
    if telecine.is_suspect(w):
        if telecine.window_is_telecined(w):
            return WindowVerdict("telecined", None, w.multi)
        # Signature present but fieldmatch says it stays interlaced: judge that pass.
        after = _counts_verdict(w.after_fieldmatch) if w.after_fieldmatch is not None else None
        if after is not None and after.kind == "interlaced":
            return after
        return WindowVerdict("undetermined", None, w.multi)
    return _counts_verdict(w.multi)


def classify_window(w: IdetWindow) -> ContentClass:
    v = window_verdict(w)
    if v.kind == "telecined":
        return ContentClass.TELECINED
    if v.kind == "interlaced":
        return _ORDER_CLASS.get(v.order, ContentClass.UNDETERMINED) if v.order else ContentClass.UNDETERMINED
    if v.kind == "progressive":
        return ContentClass.PROGRESSIVE
    return ContentClass.UNDETERMINED


def _fmt_window(w: IdetWindow, v: WindowVerdict) -> str:
    c = w.multi
    where = "whole file" if (w.start_frame == 0 and w.start_s == 0.0) else f"from frame {w.start_frame} ({w.start_s:.1f} s)"
    rep = telecine.repeat_ratio(w)
    tail = f", repeated fields {rep:.0%}" if rep > 0 else ""
    kind = v.kind
    if v.kind == "interlaced":
        kind += f" {v.order.value}" if v.order else " (order unclear)"
    return f"{where}: {c.total} frames, TFF {c.tff} BFF {c.bff} progressive {c.progressive} undetermined {c.undetermined}{tail} -> {kind}"


def classify_file(windows: list[IdetWindow], probe: ProbeInfo) -> tuple[Classification, Recommendation]:
    verdicts = [window_verdict(w) for w in windows]
    for w in windows:
        w.window_class = classify_window(w)
    reasons = [_fmt_window(w, v) for w, v in zip(windows, verdicts, strict=False)]
    cfo = probe.video.container_field_order
    reasons.append(f"container field_order '{cfo}' is reported but not used (container flags are not proof of content)")

    total = sum(w.multi.total for w in windows)
    tff = sum(w.multi.tff for w in windows)
    bff = sum(w.multi.bff for w in windows)
    prog = sum(w.multi.progressive for w in windows)
    tele_frames = sum(w.multi.total for w, v in zip(windows, verdicts, strict=False) if v.kind == "telecined")
    kinds = {v.kind for v in verdicts}
    warnings: list[str] = []

    def share(x: int) -> float:
        return round(min(1.0, max(0.0, x / total)), 4) if total > 0 else 0.0

    if not windows or total == 0:
        return (
            Classification(ContentClass.UNDETERMINED, None, 0.0, reasons + ["no frames were analysed"], [NO_INTERLACING_WARNING]),
            Recommendation(RecommendedAction.CONFIRM_FIELD_ORDER, _confirm_message(None), True),
        )

    det_total = tff + bff + prog
    if det_total < MIN_DETERMINED_FRAMES and kinds != {"undetermined"}:
        reasons.append(
            f"only {det_total} determined frame(s) of {total} analysed; at least {MIN_DETERMINED_FRAMES} are needed for a verdict"
        )
        warnings.append(TOO_FEW_FRAMES_WARNING)
        best_n = max(tff, bff, prog)
        lean = None
        if best_n > 0 and best_n in (tff, bff) and tff != bff:
            lean = FieldOrder.TFF if tff > bff else FieldOrder.BFF
        conf = round(share(best_n) * det_total / MIN_DETERMINED_FRAMES, 4)
        return (
            Classification(ContentClass.UNDETERMINED, None, conf, reasons, warnings),
            Recommendation(RecommendedAction.CONFIRM_FIELD_ORDER, _confirm_message(lean), True),
        )

    if "telecined" in kinds and "interlaced" in kinds:
        inter_frames = sum(w.multi.total for w, v in zip(windows, verdicts, strict=False) if v.kind == "interlaced")
        reasons.append(f"{tele_frames} sampled frames telecined and {inter_frames} truly interlaced")
        return (
            Classification(ContentClass.MIXED, None, share(tele_frames + inter_frames), reasons, warnings),
            Recommendation(RecommendedAction.BLOCK_MIXED, MIXED_MESSAGE, True),
        )

    if "telecined" in kinds:
        reasons.append("repeated-field pattern of 3:2 pulldown; fieldmatch removes the combing (film content)")
        return (
            Classification(ContentClass.TELECINED, None, share(tele_frames), reasons, warnings),
            Recommendation(RecommendedAction.BLOCK_TELECINED, IVTC_MESSAGE, True),
        )

    if "interlaced" in kinds:
        orders = {v.order for v in verdicts if v.kind == "interlaced"}
        if None in orders or len(orders) > 1:
            reasons.append(f"field order conflict: TFF {tff} vs BFF {bff} frames")
            warnings.append(CONFLICT_WARNING)
            best = FieldOrder.TFF if tff >= bff else FieldOrder.BFF
            return (
                Classification(ContentClass.UNDETERMINED, None, share(max(tff, bff)), reasons, warnings),
                Recommendation(RecommendedAction.CONFIRM_FIELD_ORDER, _confirm_message(best), True),
            )
        (order,) = orders
        assert order is not None
        if "progressive" in kinds:
            warnings.append(PROGRESSIVE_SECTIONS_WARNING)
        if "undetermined" in kinds:
            reasons.append(UNDETERMINED_SECTIONS_REASON)
        supporting = tff if order is FieldOrder.TFF else bff
        name = "top field first" if order is FieldOrder.TFF else "bottom field first"
        msg = (
            f"Interlaced, {name} ({supporting} of {total} sampled frames). "
            f"Deinterlace with field order {order.value.upper()}."
        )
        return (
            Classification(_ORDER_CLASS[order], order, share(supporting), reasons, warnings),
            Recommendation(RecommendedAction.DEINTERLACE, msg, True),
        )

    if "progressive" in kinds:
        if "undetermined" in kinds:
            reasons.append(UNDETERMINED_SECTIONS_REASON)
        return (
            Classification(ContentClass.PROGRESSIVE, None, share(prog), reasons, warnings),
            Recommendation(RecommendedAction.SKIP_PROGRESSIVE, PROGRESSIVE_MESSAGE, True),
        )

    warnings.append(NO_INTERLACING_WARNING)
    best = max(tff, bff, prog)
    return (
        Classification(ContentClass.UNDETERMINED, None, share(best), reasons, warnings),
        Recommendation(RecommendedAction.CONFIRM_FIELD_ORDER, _confirm_message(None), True),
    )


def _confirm_message(best: FieldOrder | None) -> str:
    hint = f" The sampled frames lean towards {best.value.upper()}." if best else ""
    return (
        "The field order could not be determined from the content."
        + hint
        + " Check the fields in the preview (show fields mode) and set the field order "
        "explicitly (--field-order tff|bff / the override) before deinterlacing."
    )
