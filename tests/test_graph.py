"""Graph builder: field order / preset resolution, y4m support, script text, build with the stack."""

from __future__ import annotations

import os

import pytest

from helpers_fidelity import compare_fields
from helpers_wpb import reference_analysis
from qtgmc_6ui import graph, vsenv
from qtgmc_6ui.errors import BlockedError, ErrorCode, InputError
from qtgmc_6ui.models import FieldOrder, FieldOrderChoice, MasterSettings, Preset, RateMode


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
def test_geometry_problem(vscore) -> None:
    import vapoursynth as vs

    core = vscore.core
    fmt = lambda f: core.std.BlankClip(width=16, height=16, format=f, length=1).format  # noqa: E731
    assert graph.geometry_problem(fmt(vs.YUV420P8), 720, 576) == ""
    assert graph.geometry_problem(fmt(vs.YUV422P8), 720, 486) == ""  # NTSC D1 4:2:2 works
    assert "486" in graph.geometry_problem(fmt(vs.YUV420P8), 720, 486)  # 243-line 4:2:0 fields do not exist
    assert "480 lines" in graph.geometry_problem(fmt(vs.YUV420P8), 720, 486)
    assert "287" in graph.geometry_problem(fmt(vs.YUV444P8), 352, 287)
    assert graph.geometry_problem(fmt(vs.GRAY8), 352, 288) == ""


def test_encodable_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", str(tmp_path / "cache"))
    assert graph.encodable_path("/x/ünï.avi") == "/x/ünï.avi"
    raw = bytes(tmp_path) + b"/caf\xe9 latin1.mkv"
    with open(raw, "wb") as f:
        f.write(b"x")
    p = os.fsdecode(raw)
    link = graph.encodable_path(p)
    link.encode("utf-8")
    assert os.path.islink(link) and os.readlink(link.encode()) == raw and link.endswith(".mkv")
    assert graph.encodable_path(p) == link  # idempotent


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
    assert g.color_range == "" and g.chroma_location == "topleft" and g.warnings == []  # DV: untagged range, top-left siting
    assert compare_fields(g.source[100:108], g.clip, tff=False).ok
    # post hook runs after deinterlacing
    g2 = graph.build(vscore, a, MasterSettings(preset=Preset.DRAFT, rate=RateMode.SINGLE), trim=(0, 3),
                     cachepath=str(tmp_path / "idx"), post=lambda c: c.std.Crop(left=8, right=8))
    assert g2.width == 704 and g2.frames_out == 4 and g2.fps_num == 25
    # probe dimensions that differ from the decoded clip are refused (odd width would be cropped silently)
    a.probe.video.width = 721
    with pytest.raises(InputError) as ei:
        graph.build(vscore, a, MasterSettings(preset=Preset.DRAFT), trim=(0, 3), cachepath=str(tmp_path / "idx"))
    assert ei.value.code == ErrorCode.INPUT_FORMAT_UNSUPPORTED and "721x576" in ei.value.message
    a.probe.video.width = 720
    # a container frame rate that disagrees with the decoded stream is reported (VFR)
    from fractions import Fraction

    a.probe.video.fps = Fraction(30000, 1001)
    g3 = graph.build(vscore, a, MasterSettings(preset=Preset.DRAFT), trim=(0, 3), cachepath=str(tmp_path / "idx"))
    assert g3.warnings and "frame rate" in g3.warnings[0]
