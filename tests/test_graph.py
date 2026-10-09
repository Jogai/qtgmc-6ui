"""Graph builder: field order / preset resolution, y4m support, script text, build with the stack."""

from __future__ import annotations

import os

import pytest

from qtgmc_6ui import graph, vsenv
from qtgmc_6ui.errors import BlockedError, ErrorCode, InputError
from qtgmc_6ui.models import FieldOrder, FieldOrderChoice, MasterSettings, Preset, RateMode

from helpers_fidelity import compare_fields
from helpers_wpb import reference_analysis


def test_resolve_field_order_and_preset() -> None:
    a = reference_analysis()
    assert graph.resolve_field_order(a, MasterSettings()) == FieldOrder.BFF
    assert graph.resolve_field_order(a, MasterSettings(field_order=FieldOrderChoice.TFF)) == FieldOrder.TFF
    a.classification.field_order = None
    with pytest.raises(BlockedError) as ei:
        graph.resolve_field_order(a, MasterSettings())
    assert ei.value.code == ErrorCode.BLOCKED_UNDETERMINED
    assert graph.resolve_field_order(a, MasterSettings(field_order=FieldOrderChoice.BFF)) == FieldOrder.BFF
    assert graph.resolve_preset(a, MasterSettings()) == Preset.SLOWER
    assert graph.resolve_preset(a, MasterSettings(preset=Preset.DRAFT)) == Preset.DRAFT
    a.probe.video.height = 1080
    assert graph.resolve_preset(a, MasterSettings()) == Preset.FAST


def test_script_text_is_standalone(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("QTGMC6UI_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setenv("QTGMC6UI_BUILD_INFO", str(tmp_path / "absent.json"))
    (tmp_path / "libakarin.so").write_bytes(b"")
    a = reference_analysis()
    s = graph.script_text(a, MasterSettings(rate=RateMode.SINGLE))
    assert "core.std.LoadPlugin('" in s and "libbestsource.so" in s and "libakarin.so" in s
    assert "SetFieldBased(clip, 1)" in s and "tff=False" in s
    assert "SourceMatchMode(3)" in s and "LosslessMode(2)" in s and "out[::2]" in s
    assert "NNEDI3(nsize=1, nns=1)" in s and "set_output()" in s
    compile(s, "script.vpy", "exec")
    a.classification.field_order = None
    s = graph.script_text(a, MasterSettings(preset=Preset.DRAFT))
    assert "UNDETERMINED" in s and "Catrom()" in s
    compile(s, "script.vpy", "exec")
    a.probe.video.pix_fmt = "yuv411p"
    s = graph.script_text(a, MasterSettings(field_order=FieldOrderChoice.TFF, denoise_sigma=3))
    assert "subsampling_w=1" in s and "subsampling_w=2" in s and "DFTTest(sigma=3.0)" in s and "tff=True" in s
    compile(s, "script.vpy", "exec")


@pytest.fixture(scope="module")
def vscore():
    pytest.importorskip("vapoursynth")
    if not os.environ.get("QTGMC6UI_PLUGIN_DIR"):
        pytest.skip("QTGMC6UI_PLUGIN_DIR not set")
    vc = vsenv.open_core()
    yield vc
    vc.close()


@pytest.mark.needs_sandbox
def test_y4m_support(vscore) -> None:
    import vapoursynth as vs

    core = vscore.core
    for f, ok in ((vs.YUV420P8, True), (vs.YUV422P10, True), (vs.YUV444P16, True), (vs.YUV411P8, True),
                  (vs.GRAY8, True), (vs.RGB24, False), (vs.YUV420PS, False), (vs.YUV410P8, False), (vs.YUV440P8, False)):
        c = core.std.BlankClip(width=64, height=48, format=f, length=1)
        assert (graph.y4m_support(c.format) == "") is ok, f


@pytest.mark.needs_sandbox
def test_open_source_errors(vscore, tmp_path) -> None:
    with pytest.raises(InputError) as ei:
        graph.open_source(vscore, str(tmp_path / "missing.avi"), FieldOrder.BFF, cachepath=str(tmp_path / "idx"))
    assert ei.value.code == ErrorCode.INPUT_NOT_FOUND
    bad = tmp_path / "bad.avi"
    bad.write_bytes(b"RIFF garbage" * 100)
    with pytest.raises(InputError) as ei:
        graph.open_source(vscore, str(bad), FieldOrder.BFF, cachepath=str(tmp_path / "idx"))
    assert ei.value.code == ErrorCode.INPUT_UNREADABLE


@pytest.mark.needs_qtgmc
def test_build_reference_trimmed(vscore, reference_clip, tmp_path) -> None:
    a = reference_analysis(str(reference_clip))
    g = graph.build(vscore, a, MasterSettings(preset=Preset.FAST), trim=(100, 107), cachepath=str(tmp_path / "idx"))
    assert g.field_order == FieldOrder.BFF and g.preset == Preset.FAST
    assert g.frames_out == 16 and (g.fps_num, g.fps_den) == (50, 1)
    assert g.y4m_ok and g.width == 720 and g.height == 576 and g.format_name == "YUV420P8"
    assert g.source.num_frames == 274 and g.source.get_frame(0).props["_FieldBased"] == 1
    assert g.qtgmc_kwargs["source_match_mode"] == 3
    assert compare_fields(g.source[100:108], g.clip, tff=False).ok
    # post hook runs after deinterlacing
    g2 = graph.build(vscore, a, MasterSettings(preset=Preset.DRAFT, rate=RateMode.SINGLE), trim=(0, 3),
                     cachepath=str(tmp_path / "idx"), post=lambda c: c.std.Crop(left=8, right=8))
    assert g2.width == 704 and g2.frames_out == 4 and g2.fps_num == 25
