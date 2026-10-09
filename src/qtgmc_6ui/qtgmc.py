"""QTGMC adapter (WP-B): maps MasterSettings to vsdeinterlace.QTempGaussMC.

Pinned implementation: vsjetpack 2.2.6 ``vsdeinterlace.qtgmc.QTempGaussMC``.
Fidelity mapping (verified by research/qtgmc-impl.md):
  field order   -> tff=True/False passed explicitly to bob()/deinterlace()
  double rate   -> .bob(clip, tff=...)
  single rate   -> .bob(clip, tff=...)[::2]   (NEVER fps_divisor=2 with default shutter angle)
  source match  -> source_match_mode=SourceMatchMode.TWICE_REFINED
  lossless      -> lossless_mode=LosslessMode.POSTSMOOTH (mode 1 is NOT lossless)
  denoise off   -> default (denoise_mode IDENTIFY, noise_restore 0)
  denoise on    -> denoise_mode=DENOISE, denoise_func=DFTTest(sigma), denoise_tr=1
                   (requires dfttest2; lossless then re-weaves DENOISED fields: warn)

Presets: QTempGaussMC has none; PRESET_TABLE holds this project's table, derived from
havsfunc's table (havsfunc/havsfunc.py @ aa79ebc9eb; the preset arrays are at lines
919-942 of that file, not 413-437 as older notes say) with the mapping in
research/qtgmc-impl.md §1:
  TR0 -> prefilter_tr, TR1 -> basic_tr, TR2 -> final_tr,
  Rep0 -> prefilter_mask_shimmer_args.erosion_distance, Rep2 -> final_mask_shimmer_args.erosion_distance,
  NNSize/NNeurons -> NNEDI3(nsize, nns) ("bobber"), BlockSize/Overlap -> analyze_blksize/analyze_overlap
  (overlap is the blksize divisor), SLRad -> sharpen_limit_radius, Sbb -> back_blend_mode (0 = sigma 0).
havsfunc columns used: placebo, very slow, slower, slow, medium, fast, faster, draft (draft's
"Bob" interpolator becomes a Catrom bob).  havsfunc rules kept: final_tr is at least 1 with
source match; sharpness limiting is left off with source match (the library default).

``build_kwargs`` returns a JSON-able description (enums as ints, the bobber and the denoiser
as small dicts) that is stored in JobResult.qtgmc_kwargs and rendered by graph.script_text;
``materialize_kwargs`` turns it into real QTempGaussMC keyword arguments.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from .errors import ComponentError, ErrorCode, InputError
from .models import ChromaMode, FieldOrder, MasterSettings, Preset, RateMode

# enum values of QTempGaussMC (vsjetpack 2.2.6), kept as ints so the table is JSON-able
SOURCE_MATCH_NONE, SOURCE_MATCH_TWICE_REFINED = 0, 3
LOSSLESS_NONE, LOSSLESS_POSTSMOOTH = 0, 2
BACK_BLEND_PRELIMIT, BACK_BLEND_POSTLIMIT, BACK_BLEND_BOTH = 0, 1, 2
DENOISE_IDENTIFY, DENOISE_DENOISE = 0, 1

HD_BLKSIZE_WIDTH = 1100  # havsfunc: block size 32 above this width


def _entry(tr0: int, tr1: int, tr2: int, rep0: int, rep2: int, bobber: Any, slrad: int, sbb: int,
           blksize: int, overlap: int) -> dict[str, Any]:
    e: dict[str, Any] = {
        "prefilter_tr": tr0,
        "basic_tr": tr1,
        "final_tr": tr2,
        "prefilter_mask_shimmer_args": {"erosion_distance": rep0},
        "final_mask_shimmer_args": {"erosion_distance": rep2},
        "bobber": bobber,
        "analyze_blksize": blksize,
        "analyze_overlap": overlap,
        "sharpen_limit_radius": slrad,
    }
    if sbb == 0:
        e["back_blend_sigma"] = 0
    else:
        e["back_blend_mode"] = {1: BACK_BLEND_PRELIMIT, 2: BACK_BLEND_POSTLIMIT, 3: BACK_BLEND_BOTH}[sbb]
    return e


def _nnedi3(nsize: int, nns: int) -> dict[str, Any]:
    return {"nnedi3": {"nsize": nsize, "nns": nns}}


#                             TR0 TR1 TR2 Rep0 Rep2  bobber          SLRad Sbb blk ovl
PRESET_TABLE: dict[Preset, dict[str, Any]] = {
    Preset.DRAFT: _entry(0, 1, 0, 0, 0, {"catrom": {}}, 0, 0, 32, 4),
    Preset.FASTER: _entry(1, 1, 0, 0, 4, _nnedi3(4, 0), 1, 0, 32, 2),
    Preset.FAST: _entry(2, 1, 0, 3, 4, _nnedi3(5, 0), 1, 0, 16, 2),
    Preset.MEDIUM: _entry(2, 1, 1, 3, 4, _nnedi3(5, 1), 1, 0, 16, 2),
    Preset.SLOW: _entry(2, 1, 1, 4, 4, _nnedi3(1, 1), 1, 0, 16, 2),
    Preset.SLOWER: _entry(2, 2, 1, 4, 4, _nnedi3(1, 1), 1, 1, 16, 2),
    Preset.VERY_SLOW: _entry(2, 2, 2, 4, 4, _nnedi3(1, 2), 1, 1, 16, 2),
    Preset.PLACEBO: _entry(2, 2, 3, 4, 4, _nnedi3(1, 2), 3, 3, 16, 2),
}
PRESET_TABLE[Preset.DRAFT]["sharpen_strength"] = 0  # havsfunc draft: SMode 0

PRESET_ORDER: tuple[Preset, ...] = tuple(PRESET_TABLE)


def default_preset(width: int, height: int) -> Preset:
    """SD (height <= 576) -> SLOWER, 720p (<= 720) -> MEDIUM, 1080 and above -> FAST."""
    if height <= 576:
        return Preset.SLOWER
    if height <= 720:
        return Preset.MEDIUM
    return Preset.FAST


def build_kwargs(settings: MasterSettings, preset: Preset, width: int, height: int) -> dict[str, Any]:
    """Final QTempGaussMC(**kwargs) as a JSON-able dict; HD uses blksize 32 like havsfunc (width > 1100)."""
    kw: dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in PRESET_TABLE[preset].items()}
    if width > HD_BLKSIZE_WIDTH:
        kw["analyze_blksize"] = max(int(kw["analyze_blksize"]), 32)
    if settings.source_match:
        kw["source_match_mode"] = SOURCE_MATCH_TWICE_REFINED
        kw["final_tr"] = max(int(kw["final_tr"]), 1)
        # havsfunc disables sharpness limiting with source match; the library default does too
        kw.pop("sharpen_limit_radius", None)
    else:
        kw["source_match_mode"] = SOURCE_MATCH_NONE
    kw["lossless_mode"] = LOSSLESS_POSTSMOOTH if settings.lossless else LOSSLESS_NONE
    if settings.denoise_sigma > 0:
        kw["denoise_mode"] = DENOISE_DENOISE
        kw["denoise_tr"] = 1
        kw["denoise_func"] = {"dfttest": {"sigma": float(settings.denoise_sigma)}}
    return kw


# QTempGaussMC.__init__ dispatches kwargs by method-name prefix in a fixed order and
# "sharpen_" is matched before "sharpen_limit_", so sharpen_limit_* must be set by calling
# the stage method after construction (see construct()).
POST_CALL_KEYS: dict[str, tuple[str, str]] = {"sharpen_limit_radius": ("sharpen_limit", "radius")}


def materialize_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """JSON description -> real QTempGaussMC keyword arguments (imports vsjetpack lazily).
    Keys in POST_CALL_KEYS are left out; ``construct()`` applies them."""
    from vsdeinterlace import QTempGaussMC

    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        if k in POST_CALL_KEYS:
            continue
        if k == "bobber":
            out["basic_bobber"] = _make_bobber(v)
        elif k == "denoise_func":
            from vsdenoise import DFTTest

            out[k] = DFTTest(**v["dfttest"])
        elif k == "source_match_mode":
            out[k] = QTempGaussMC.SourceMatchMode(v)
        elif k == "lossless_mode":
            out[k] = QTempGaussMC.LosslessMode(v)
        elif k == "back_blend_mode":
            out[k] = QTempGaussMC.BackBlendMode(v)
        elif k == "denoise_mode":
            out[k] = QTempGaussMC.NoiseProcessMode(v)
        else:
            out[k] = dict(v) if isinstance(v, dict) else v
    return out


def construct(kwargs: dict[str, Any]) -> Any:
    """QTempGaussMC from a build_kwargs() description, incl. the post-construction stage calls."""
    from vsdeinterlace import QTempGaussMC

    q = QTempGaussMC(**materialize_kwargs(kwargs))
    for key, (method, arg) in POST_CALL_KEYS.items():
        if key in kwargs:
            getattr(q, method)(**{arg: kwargs[key]})
    return q


def _make_bobber(spec: dict[str, Any]) -> Any:
    if "nnedi3" in spec:
        from vsaa import NNEDI3

        return NNEDI3(**spec["nnedi3"])
    if "catrom" in spec:
        from vskernels import Catrom

        return Catrom(**spec["catrom"])
    raise ValueError(f"unknown bobber spec {spec!r}")


def is_yuv411(fmt: Any) -> bool:
    return fmt.color_family.name == "YUV" and fmt.subsampling_w == 2 and fmt.subsampling_h == 0


def denoise_available() -> bool:
    """dfttest2 python module importable and the dfttest2_cpu plugin loaded in the open core."""
    from . import vsenv

    vc = vsenv.current()
    return bool(importlib.util.find_spec("dfttest2")) and vc is not None and vc.has("dfttest2_cpu")


def deinterlace(clip: Any, settings: MasterSettings, field_order: FieldOrder, preset: Preset) -> Any:
    """Return the deinterlaced VideoNode (double or single rate).  4:1:1 input is converted to
    4:2:2 by point resize before and back after (ChromaMode.KEEP) - bit-exact for the field lines."""
    import vapoursynth as vs

    if settings.denoise_sigma > 0 and not denoise_available():
        raise ComponentError(
            ErrorCode.DENOISE_UNAVAILABLE,
            "denoising needs the optional dfttest2 plugin and python module, which are not available",
            "run without --denoise, or install a qtgmc-6ui build that bundles dfttest2",
        )
    fmt = clip.format
    if fmt is None:
        raise InputError(ErrorCode.INPUT_FORMAT_UNSUPPORTED, "variable-format clips are not supported")
    src_fmt = fmt
    tff = field_order == FieldOrder.TFF
    # the _FieldBased prop and the explicit tff must agree (the lossless re-weave reads both)
    clip = vs.core.std.SetFieldBased(clip, 2 if tff else 1)
    if is_yuv411(fmt):
        # mvu.Super rejects 4:1:1; horizontal chroma duplication is field-safe (verified bit-exact)
        clip = vs.core.resize.Point(clip, format=fmt.replace(subsampling_w=1).id)
    kwargs = build_kwargs(settings, preset, clip.width, clip.height)
    q = construct(kwargs)
    out = q.bob(clip, tff=tff)
    if settings.rate == RateMode.SINGLE:
        out = out[::2]
    if is_yuv411(src_fmt) and settings.chroma == ChromaMode.KEEP:
        out = vs.core.resize.Point(out, format=src_fmt.id)
    return out


def fidelity_warnings(settings: MasterSettings) -> list[str]:
    """User-facing warnings: denoise breaks field preservation; lossless off; source match off."""
    w: list[str] = []
    if settings.denoise_sigma > 0:
        w.append(
            f"denoise (sigma {settings.denoise_sigma:g}) alters the image: lossless re-weaves the DENOISED fields, "
            "original pixels are no longer preserved"
        )
    if not settings.lossless:
        w.append("lossless is off: synthesised lines replace the original field lines in the output")
    if not settings.source_match:
        w.append("source match is off: lossless still preserves the original fields, source match only improves the synthesised lines")
    if settings.chroma == ChromaMode.YUV422:
        w.append("chroma 422: a 4:1:1 source is written as 4:2:2 (chroma duplicated, not resampled)")
    return w
