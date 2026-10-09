"""Analysis orchestration: unit tests with a fake idet runner, cache behaviour, and
integration tests (needs_sandbox) on the reference clip and locally generated synthetic clips.

    tools/archrun --user sh -c 'cd /workspace/qtgmc/gui && .venv/bin/python -m pytest -m needs_sandbox tests/test_analysis.py'
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import REFERENCE_FRAMES

from qtgmc_6ui import analysis, idet, telecine
from qtgmc_6ui import ffprobe as ffprobe_mod
from qtgmc_6ui.errors import ErrorCode, InputError
from qtgmc_6ui.events import Emitter, Event
from qtgmc_6ui.models import (
    AnalysisResult,
    ContentClass,
    FieldOrder,
    IdetCounts,
    Preset,
    ProbeInfo,
    RecommendedAction,
    RepeatCounts,
    VideoProbe,
)


class ListSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def names(self, ev: str) -> list[str]:
        return [e.data.get("name", "") for e in self.events if e.ev == ev]


# --------------------------------------------------------------------------- unit (fake runner)
class FakeIdet:
    """Serves recorded counts per (start_s, fieldmatch) and counts calls."""

    def __init__(self, plain, after=None) -> None:
        self.plain, self.after = plain, after or plain
        self.calls: list[tuple[float, int, bool]] = []

    span: tuple[float, float] | None = None  # (frames, time_s) reported as ffmpeg stats

    def __call__(self, path, start_s, frames, *, fieldmatch=False, ffmpeg="ffmpeg", extra_filters=(), stats=None):
        self.calls.append((start_s, frames, fieldmatch))
        single, multi, rep = self.after if fieldmatch else self.plain
        if stats is not None:
            fr, t = self.span or (multi.total, multi.total / 25.0)
            stats.update({"frames": float(fr), "time_s": t})
        return single, multi, rep


REF_COUNTS = (IdetCounts(bff=274), IdetCounts(bff=274), RepeatCounts(neither=274))
TELE_COUNTS = (IdetCounts(tff=96, undetermined=24), IdetCounts(tff=119, undetermined=1), RepeatCounts(neither=72, top=24, bottom=24))
TELE_AFTER = (IdetCounts(undetermined=120), IdetCounts(undetermined=120), RepeatCounts(neither=120))


@pytest.fixture
def fake_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def install(counts, after=None, nb_frames=274, duration=10.96, fps="25/1", size=(720, 576)):
        clip = tmp_path / "clip.avi"
        clip.write_bytes(b"x" * 1024)
        fake = FakeIdet(counts, after)
        monkeypatch.setattr(idet, "run_idet_window", fake)
        monkeypatch.setattr(telecine, "run_idet_window", fake)
        info = ProbeInfo(path=str(clip), container="avi", size_bytes=1024, duration_s=duration,
                         video=VideoProbe(codec="dvvideo", width=size[0], height=size[1], fps=Fraction(fps),
                                          sar=Fraction(16, 15), nb_frames=nb_frames, nb_frames_exact=nb_frames is not None,
                                          duration_s=duration))
        monkeypatch.setattr(ffprobe_mod, "probe", lambda path, ffprobe="ffprobe": info)
        monkeypatch.setattr(ffprobe_mod, "ffmpeg_version", lambda ffmpeg="ffmpeg": "n9.0.2")
        return clip, fake

    return install


def test_analyze_reference_like(fake_tools, isolated_dirs) -> None:
    clip, fake = fake_tools(REF_COUNTS)
    sink = ListSink()
    r = analysis.analyze(str(clip), emitter=Emitter(sink))
    assert r.classification.content is ContentClass.INTERLACED_BFF
    assert r.classification.field_order is FieldOrder.BFF and r.classification.confidence == 1.0
    assert r.recommendation.action is RecommendedAction.DEINTERLACE
    assert r.sampled_all_frames and len(r.windows) == 1 and r.windows[0].frames == 274
    assert not r.fieldmatch_run and fake.calls == [(0.0, 0, False)]
    assert r.totals_multi == IdetCounts(bff=274) and r.totals_repeated.neither == 274
    assert r.frames_out_double == 548 and r.fps_out_double == Fraction(50, 1)
    assert r.colorimetry.matrix == "bt470bg" and r.colorimetry.source == "default"
    assert r.preset_default is Preset.SLOWER
    assert r.tool_versions == {"ffmpeg": "n9.0.2"} and r.took_s >= 0
    assert sink.names("stage") == ["probe", "idet", "classify"]
    prog = [e for e in sink.events if e.ev == "progress"]
    assert prog and prog[-1].data["done"] == prog[-1].data["total"] == 1 and prog[-1].data["stage"] == "idet"
    # contract round trip
    d = json.loads(json.dumps(r.to_dict()))
    assert d["schema"] == 1 and d["classification"]["content"] == "interlaced_bff"
    assert AnalysisResult.from_dict(d) == r


def test_analyze_telecined_runs_confirmation(fake_tools, isolated_dirs) -> None:
    clip, fake = fake_tools(TELE_COUNTS, TELE_AFTER, nb_frames=120, duration=4.004, fps="30000/1001", size=(720, 480))
    sink = ListSink()
    r = analysis.analyze(str(clip), emitter=Emitter(sink))
    assert r.classification.content is ContentClass.TELECINED
    assert r.recommendation.action is RecommendedAction.BLOCK_TELECINED and "IVTC" in r.recommendation.message
    assert r.fieldmatch_run and fake.calls == [(0.0, 0, False), (0.0, 120, True)]
    assert r.windows[0].after_fieldmatch == IdetCounts(undetermined=120)
    assert sink.names("stage") == ["probe", "idet", "fieldmatch", "classify"]
    assert r.colorimetry.matrix == "smpte170m" and r.fps_out_double == Fraction(60000, 1001)


def test_analyze_long_file_samples_windows(fake_tools, isolated_dirs) -> None:
    clip, fake = fake_tools(REF_COUNTS, nb_frames=90000, duration=3600.0)
    r = analysis.analyze(str(clip))
    assert len(r.windows) == idet.IDET_WINDOWS and not r.sampled_all_frames
    assert [c[1] for c in fake.calls] == [idet.IDET_WINDOW_FRAMES] * idet.IDET_WINDOWS
    assert r.windows[0].start_s > 0 and r.windows[-1].start_frame < 90000 * 0.98
    r2 = analysis.analyze(str(clip), windows=2, window_frames=100, use_cache=False)
    assert len(r2.windows) == 2 and r2.windows[0].frames == 274  # fake counts, not the request
    r3 = analysis.analyze(str(clip), full_scan=True, use_cache=False)
    assert r3.sampled_all_frames and len(r3.windows) == 1


def test_analyze_short_mixed_file_rescans_in_sections(fake_tools, isolated_dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    whole = (IdetCounts(tff=216, progressive=24), IdetCounts(tff=239, undetermined=1), RepeatCounts(neither=192, top=24, bottom=24))
    whole_after = (IdetCounts(tff=120, undetermined=120), IdetCounts(tff=120, undetermined=120), RepeatCounts(neither=240))
    tele_sec = (IdetCounts(tff=96, undetermined=24), IdetCounts(tff=119, undetermined=1), RepeatCounts(neither=72, top=24, bottom=24))
    tff_sec = (IdetCounts(tff=120), IdetCounts(tff=120), RepeatCounts(neither=120))

    class Router(FakeIdet):
        def __call__(self, path, start_s, frames, *, fieldmatch=False, ffmpeg="ffmpeg", extra_filters=(), stats=None):
            self.calls.append((start_s, frames, fieldmatch))
            if frames == 0 or frames >= 240:  # whole file (plain pass, then its confirmation)
                return whole_after if fieldmatch else whole
            if start_s == 0.0:
                return TELE_AFTER if fieldmatch else tele_sec
            return tff_sec

    clip, _ = fake_tools(whole, nb_frames=240, duration=8.008, fps="30000/1001", size=(720, 480))
    router = Router(whole)
    monkeypatch.setattr(idet, "run_idet_window", router)
    monkeypatch.setattr(telecine, "run_idet_window", router)
    sink = ListSink()
    r = analysis.analyze(str(clip), emitter=Emitter(sink))
    assert r.classification.content is ContentClass.MIXED and r.recommendation.action is RecommendedAction.BLOCK_MIXED
    assert not r.sampled_all_frames and len(r.windows) == 4 and r.fieldmatch_run
    assert router.calls[:2] == [(0.0, 0, False), (0.0, 240, True)]
    assert len(router.calls) == 7  # whole, whole+fm, 4 sections, 1 section confirmed
    assert sink.names("stage") == ["probe", "idet", "fieldmatch", "idet", "fieldmatch", "classify"]
    # a clear whole-file verdict is never re-scanned
    clip2, fake = fake_tools(REF_COUNTS)
    r = analysis.analyze(str(clip2), use_cache=False)
    assert r.sampled_all_frames and len(fake.calls) == 1


def test_analyze_warnings_emitted(fake_tools, isolated_dirs) -> None:
    clip, _ = fake_tools((IdetCounts(undetermined=96), IdetCounts(undetermined=96), RepeatCounts(neither=96)))
    sink = ListSink()
    r = analysis.analyze(str(clip), emitter=Emitter(sink))
    assert r.classification.content is ContentClass.UNDETERMINED
    warns = [e for e in sink.events if e.ev == "warning"]
    assert warns and "no interlacing" in warns[0].data["message"]


def test_analyze_missing_input(isolated_dirs, tmp_path: Path) -> None:
    with pytest.raises(InputError) as ei:
        analysis.analyze(str(tmp_path / "missing.avi"))
    assert ei.value.code == ErrorCode.INPUT_NOT_FOUND


# --------------------------------------------------------------------------- cache
def test_cache_roundtrip_and_invalidation(fake_tools, isolated_dirs) -> None:
    clip, fake = fake_tools(REF_COUNTS)
    r1 = analysis.analyze(str(clip))
    assert analysis.cache_path(str(clip)).is_file()
    assert analysis.cache_path(str(clip)).parent == isolated_dirs["cache"] / "analysis"
    sink = ListSink()
    r2 = analysis.analyze(str(clip), emitter=Emitter(sink))
    assert r2 == r1 and len(fake.calls) == 1  # served from cache, no idet run
    assert sink.names("stage") == ["probe"]
    # other scan parameters -> fresh analysis
    analysis.analyze(str(clip), windows=2)
    assert len(fake.calls) == 2
    # use_cache=False -> fresh
    analysis.analyze(str(clip), use_cache=False)
    assert len(fake.calls) == 3
    # file changed (size) -> fresh
    clip.write_bytes(b"y" * 2048)
    analysis.analyze(str(clip))
    assert len(fake.calls) == 4
    assert analysis.load_cached(str(clip)) is not None
    assert analysis.load_cached(str(clip), scan=analysis.ScanParams(full_scan=True)) is None


def test_cache_ignores_corrupt_or_foreign_files(fake_tools, isolated_dirs) -> None:
    clip, fake = fake_tools(REF_COUNTS)
    p = analysis.cache_path(str(clip))
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert analysis.load_cached(str(clip)) is None
    p.write_text(json.dumps({"schema": 99}), encoding="utf-8")
    assert analysis.load_cached(str(clip)) is None
    analysis.analyze(str(clip))
    assert len(fake.calls) == 1 and analysis.load_cached(str(clip)) is not None


def test_cache_non_utf8_file_name_roundtrips_without_tmp_leftovers(fake_tools, isolated_dirs, tmp_path: Path) -> None:
    clip, fake = fake_tools(REF_COUNTS)
    weird = os.fsdecode(os.fsencode(str(tmp_path)) + b"/caf\xe9 latin1.avi")
    assert "\udce9" in weird
    os.rename(clip, weird)
    r = analysis.analyze(weird)
    assert r.input == weird and r.classification.content is ContentClass.INTERLACED_BFF
    cache_dir = analysis.cache_path(weird).parent
    assert [f.name for f in cache_dir.iterdir() if f.suffix == ".tmp"] == []
    assert analysis.cache_path(weird).is_file()
    cached = analysis.load_cached(weird)
    assert cached is not None and cached.input == weird and cached == r
    assert len(fake.calls) == 1 and analysis.analyze(weird) == r and len(fake.calls) == 1


def test_vfr_warning_unit(fake_tools, isolated_dirs) -> None:
    clip, fake = fake_tools(REF_COUNTS)
    r = analysis.analyze(str(clip), use_cache=False)
    assert r.warnings == [] and r.windows[0].span_frames == 274 and r.windows[0].span_s == pytest.approx(10.96)
    # idet window spans more time than frames/fps -> dropped frames
    fake.span = (86.0, 4.0)
    sink = ListSink()
    r = analysis.analyze(str(clip), emitter=Emitter(sink), use_cache=False)
    assert [w.code for w in r.warnings] == [analysis.VFR_CODE]
    assert "21.50 fps" in r.warnings[0].message and r.warnings[0].hint
    assert [e.data["code"] for e in sink.events if e.ev == "warning"] == [analysis.VFR_CODE]
    d = json.loads(json.dumps(r.to_dict()))
    assert d["warnings"][0]["code"] == "variable_frame_rate" and AnalysisResult.from_dict(d) == r
    # ffprobe rates disagree
    fake.span = None
    info = ffprobe_mod.probe(str(clip))
    info.video.avg_fps = Fraction(4250, 99)
    r = analysis.analyze(str(clip), use_cache=False)
    assert r.warnings and "avg_frame_rate 4250/99" in r.warnings[0].message
    # within tolerance: nothing
    info.video.avg_fps = Fraction(25, 1)
    fake.span = (274.0, 10.9)
    assert analysis.analyze(str(clip), use_cache=False).warnings == []


def test_cache_key_non_ascii_and_unwritable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clip = tmp_path / "tést ビデオ.avi"
    clip.write_bytes(b"x")
    k = analysis.cache_key(str(clip))
    assert len(k) == 64 and k == analysis.cache_key(str(clip)) and k != analysis.cache_key(str(tmp_path / "other.avi"))
    assert analysis.cache_key(str(tmp_path / "missing.avi"))  # no stat -> still a key
    # unwritable cache dir never raises
    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", "/proc/no-such-dir/qtgmc")
    analysis.store_cached(AnalysisResult(input=str(clip)))


# --------------------------------------------------------------------------- integration
def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args], check=True)


def _gen_clip(out: Path, kind: str, size: str, rate: str, seconds: float, content: str = "mandel") -> Path:
    """Synthetic clip: smooth non-testsrc2 content (mandelbrot+rotate, or life for long clips)."""
    w, h = size.split("x")
    double = str(Fraction(rate) * 2)
    if content == "bars":  # rotating colour bars: continuous smooth motion, cheap for long clips
        src = ["-f", "lavfi", "-i", f"smptehdbars=size={size}:rate={{rate}}"]
        base = "rotate=t*0.6:fillcolor=black,format=yuv420p"
    elif content == "bars_then_static":  # motion for 4 s, then a still image (idet: undetermined)
        src = ["-f", "lavfi", "-i", f"smptehdbars=size={size}:rate={{rate}}"]
        base = "rotate='if(lt(t,4),t*0.6,2.4)':fillcolor=black,format=yuv420p"
    else:
        src = ["-f", "lavfi", "-i", f"mandelbrot=size={size}:rate={{rate}}"]
        base = "rotate=t*0.6:fillcolor=black,format=yuv420p"
    if kind in ("tff", "bff"):
        r, vf = double, f"{base},tinterlace=interleave_{'top' if kind == 'tff' else 'bottom'}"
    elif kind == "tele":
        r, vf = "24000/1001", f"{base},telecine=pattern=23"
    else:
        r, vf = rate, base
    src[-1] = src[-1].replace("{rate}", r)
    _ffmpeg(*src, "-t", str(seconds), "-vf", vf, "-c:v", "ffv1", "-an", str(out))
    return out


@pytest.fixture(scope="session")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    d = tmp_path_factory.mktemp("wpa_clips")
    out: dict[str, Path] = {}
    out["pal_tff"] = _gen_clip(d / "pal_tff.mkv", "tff", "352x288", "25/1", 4)
    out["pal_bff"] = _gen_clip(d / "pal bff ünï ビデオ.mkv", "bff", "352x288", "25/1", 4)
    out["ntsc_tff"] = _gen_clip(d / "ntsc_tff.mkv", "tff", "720x480", "30000/1001", 4)
    out["ntsc_bff"] = _gen_clip(d / "ntsc_bff.mkv", "bff", "720x480", "30000/1001", 4)
    out["ntsc_tele"] = _gen_clip(d / "ntsc_tele.mkv", "tele", "720x480", "30000/1001", 4)
    out["prog_pal"] = _gen_clip(d / "prog_pal.mkv", "prog", "352x288", "25/1", 3)
    out["prog_ntsc"] = _gen_clip(d / "prog_ntsc.mkv", "prog", "720x480", "30000/1001", 3)
    lst = d / "mix.txt"
    lst.write_text(f"file '{out['ntsc_tele']}'\nfile '{out['ntsc_tff']}'\n", encoding="utf-8")
    _ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(d / "ntsc_mixed.mkv"))
    out["ntsc_mixed"] = d / "ntsc_mixed.mkv"
    out["tiny2"] = _gen_clip(d / "tiny2.mkv", "tff", "352x288", "25/1", 0.08)  # 2 frames
    _ffmpeg("-f", "lavfi", "-i", "smptehdbars=size=352x288:rate=50", "-t", "4",
            "-vf", "rotate=t*0.6:fillcolor=black,format=yuv420p,tinterlace=interleave_top,select='not(eq(mod(n\\,7)\\,6))'",
            "-fps_mode", "passthrough", "-c:v", "ffv1", "-an", str(d / "vfr_gaps.mkv"))
    out["vfr_gaps"] = d / "vfr_gaps.mkv"
    out["long_tff"] = _gen_clip(d / "long_tff.mkv", "tff", "352x288", "25/1", 66, content="bars")
    out["long_static_tff"] = _gen_clip(d / "long_static_tff.mkv", "tff", "352x288", "25/1", 66, content="bars_then_static")
    return out


@pytest.mark.needs_sandbox
def test_reference_clip_is_bff(reference_clip: Path, isolated_dirs) -> None:
    sink = ListSink()
    r = analysis.analyze(str(reference_clip), emitter=Emitter(sink), use_cache=False)
    c = r.classification
    assert c.content is ContentClass.INTERLACED_BFF and c.field_order is FieldOrder.BFF
    assert c.confidence >= 0.95
    assert r.recommendation.action is RecommendedAction.DEINTERLACE
    assert r.probe.video.nb_frames == REFERENCE_FRAMES and r.probe.video.sar == Fraction(16, 15)
    assert r.probe.video.container_field_order == "unknown"  # DV: ffprobe does not know
    assert r.totals_multi == IdetCounts(bff=REFERENCE_FRAMES) and r.sampled_all_frames
    assert r.windows[0].frames == REFERENCE_FRAMES and not r.fieldmatch_run
    assert r.colorimetry.matrix == "bt470bg" and r.colorimetry.transfer == "bt709"
    assert r.preset_default is Preset.SLOWER and r.frames_out_double == 548
    assert r.tool_versions["ffmpeg"].startswith("n")
    assert sink.names("stage") == ["probe", "idet", "classify"]
    assert r.warnings == []  # constant frame rate
    assert r.windows[0].span_frames == REFERENCE_FRAMES and r.windows[0].span_s == pytest.approx(10.96, abs=0.05)


@pytest.mark.needs_sandbox
def test_tiny_clip_is_undetermined(clips, isolated_dirs) -> None:
    r = analysis.analyze(str(clips["tiny2"]), use_cache=False)
    assert r.totals_multi.total <= 3
    assert r.classification.content is ContentClass.UNDETERMINED and r.classification.confidence < 0.5
    assert r.recommendation.action is RecommendedAction.CONFIRM_FIELD_ORDER


@pytest.mark.needs_sandbox
def test_vfr_gapped_clip_warns(clips, isolated_dirs) -> None:
    sink = ListSink()
    r = analysis.analyze(str(clips["vfr_gaps"]), emitter=Emitter(sink), use_cache=False)
    assert r.probe.video.nb_frames and r.windows[0].span_frames and r.windows[0].span_frames < 95
    assert [w.code for w in r.warnings] == [analysis.VFR_CODE], (r.probe.video, r.windows[0])
    assert any(e.ev == "warning" and e.data["code"] == analysis.VFR_CODE for e in sink.events)
    assert r.classification.content is ContentClass.INTERLACED_TFF


@pytest.mark.needs_sandbox
@pytest.mark.parametrize(
    "name,content,order",
    [("pal_tff", ContentClass.INTERLACED_TFF, FieldOrder.TFF), ("pal_bff", ContentClass.INTERLACED_BFF, FieldOrder.BFF),
     ("ntsc_tff", ContentClass.INTERLACED_TFF, FieldOrder.TFF), ("ntsc_bff", ContentClass.INTERLACED_BFF, FieldOrder.BFF)],
)
def test_interlaced_synthetic(clips, isolated_dirs, name: str, content: ContentClass, order: FieldOrder) -> None:
    r = analysis.analyze(str(clips[name]), use_cache=False)
    assert r.classification.content is content and r.classification.field_order is order, r.classification.reasons
    assert r.classification.confidence >= 0.9
    assert r.recommendation.action is RecommendedAction.DEINTERLACE and not r.fieldmatch_run
    fam = "ntsc" if name.startswith("ntsc") else "pal"
    assert r.colorimetry.matrix == ("smpte170m" if fam == "ntsc" else "bt470bg")
    assert r.fps_out_double == (Fraction(60000, 1001) if fam == "ntsc" else Fraction(50))


@pytest.mark.needs_sandbox
def test_telecined_synthetic(clips, isolated_dirs) -> None:
    sink = ListSink()
    r = analysis.analyze(str(clips["ntsc_tele"]), emitter=Emitter(sink), use_cache=False)
    assert r.classification.content is ContentClass.TELECINED, r.classification.reasons
    assert r.recommendation.action is RecommendedAction.BLOCK_TELECINED and "IVTC" in r.recommendation.message
    assert r.fieldmatch_run and r.windows[0].after_fieldmatch is not None
    assert telecine.repeat_ratio(r.windows[0]) == pytest.approx(0.40, abs=0.05)
    assert telecine.interlaced_share(r.windows[0].after_fieldmatch) <= telecine.FIELDMATCH_INTERLACED_MAX
    assert "fieldmatch" in sink.names("stage")


@pytest.mark.needs_sandbox
def test_mixed_synthetic(clips, isolated_dirs) -> None:
    # 240 frames: the whole-file window blurs the signature (repeat ratio 0.20, fieldmatch
    # leaves half interlaced) -> analysis re-scans in sections and finds both parts.
    sink = ListSink()
    r = analysis.analyze(str(clips["ntsc_mixed"]), emitter=Emitter(sink), use_cache=False)
    assert len(r.windows) >= 2 and not r.sampled_all_frames
    assert r.classification.content is ContentClass.MIXED, r.classification.reasons
    assert r.recommendation.action is RecommendedAction.BLOCK_MIXED and "IVTC" in r.recommendation.message
    classes = {w.window_class for w in r.windows}
    assert ContentClass.TELECINED in classes and ContentClass.INTERLACED_TFF in classes
    assert sink.names("stage").count("idet") == 2
    # explicit windows are honoured on short files too
    r = analysis.analyze(str(clips["ntsc_mixed"]), use_cache=False, windows=2, window_frames=100)
    assert len(r.windows) == 2 and r.classification.content is ContentClass.MIXED
    assert [w.window_class for w in r.windows] == [ContentClass.TELECINED, ContentClass.INTERLACED_TFF]


@pytest.mark.needs_sandbox
@pytest.mark.parametrize("name", ["prog_pal", "prog_ntsc"])
def test_progressive_synthetic(clips, isolated_dirs, name: str) -> None:
    sink = ListSink()
    r = analysis.analyze(str(clips[name]), emitter=Emitter(sink), use_cache=False)
    c = r.classification
    assert c.content in (ContentClass.PROGRESSIVE, ContentClass.UNDETERMINED), c.reasons
    assert c.field_order is None and r.totals_multi.tff + r.totals_multi.bff == 0
    if c.content is ContentClass.PROGRESSIVE:
        assert r.recommendation.action is RecommendedAction.SKIP_PROGRESSIVE and c.confidence >= 0.9
    else:
        assert r.recommendation.action is RecommendedAction.CONFIRM_FIELD_ORDER and c.warnings
        assert [e for e in sink.events if e.ev == "warning"]


@pytest.mark.needs_sandbox
@pytest.mark.slow
def test_long_clip_uses_multiple_windows(clips, isolated_dirs) -> None:
    r = analysis.analyze(str(clips["long_tff"]), use_cache=False)
    assert r.probe.video.nb_frames and r.probe.video.nb_frames > idet.IDET_FULL_SCAN_MAX_FRAMES
    assert len(r.windows) == idet.IDET_WINDOWS and not r.sampled_all_frames
    assert r.windows[0].start_frame > 0 and all(w.frames >= idet.IDET_WINDOW_FRAMES for w in r.windows)
    assert r.classification.content is ContentClass.INTERLACED_TFF, r.classification.reasons
    assert r.classification.confidence >= 0.9
    r2 = analysis.analyze(str(clips["long_tff"]), use_cache=False, full_scan=True)
    assert r2.sampled_all_frames and r2.windows[0].frames == r.probe.video.nb_frames


@pytest.mark.needs_sandbox
@pytest.mark.slow
def test_long_mostly_static_clip_low_confidence(clips, isolated_dirs) -> None:
    # interlaced motion only in the first window, the rest is static -> verdict stands, low confidence
    r = analysis.analyze(str(clips["long_static_tff"]), use_cache=False)
    assert len(r.windows) == idet.IDET_WINDOWS
    assert r.classification.content is ContentClass.INTERLACED_TFF, r.classification.reasons
    assert r.classification.confidence < 0.5
    assert any("undetermined" in s for s in r.classification.reasons)


@pytest.mark.needs_sandbox
def test_non_ascii_path_end_to_end(clips, isolated_dirs) -> None:
    p = clips["pal_bff"]
    assert any(ord(ch) > 127 for ch in p.name)
    r = analysis.analyze(str(p))  # cache on: exercises the JSON path too
    assert r.classification.content is ContentClass.INTERLACED_BFF and r.input == str(p)
    cached = analysis.load_cached(str(p))
    assert cached is not None and cached.input == str(p)
    assert os.path.exists(analysis.cache_path(str(p)))
