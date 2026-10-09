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

Presets: QTempGaussMC has none; PRESET_TABLE holds this project's table.  The
owner of WP-B fills the values from havsfunc's table (research/dl/havsfunc,
havsfunc.py @ aa79ebc9eb lines 413-437) using the mapping in
research/qtgmc-impl.md §1 (TR0->prefilter_tr, TR1->basic_tr, TR2->final_tr,
Rep0/Rep2->*_mask_shimmer_args erosion_distance, NNSize/NNeurons->NNEDI3(nsize, nns),
BlockSize/Overlap->analyze_blksize/overlap, SLRad->sharpen_limit_radius,
Sbb->back_blend_mode, MatchTR2->source_match_tr).  Keys of each entry are
QTempGaussMC kwargs; "bobber" holds (nsize, nns) for NNEDI3.
"""

from __future__ import annotations

from typing import Any

from .models import FieldOrder, MasterSettings, Preset

# Values are placeholders until WP-B fills them; the keys define the contract.
PRESET_TABLE: dict[Preset, dict[str, Any]] = {
    Preset.DRAFT: {},
    Preset.FASTER: {},
    Preset.FAST: {},
    Preset.MEDIUM: {},
    Preset.SLOW: {},
    Preset.SLOWER: {},
    Preset.VERY_SLOW: {},
    Preset.PLACEBO: {},
}

PRESET_ORDER: tuple[Preset, ...] = tuple(PRESET_TABLE)


def default_preset(width: int, height: int) -> Preset:
    """SD (height <= 576) -> SLOWER, 720p (<= 720) -> MEDIUM, 1080 and above -> FAST."""
    if height <= 576:
        return Preset.SLOWER
    if height <= 720:
        return Preset.MEDIUM
    return Preset.FAST


def build_kwargs(settings: MasterSettings, preset: Preset, width: int, height: int) -> dict[str, Any]:
    """Final QTempGaussMC(**kwargs); HD uses blksize 32 like havsfunc (width > 1100)."""
    raise NotImplementedError


def deinterlace(clip: Any, settings: MasterSettings, field_order: FieldOrder, preset: Preset) -> Any:
    """Return the deinterlaced VideoNode (double or single rate).  4:1:1 input is converted to
    4:2:2 by point resize before and back after (ChromaMode.KEEP) - bit-exact for the field lines."""
    raise NotImplementedError


def fidelity_warnings(settings: MasterSettings) -> list[str]:
    """User-facing warnings: denoise breaks field preservation; lossless off; source match off."""
    raise NotImplementedError
