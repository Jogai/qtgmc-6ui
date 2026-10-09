"""QTGMC adapter: preset table, kwargs mapping, fidelity of the deinterlace call."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qtgmc_6ui import qtgmc, vsenv
from qtgmc_6ui.errors import ComponentError, ErrorCode
from qtgmc_6ui.models import ChromaMode, FieldOrder, MasterSettings, Preset, RateMode

from helpers_fidelity import compare_fields

# ----------------------------------------------------------------------------- pure
def test_preset_table_complete() -> None:
    assert set(qtgmc.PRESET_TABLE) == set(Preset)
    assert len(qtgmc.PRESET_ORDER) == 8
    for p, e in qtgmc.PRESET_TABLE.items():
        for key in ("prefilter_tr", "basic_tr", "final_tr", "prefilter_mask_shimmer_args", "final_mask_shimmer_args",
                    "bobber", "analyze_blksize", "analyze_overlap"):
            assert key in e, (p, key)
        assert set(e["bobber"]) & {"nnedi3", "catrom"}


def test_preset_values_follow_havsfunc() -> None:
    t = qtgmc.PRESET_TABLE
    # havsfunc.py @ aa79ebc9eb lines 919-942: TR0 TR1 TR2 Rep0 Rep2 NNSize NNeurons
    assert (t[Preset.PLACEBO]["prefilter_tr"], t[Preset.PLACEBO]["basic_tr"], t[Preset.PLACEBO]["final_tr"]) == (2, 2, 3)
    assert (t[Preset.SLOWER]["prefilter_tr"], t[Preset.SLOWER]["basic_tr"], t[Preset.SLOWER]["final_tr"]) == (2, 2, 1)
    assert (t[Preset.MEDIUM]["basic_tr"], t[Preset.MEDIUM]["final_tr"]) == (1, 1)
    assert t[Preset.FAST]["final_tr"] == 0 and t[Preset.FASTER]["prefilter_tr"] == 1
    assert t[Preset.DRAFT]["prefilter_tr"] == 0 and t[Preset.DRAFT]["sharpen_strength"] == 0
    assert t[Preset.SLOWER]["bobber"] == {"nnedi3": {"nsize": 1, "nns": 1}}
    assert t[Preset.MEDIUM]["bobber"] == {"nnedi3": {"nsize": 5, "nns": 1}}
    assert t[Preset.PLACEBO]["bobber"] == {"nnedi3": {"nsize": 1, "nns": 2}}
    assert t[Preset.FASTER]["analyze_blksize"] == 32 and t[Preset.DRAFT]["analyze_overlap"] == 4
    assert t[Preset.PLACEBO]["prefilter_mask_shimmer_args"] == {"erosion_distance": 4}
    assert t[Preset.MEDIUM]["prefilter_mask_shimmer_args"] == {"erosion_distance": 3}
    assert t[Preset.FASTER]["prefilter_mask_shimmer_args"] == {"erosion_distance": 0}
    assert t[Preset.PLACEBO]["back_blend_mode"] == qtgmc.BACK_BLEND_BOTH and t[Preset.SLOWER]["back_blend_mode"] == qtgmc.BACK_BLEND_PRELIMIT
    assert t[Preset.SLOW]["back_blend_sigma"] == 0


def test_default_preset_by_resolution() -> None:
    assert qtgmc.default_preset(720, 576) == Preset.SLOWER
    assert qtgmc.default_preset(720, 480) == Preset.SLOWER
    assert qtgmc.default_preset(1280, 720) == Preset.MEDIUM
    assert qtgmc.default_preset(1440, 1080) == Preset.FAST
    assert qtgmc.default_preset(1920, 1080) == Preset.FAST


def test_build_kwargs_fidelity_mapping() -> None:
    kw = qtgmc.build_kwargs(MasterSettings(), Preset.SLOWER, 720, 576)
    assert kw["source_match_mode"] == 3 and kw["lossless_mode"] == 2
    assert "denoise_mode" not in kw and "denoise_func" not in kw
    assert "sharpen_limit_radius" not in kw  # library default (0) with source match, like havsfunc
    assert kw["analyze_blksize"] == 16
    # TR2 at least 1 with source match (havsfunc rule), FAST has 0 in the table
    assert qtgmc.build_kwargs(MasterSettings(), Preset.FAST, 720, 576)["final_tr"] == 1
    assert qtgmc.build_kwargs(MasterSettings(source_match=False), Preset.FAST, 720, 576)["final_tr"] == 0
    off = qtgmc.build_kwargs(MasterSettings(source_match=False, lossless=False), Preset.SLOWER, 720, 576)
    assert off["source_match_mode"] == 0 and off["lossless_mode"] == 0 and off["sharpen_limit_radius"] == 1
    # HD block size
    assert qtgmc.build_kwargs(MasterSettings(), Preset.SLOWER, 1440, 1080)["analyze_blksize"] == 32
    assert qtgmc.build_kwargs(MasterSettings(), Preset.FASTER, 1920, 1080)["analyze_blksize"] == 32
    # denoise
    dn = qtgmc.build_kwargs(MasterSettings(denoise_sigma=4), Preset.SLOWER, 720, 576)
    assert dn["denoise_mode"] == 1 and dn["denoise_func"] == {"dfttest": {"sigma": 4.0}} and dn["denoise_tr"] == 1
    # table not mutated
    assert "source_match_mode" not in qtgmc.PRESET_TABLE[Preset.SLOWER]
    import json

    json.dumps(kw)


def test_fidelity_warnings() -> None:
    assert qtgmc.fidelity_warnings(MasterSettings()) == []
    w = qtgmc.fidelity_warnings(MasterSettings(denoise_sigma=8))
    assert len(w) == 1 and "DENOISED" in w[0]
    w = qtgmc.fidelity_warnings(MasterSettings(lossless=False, source_match=False))
    assert len(w) == 2 and "synthesised" in w[0]
    assert qtgmc.fidelity_warnings(MasterSettings(chroma=ChromaMode.YUV422))


# ----------------------------------------------------------------------------- with the stack
@pytest.fixture(scope="module")
def vscore():
    pytest.importorskip("vapoursynth")
    if not os.environ.get("QTGMC6UI_PLUGIN_DIR"):
        pytest.skip("QTGMC6UI_PLUGIN_DIR not set")
    vc = vsenv.open_core()
    yield vc
    vc.close()


@pytest.fixture(scope="module")
def ref_clip(vscore, reference_clip: Path, tmp_path_factory: pytest.TempPathFactory):
    core = vscore.core
    idx = tmp_path_factory.mktemp("bsidx")
    src = core.bs.VideoSource(str(reference_clip), cachepath=str(idx))
    return core.std.SetFieldBased(src, 1)[60:76]  # 16 real BFF frames


@pytest.mark.needs_qtgmc
def test_materialize_kwargs(vscore) -> None:
    from vsdeinterlace import QTempGaussMC

    q = qtgmc.construct(qtgmc.build_kwargs(MasterSettings(), Preset.PLACEBO, 720, 576))
    assert q.source_match_mode == QTempGaussMC.SourceMatchMode.TWICE_REFINED
    assert q.lossless_mode == QTempGaussMC.LosslessMode.POSTSMOOTH
    assert q.basic_bobber.nsize == 1 and q.basic_bobber.nns == 2
    assert q.final_tr == 3 and q.backblend_mode == QTempGaussMC.BackBlendMode.BOTH
    assert q.sharpen_strength == 0 and q.sharpen_limit_radius == 0
    for p in Preset:
        for sm in (True, False):
            q = qtgmc.construct(qtgmc.build_kwargs(MasterSettings(source_match=sm), p, 720, 576))
            assert q.sharpen_limit_radius == (0 if sm else qtgmc.PRESET_TABLE[p]["sharpen_limit_radius"])


@pytest.mark.needs_qtgmc
@pytest.mark.parametrize("preset", [Preset.DRAFT, Preset.FAST, Preset.SLOWER])
def test_deinterlace_lossless_double_and_single(vscore, ref_clip, preset: Preset) -> None:
    out = qtgmc.deinterlace(ref_clip, MasterSettings(), FieldOrder.BFF, preset)
    assert out.num_frames == 2 * ref_clip.num_frames and out.fps == 50
    assert out.format.id == ref_clip.format.id
    f0 = out.get_frame(0).props
    assert f0["_FieldBased"] == 0 and f0["_SARNum"] == 16 and f0["_SARDen"] == 15
    r = compare_fields(ref_clip, out, tff=False, double_rate=True)
    assert r.ok and r.planes_compared == 3 * out.num_frames, r.details
    single = qtgmc.deinterlace(ref_clip, MasterSettings(rate=RateMode.SINGLE), FieldOrder.BFF, preset)
    assert single.num_frames == ref_clip.num_frames and single.fps == 25
    r = compare_fields(ref_clip, single, tff=False, double_rate=False)
    assert r.ok, r.details


@pytest.mark.needs_qtgmc
def test_deinterlace_tff_param_wins_over_prop(vscore, ref_clip) -> None:
    # the explicit field order is applied even if the clip prop says otherwise
    out = qtgmc.deinterlace(ref_clip, MasterSettings(), FieldOrder.TFF, Preset.DRAFT)
    assert compare_fields(ref_clip, out, tff=True).ok


@pytest.mark.needs_qtgmc
def test_lossless_off_changes_source_lines(vscore, ref_clip) -> None:
    out = qtgmc.deinterlace(ref_clip, MasterSettings(lossless=False), FieldOrder.BFF, Preset.FAST)
    r = compare_fields(ref_clip, out, tff=False)
    assert r.planes_mismatched > 0  # proves the check can fail


@pytest.mark.needs_qtgmc
def test_yuv411_wrap_is_bit_exact(vscore, ref_clip) -> None:
    import vapoursynth as vs

    c411 = vs.core.resize.Point(ref_clip, format=vs.YUV411P8)
    out = qtgmc.deinterlace(c411, MasterSettings(), FieldOrder.BFF, Preset.FAST)
    assert out.format.name == "YUV411P8"
    assert compare_fields(c411, out, tff=False).ok
    out422 = qtgmc.deinterlace(c411, MasterSettings(chroma=ChromaMode.YUV422), FieldOrder.BFF, Preset.FAST)
    assert out422.format.name == "YUV422P8"


@pytest.mark.needs_qtgmc
def test_denoise_unavailable_is_component_error(vscore, ref_clip) -> None:
    if qtgmc.denoise_available():
        pytest.skip("dfttest2 is bundled here")
    with pytest.raises(ComponentError) as ei:
        qtgmc.deinterlace(ref_clip, MasterSettings(denoise_sigma=8), FieldOrder.BFF, Preset.FAST)
    assert ei.value.code == ErrorCode.DENOISE_UNAVAILABLE
