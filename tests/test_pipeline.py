"""Pipeline: blocking rules, output naming, the reference master encode, cancel, non-ASCII paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

import pytest

from helpers_fidelity import compare_files
from helpers_wpb import reference_analysis, synth_analysis
from qtgmc_6ui import pipeline, vsenv
from qtgmc_6ui.errors import BlockedError, CancelledError, ErrorCode, ExitCode, OutputError
from qtgmc_6ui.events import Emitter, Event
from qtgmc_6ui.models import (
    FieldOrder,
    FieldOrderChoice,
    JobSpec,
    MasterSettings,
    Preset,
    RateMode,
    RecommendedAction,
)
from qtgmc_6ui.runtime import CancelToken


class Recorder:
    def __init__(self, cancel: CancelToken | None = None, cancel_at: int = 0) -> None:
        self.events: list[Event] = []
        self.cancel = cancel
        self.cancel_at = cancel_at

    def emit(self, ev: Event) -> None:
        self.events.append(ev)
        if self.cancel is not None and ev.ev == "progress" and ev.data.get("done", 0) >= self.cancel_at:
            self.cancel.cancel("test")

    def of(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.ev == kind]


def stream_duration(s: dict) -> float:
    """mkv streams carry the duration as a tag (HH:MM:SS.nnn), not as a field."""
    if s.get("duration"):
        return float(s["duration"])
    t = s.get("tags", {}).get("DURATION", "")
    h, m, sec = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def ffprobe_json(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                        "-count_packets", path], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


# ----------------------------------------------------------------------------- pure
def test_check_blocked_rules() -> None:
    a = reference_analysis()
    pipeline.check_blocked(a, MasterSettings(), False)
    for action, code in ((RecommendedAction.BLOCK_TELECINED, ErrorCode.BLOCKED_TELECINED),
                         (RecommendedAction.BLOCK_MIXED, ErrorCode.BLOCKED_MIXED),
                         (RecommendedAction.SKIP_PROGRESSIVE, ErrorCode.BLOCKED_PROGRESSIVE)):
        a.recommendation.action = action
        with pytest.raises(BlockedError) as ei:
            pipeline.check_blocked(a, MasterSettings(), False)
        assert ei.value.code == code and ei.value.exit_code == ExitCode.BLOCKED
        pipeline.check_blocked(a, MasterSettings(), True)  # --force
    a.recommendation.action = RecommendedAction.CONFIRM_FIELD_ORDER
    a.classification.field_order = None
    with pytest.raises(BlockedError) as ei:
        pipeline.check_blocked(a, MasterSettings(), False)
    assert ei.value.code == ErrorCode.BLOCKED_UNDETERMINED
    with pytest.raises(BlockedError):
        pipeline.check_blocked(a, MasterSettings(), True)  # force alone is not enough
    pipeline.check_blocked(a, MasterSettings(field_order=FieldOrderChoice.TFF), False)
    a.recommendation.action = RecommendedAction.BLOCK_TELECINED
    with pytest.raises(BlockedError) as ei:
        pipeline.check_blocked(a, MasterSettings(), True)  # forced telecine without a known order
    assert ei.value.code == ErrorCode.BLOCKED_UNDETERMINED


def test_master_output_path(tmp_path: Path) -> None:
    j = JobSpec(input="/media/in/Tape 1.avi")
    assert pipeline.master_output_path(j) == "/media/in/Tape 1.deint.mkv"
    j.output_dir = str(tmp_path)
    j.name_template = "{stem}_master"
    assert pipeline.master_output_path(j) == str(tmp_path / "Tape 1_master.mkv")
    j.master_output = "/x/y.mkv"
    assert pipeline.master_output_path(j) == "/x/y.mkv"


def test_output_checks(tmp_path: Path) -> None:
    inp = tmp_path / "in.avi"
    inp.write_bytes(b"x")
    with pytest.raises(OutputError) as ei:
        pipeline._check_output(str(inp), str(inp), True)
    assert ei.value.code == ErrorCode.OUTPUT_NOT_WRITABLE
    out = tmp_path / "out.mkv"
    out.write_bytes(b"")
    with pytest.raises(OutputError) as ei:
        pipeline._check_output(str(out), str(inp), False)
    assert ei.value.code == ErrorCode.OUTPUT_EXISTS
    pipeline._check_output(str(out), str(inp), True)
    pipeline._check_output(str(tmp_path / "new" / "dir" / "o.mkv"), str(inp), False)
    assert (tmp_path / "new" / "dir").is_dir()


# ----------------------------------------------------------------------------- with the stack
@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("QTGMC6UI_CONFIG_DIR", str(tmp_path / "config"))
    assert vsenv.current() is None
    return tmp_path


@pytest.mark.needs_qtgmc
@pytest.mark.slow
def test_reference_master(env: Path, reference_clip: Path) -> None:
    from conftest import REFERENCE_AUDIO, REFERENCE_DURATION_S, REFERENCE_FRAMES_BOBBED

    before = reference_clip.stat()
    out = env / "out" / "ref.deint.mkv"
    job = JobSpec(input=str(reference_clip), master_output=str(out))
    rec = Recorder()
    t0 = time.monotonic()
    res = pipeline.run_job(job, emitter=Emitter(rec), cancel=CancelToken())
    elapsed = time.monotonic() - t0
    assert reference_clip.stat().st_mtime_ns == before.st_mtime_ns and reference_clip.stat().st_size == before.st_size
    assert vsenv.current() is None  # core closed after the run

    assert [o.kind for o in res.outputs] == ["master"] and res.outputs[0].path == str(out)
    assert res.outputs[0].frames == REFERENCE_FRAMES_BOBBED
    assert res.effective_master is not None and res.effective_master.preset == Preset.SLOWER
    assert res.effective_master.field_order == FieldOrderChoice.BFF
    assert res.qtgmc_kwargs["lossless_mode"] == 2 and res.warnings == []
    assert res.average_fps > 0

    info = ffprobe_json(str(out))
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert int(v["nb_read_packets"]) == REFERENCE_FRAMES_BOBBED
    assert v["r_frame_rate"] == "50/1" and (v["width"], v["height"]) == (720, 576)
    assert v["sample_aspect_ratio"] == "16:15" and v["display_aspect_ratio"] == "4:3"
    assert v["codec_name"] == "ffv1" and v["pix_fmt"] == "yuv420p"
    assert (v["color_space"], v["color_primaries"], v["color_transfer"], v["color_range"]) == ("bt470bg", "bt470bg", "bt709", "tv")
    assert v.get("field_order", "progressive") == "progressive"
    assert len(a) == 1 and a[0]["codec_name"] == "flac"
    assert int(a[0]["sample_rate"]) == REFERENCE_AUDIO["sample_rate"] and a[0]["channels"] == REFERENCE_AUDIO["channels"]
    assert abs(stream_duration(a[0]) - REFERENCE_DURATION_S) < 0.02
    assert abs(stream_duration(v) - REFERENCE_DURATION_S) < 0.02
    assert abs(float(info["format"]["duration"]) - REFERENCE_DURATION_S) < 0.02

    # events: stages in order, per-frame progress, done == total at the end
    assert [e.data["name"] for e in rec.of("stage")][-3:] == ["setup", "master", "verify"]  # analysis stages first
    prog = [e for e in rec.of("progress") if e.data["stage"] == "master"]
    assert prog and prog[-1].data["done"] == prog[-1].data["total"] == REFERENCE_FRAMES_BOBBED
    assert len(prog) > 10 and all(e.data["fps"] >= 0 for e in prog)
    assert any("ffmpeg command" in e.data["line"] for e in rec.of("log"))

    # fidelity: separated output fields == source fields, bit-exact, all 548 frames
    vc = vsenv.open_core()
    try:
        r = compare_files(vc, str(reference_clip), str(out), tff=False, cachepath=str(env / "idx"))
    finally:
        vc.close()
    assert r.ok and r.frames_compared == REFERENCE_FRAMES_BOBBED and r.planes_compared == 3 * REFERENCE_FRAMES_BOBBED, r.details
    enc_line = next(e.data["line"] for e in rec.of("log") if e.data["line"].startswith("encoded "))
    print(f"\nreference master: {elapsed:.1f} s total incl. analysis, {res.average_fps} fps average; {enc_line} (preset slower)")


@pytest.mark.needs_qtgmc
def test_cancel_removes_partial(env: Path, reference_clip: Path) -> None:
    out = env / "cancel.mkv"
    job = JobSpec(input=str(reference_clip), master_output=str(out), master=MasterSettings(preset=Preset.DRAFT))
    cancel = CancelToken()
    rec = Recorder(cancel, cancel_at=60)
    t0 = time.monotonic()
    with pytest.raises(CancelledError) as ei:
        pipeline.run_job(job, emitter=Emitter(rec), cancel=cancel)
    assert time.monotonic() - t0 < 60
    assert ei.value.exit_code == ExitCode.CANCELLED
    partial = pipeline.partial_path(str(out))
    assert os.path.basename(partial) == ".cancel.mkv.partial"
    assert ei.value.details["removed"] == [partial] and ei.value.details["kept"] == []
    assert not out.exists() and not os.path.exists(partial)
    assert vsenv.current() is None
    assert rec.of("progress")[-1].data["done"] >= 60


@pytest.mark.needs_qtgmc
def test_non_ascii_paths_single_rate(env: Path, reference_clip: Path) -> None:
    src_dir = env / "Quellen ✓"
    src_dir.mkdir()
    src = src_dir / "Bänd 1 – test ✓.avi"
    os.symlink(reference_clip, src)
    out = env / "Ausgabe ✓" / "Bänd 1 – test ✓.deint.mkv"
    analysis = reference_analysis(str(src))
    job = JobSpec(input=str(src), output_dir=str(out.parent), analysis=analysis,
                  master=MasterSettings(preset=Preset.DRAFT, rate=RateMode.SINGLE))
    assert pipeline.master_output_path(job) == str(out)
    res = pipeline.run_job(job, emitter=Emitter(Recorder()), cancel=CancelToken())
    assert out.is_file() and res.outputs[0].frames == 274
    v = next(s for s in ffprobe_json(str(out))["streams"] if s["codec_type"] == "video")
    assert v["r_frame_rate"] == "25/1" and int(v["nb_read_packets"]) == 274 and v["sample_aspect_ratio"] == "16:15"
    vc = vsenv.open_core()
    try:
        r = compare_files(vc, str(src), str(out), tff=False, double_rate=False, frames=range(0, 274, 7), cachepath=str(env / "idx"))
    finally:
        vc.close()
    assert r.ok, r.details
    # existing output is refused without overwrite, and the input is untouched
    with pytest.raises(OutputError) as ei:
        pipeline.run_job(job, emitter=Emitter(Recorder()), cancel=CancelToken())
    assert ei.value.code == ErrorCode.OUTPUT_EXISTS
    assert Path(os.readlink(src)) == reference_clip


@pytest.mark.needs_qtgmc
def test_blocked_job_runs_nothing(env: Path, reference_clip: Path) -> None:
    a = reference_analysis(str(reference_clip))
    a.recommendation.action = RecommendedAction.BLOCK_TELECINED
    job = JobSpec(input=str(reference_clip), master_output=str(env / "x.mkv"), analysis=a)
    with pytest.raises(BlockedError):
        pipeline.run_job(job, emitter=Emitter(Recorder()), cancel=CancelToken())
    assert not (env / "x.mkv").exists()


@pytest.mark.needs_sandbox
def test_unsupported_format_is_refused_before_encoding(env: Path, tmp_path: Path) -> None:
    # an RGB source has no y4m path: refused with a hint, nothing written
    src = tmp_path / "rgb.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "smptebars=size=64x48:rate=25", "-frames:v", "8",
                    "-vf", "tinterlace=4", "-pix_fmt", "rgb24", "-c:v", "ffv1", str(src)], check=True)
    a = reference_analysis(str(src))
    a.probe.video.width, a.probe.video.height, a.probe.video.pix_fmt, a.probe.video.sar = 64, 48, "rgb24", Fraction(1, 1)
    if not os.environ.get("QTGMC6UI_PLUGIN_DIR"):
        pytest.skip("needs plugins")
    job = JobSpec(input=str(src), master_output=str(env / "rgb.deint.mkv"), analysis=a, master=MasterSettings(preset=Preset.DRAFT))
    from qtgmc_6ui.errors import InputError

    with pytest.raises(InputError) as ei:
        pipeline.run_job(job, emitter=Emitter(Recorder()), cancel=CancelToken())
    assert ei.value.code == ErrorCode.INPUT_FORMAT_UNSUPPORTED and "RGB" in ei.value.message
    assert not (env / "rgb.deint.mkv").exists()
    shutil.rmtree(env / "cache", ignore_errors=True)


# ----------------------------------------------------------------------------- review fixes (reviews/core.md)
FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]


def _gen(out: Path, *args: str, video: str = "mandelbrot=s=352x288:r=50", audio: str | None = "sine=r=48000",
         vf: str = "tinterlace=mode=interleave_top,setfield=tff,format=yuv420p", t: str = "1",
         video_offset: str | None = None) -> Path:
    cmd = FF + (["-itsoffset", video_offset] if video_offset else []) + ["-f", "lavfi", "-i", video]
    if audio:
        cmd += ["-f", "lavfi", "-i", audio]
    cmd += ["-vf", vf, "-t", t, *args, str(out)]
    subprocess.run(cmd, check=True)
    return out


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    d = tmp_path_factory.mktemp("clips")
    return {
        "mjpeg_full": _gen(d / "mjpeg_full_tff.avi", "-c:v", "mjpeg", "-q:v", "2", "-c:a", "pcm_s16le",
                           vf="tinterlace=mode=interleave_top,setfield=tff,format=yuvj420p"),
        "avoffset": _gen(d / "avoffset.ts", "-c:v", "mpeg2video", "-q:v", "2", "-flags", "+ildct+ilme", "-c:a", "mp2", "-f", "mpegts",
                         t="2", video_offset="0.5"),
        "oddw": _gen(d / "oddw721_422_tff.mkv", "-c:v", "ffv1", audio=None, video="mandelbrot=s=721x480:r=60000/1001",
                     vf="tinterlace=mode=interleave_top,setfield=tff,format=yuv422p"),
        "odd486_420": _gen(d / "odd486_420_bff.mkv", "-c:v", "ffv1", audio=None, video="mandelbrot=s=720x486:r=60000/1001",
                           vf="tinterlace=mode=interleave_bottom,setfield=bff,format=yuv420p"),
        "odd486_422": _gen(d / "odd486_422_bff.mkv", "-c:v", "ffv1", audio=None, video="mandelbrot=s=720x486:r=60000/1001",
                           vf="tinterlace=mode=interleave_bottom,setfield=bff,format=yuv422p"),
        "vfr": _gen(d / "vfr_gaps.mkv", "-fps_mode", "passthrough", "-c:v", "ffv1", "-c:a", "pcm_s16le", t="2",
                    vf="tinterlace=mode=interleave_top,setfield=tff,format=yuv420p,select='not(eq(mod(n\\,7)\\,3))'"),
        "pal": _gen(d / "pal_tff.mkv", "-c:v", "ffv1", "-c:a", "pcm_s16le"),
    }


def _job(clip: Path, out: Path, fo: FieldOrder, **master: object) -> JobSpec:
    return JobSpec(input=str(clip), master_output=str(out), analysis=synth_analysis(str(clip), fo),
                   master=MasterSettings(preset=Preset.DRAFT, field_order=FieldOrderChoice(fo.value), **master))  # type: ignore[arg-type]


def _run(job: JobSpec) -> tuple[object, Recorder]:
    rec = Recorder()
    res = pipeline.run_job(job, emitter=Emitter(rec), cancel=CancelToken())
    return res, rec


@pytest.mark.needs_qtgmc
def test_full_range_source_keeps_pixels(env: Path, clips: dict[str, Path]) -> None:
    # B1: yuvj420p (pc range) must not be squeezed to tv; original field lines stay bit-exact
    out = env / "mjpeg.deint.mkv"
    res, rec = _run(_job(clips["mjpeg_full"], out, FieldOrder.TFF))
    v = next(s for s in ffprobe_json(str(out))["streams"] if s["codec_type"] == "video")
    assert v["pix_fmt"] == "yuv420p" and v["color_range"] == "pc" and v["chroma_location"] == "center"
    assert not any("color_range" in w for w in res.warnings)
    cmdline = next(e.data["line"] for e in rec.of("log") if "ffmpeg command" in e.data["line"])
    assert "-color_range pc" in cmdline and "range=pc" in cmdline
    vc = vsenv.open_core()
    try:
        r = compare_files(vc, str(clips["mjpeg_full"]), str(out), tff=True, cachepath=str(env / "idx"))
    finally:
        vc.close()
    assert r.ok and r.frames_compared == 50, r.details


@pytest.mark.needs_qtgmc
def test_av_offset_preserved(env: Path, clips: dict[str, Path]) -> None:
    # M1: video starting later than audio in the source keeps that offset in the master
    src = ffprobe_json(str(clips["avoffset"]))
    src_off = pipeline.source_timing(src)
    assert src_off["video_offset_s"] > 0.4 and src_off["av_offset_s"] is not None and src_off["av_offset_s"] > 0.4
    out = env / "avoffset.deint.mkv"
    res, rec = _run(_job(clips["avoffset"], out, FieldOrder.TFF))
    got = pipeline.source_timing(ffprobe_json(str(out)))
    assert abs(got["av_offset_s"] - src_off["av_offset_s"]) <= 0.005, (got, src_off)
    assert not any("A/V" in w for w in res.warnings)
    assert any("-itsoffset" in e.data["line"] for e in rec.of("log") if "ffmpeg command" in e.data["line"])


@pytest.mark.needs_sandbox
def test_odd_geometry(env: Path, clips: dict[str, Path]) -> None:
    from qtgmc_6ui.errors import InputError

    if not os.environ.get("QTGMC6UI_PLUGIN_DIR"):
        pytest.skip("needs plugins")
    # M2: odd width would be cropped by the decoder -> refused, nothing written
    with pytest.raises(InputError) as ei:
        _run(_job(clips["oddw"], env / "oddw.mkv", FieldOrder.TFF))
    assert ei.value.code == ErrorCode.INPUT_FORMAT_UNSUPPORTED and "721x480" in ei.value.message and "crop or pad" in ei.value.hint
    # M3: 486-line 4:2:0 cannot be split into fields -> coded error with a hint, no traceback
    with pytest.raises(InputError) as ei:
        _run(_job(clips["odd486_420"], env / "odd486.mkv", FieldOrder.BFF))
    assert ei.value.code == ErrorCode.INPUT_FORMAT_UNSUPPORTED and "486" in ei.value.message and "480" in ei.value.hint
    assert not (env / "oddw.mkv").exists() and not (env / "odd486.mkv").exists()
    assert not list(env.glob(".*.partial"))
    # ... but 720x486 4:2:2 (NTSC D1 capture) works, bit-exact
    out = env / "odd486_422.deint.mkv"
    res, _ = _run(_job(clips["odd486_422"], out, FieldOrder.BFF))
    v = next(s for s in ffprobe_json(str(out))["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["pix_fmt"]) == (720, 486, "yuv422p")
    vc = vsenv.open_core()
    try:
        r = compare_files(vc, str(clips["odd486_422"]), str(out), tff=False, cachepath=str(env / "idx"))
    finally:
        vc.close()
    assert r.ok, r.details


@pytest.mark.needs_qtgmc
def test_non_utf8_file_name(env: Path, clips: dict[str, Path]) -> None:
    # M4: a name that is not valid UTF-8 (latin-1 byte) is processed via an ASCII link, no traceback
    raw = bytes(env) + b"/caf\xe9 latin1.mkv"
    shutil.copy(clips["pal"], raw)
    src = os.fsdecode(raw)
    assert "\udce9" in src
    out = env / "cafe.deint.mkv"
    res, _ = _run(_job(src, out, FieldOrder.TFF))
    assert out.is_file() and res.outputs[0].frames == 50
    assert not list((env / "cache").glob("**/*.tmp"))


@pytest.mark.needs_qtgmc
def test_vfr_source_warns(env: Path, clips: dict[str, Path]) -> None:
    # M5: dropped frames (gaps) -> averaged rate is reported, not silent
    out = env / "vfr.deint.mkv"
    res, rec = _run(_job(clips["vfr"], out, FieldOrder.TFF))
    vfr = [e for e in rec.of("warning") if e.data["code"] == "vfr"]
    assert vfr and "frame rate" in vfr[0].data["message"]
    assert any("frame rate" in w for w in res.warnings)
    v = next(s for s in ffprobe_json(str(clips["vfr"]))["streams"] if s["codec_type"] == "video")
    assert int(v["nb_read_packets"]) == 43 and res.outputs[0].frames == 2 * 43  # 50 frames minus every 7th


@pytest.mark.needs_qtgmc
def test_32bit_and_float_audio(env: Path, tmp_path: Path) -> None:
    # m2: 32-bit integer PCM stays 32-bit in FLAC; float is reported as converted
    clip = tmp_path / "aud32.mkv"
    subprocess.run(FF + ["-f", "lavfi", "-i", "mandelbrot=s=352x288:r=50", "-f", "lavfi", "-i", "sine=r=96000", "-f", "lavfi", "-i", "sine=r=48000",
                         "-map", "0:v", "-map", "1:a", "-map", "2:a", "-vf", "tinterlace=mode=interleave_top,setfield=tff,format=yuv420p",
                         "-t", "1", "-c:v", "ffv1", "-c:a:0", "pcm_s32le", "-c:a:1", "pcm_f32le", str(clip)], check=True)
    out = env / "aud32.deint.mkv"
    res, rec = _run(_job(clip, out, FieldOrder.TFF))
    a = [s for s in ffprobe_json(str(out))["streams"] if s["codec_type"] == "audio"]
    assert [s["codec_name"] for s in a] == ["flac", "flac"]
    assert int(a[0]["bits_per_raw_sample"]) == 32 and int(a[0]["sample_rate"]) == 96000
    assert any("floating point" in w for w in res.warnings)
    assert any(e.data["code"] == "audio" for e in rec.of("warning"))


@pytest.mark.needs_qtgmc
def test_partial_is_hidden_and_stale_partial_is_cleaned(env: Path, clips: dict[str, Path]) -> None:
    out = env / "p" / "pal.deint.mkv"
    out.parent.mkdir()
    stale = Path(pipeline.partial_path(str(out)))
    stale.write_bytes(b"stale")
    seen: list[str] = []

    class Spy(Recorder):
        def emit(self, ev: Event) -> None:
            super().emit(ev)
            if ev.ev == "progress":
                seen.extend(p.name for p in out.parent.iterdir())

    job = _job(clips["pal"], out, FieldOrder.TFF)
    res = pipeline.run_job(job, emitter=Emitter(Spy()), cancel=CancelToken())
    assert out.is_file() and not stale.exists() and res.outputs[0].path == str(out)
    assert ".pal.deint.mkv.partial" in seen and "pal.deint.mkv" not in seen  # final name appears only after verification
    assert sorted(p.name for p in out.parent.iterdir()) == ["pal.deint.mkv"]


@pytest.mark.needs_qtgmc
def test_sigkill_leaves_no_master_and_no_ffmpeg(env: Path, reference_clip: Path) -> None:
    out = env / "killed.deint.mkv"
    partial = pipeline.partial_path(str(out))
    script = (  # noqa: UP031
        "import sys; sys.path.insert(0, %r)\n"
        "from qtgmc_6ui import pipeline\nfrom qtgmc_6ui.events import Emitter\nfrom qtgmc_6ui.runtime import CancelToken\n"
        "from qtgmc_6ui.models import JobSpec, MasterSettings, Preset\nfrom helpers_wpb import reference_analysis\n"
        "job = JobSpec(input=%r, master_output=%r, analysis=reference_analysis(%r), master=MasterSettings(preset=Preset.DRAFT))\n"
        "pipeline.run_job(job, emitter=Emitter(), cancel=CancelToken())\n"
    ) % (str(Path(__file__).parent), str(reference_clip), str(out), str(reference_clip))
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    while not os.path.exists(partial) or os.path.getsize(partial) < 200_000:
        assert child.poll() is None, "pipeline child exited early"
        assert time.monotonic() - t0 < 120
        time.sleep(0.1)
    ffmpegs = _procs_with(partial)
    assert ffmpegs, "no ffmpeg writing the partial found in /proc"
    child.kill()
    child.wait()
    deadline = time.monotonic() + 10
    while _procs_with(partial) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _procs_with(partial), "ffmpeg survived the death of the pipeline process"
    assert not out.exists()
    # the next run cleans the stale partial up and succeeds
    job = JobSpec(input=str(reference_clip), master_output=str(out), analysis=reference_analysis(str(reference_clip)),
                  master=MasterSettings(preset=Preset.DRAFT))
    pipeline.run_job(job, emitter=Emitter(Recorder()), cancel=CancelToken())
    assert out.is_file() and not os.path.exists(partial)


def _procs_with(needle: str) -> list[int]:
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if needle.encode() in f.read():
                    found.append(int(pid))
        except OSError:
            continue
    return found
