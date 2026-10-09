"""Optional tools (x264, x265, kvazaar, sox): skipped when the tool is absent, real encodes
with frame-order and metadata checks when present; the hidden-tool behaviour runs anywhere."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from qtgmc_6ui import encoders, profiles, viewcopy
from qtgmc_6ui.errors import ComponentError, ErrorCode
from qtgmc_6ui.models import ProbeInfo, RateControl, ViewRateMode
from test_viewcopy import (
    audio_streams,
    decodes_cleanly,
    make_master,
    max_mse_vs_master,
    probe_file,
    run,
    video_stream,
)


@pytest.fixture(scope="module")
def master(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("needs ffmpeg")
    return make_master(tmp_path_factory.mktemp("master") / "clip.deint.mkv")


@pytest.fixture(scope="module")
def master_probe(master: Path) -> ProbeInfo:
    return probe_file(master)


def _check_video(out: Path, master: Path, p, probe: ProbeInfo, codec: str, frames: int = 50) -> None:  # noqa: ANN001
    assert decodes_cleanly(out)
    vs = video_stream(out)
    w, h, sar, _ = viewcopy.output_geometry(probe.video, p.video.scale)
    assert vs["codec_name"] == codec and (vs["width"], vs["height"]) == (w, h)
    assert vs["sample_aspect_ratio"] == f"{sar.numerator}:{sar.denominator}"
    assert int(vs["nb_read_packets"]) == frames
    assert (vs["color_space"], vs["color_primaries"], vs["color_transfer"], vs["color_range"]) == ("bt470bg", "bt470bg", "bt709", "tv")
    n, mse = max_mse_vs_master(out, master, p, probe)
    assert n == frames and mse < 10.0, f"frame order / content mismatch: max mse {mse}"


@pytest.mark.needs_sandbox
@pytest.mark.needs_tool("x264")
def test_x264_cli_profile(master: Path, master_probe: ProbeInfo, out_dir: Path) -> None:
    p = profiles.load("mp4-x264cli-aac")
    assert profiles.is_available(p) and profiles.validate(p) == []
    p.video.preset = "medium"  # B-frames + pyramid: exercises the display-order restoration
    of, col = run(master, master_probe, p, out_dir / "x264.mp4")
    assert of.frames == 50 and abs((of.duration_s or 0) - 1.0) < 0.05
    _check_video(Path(of.path), master, p, master_probe, "h264")
    assert video_stream(of.path)["pix_fmt"] == "yuv420p"
    assert audio_streams(of.path)[0]["codec_name"] == "aac"
    assert any("--muxer mp4" in e.data["line"] for e in col.of("log")) and not any("timestamps" in e.data["line"] for e in col.of("log"))
    last = col.of("progress")[-1].data
    assert last["done"] == last["total"] == 100  # encode + mux, one continuous count
    assert video_stream(of.path)["codec_tag_string"] == "avc1"
    assert not list(out_dir.glob(".*tmp*"))
    # 10-bit x264 into matroska, single rate, keep SAR
    p.id, p.container, p.extension = "x264-10", "matroska", "mkv"
    p.video.pix_fmt, p.video.rate, p.video.scale.mode = "yuv420p10le", ViewRateMode.SINGLE, p.video.scale.mode.__class__.KEEP
    p.audio.encoder = "flac"
    assert any("8-bit only" in x for x in profiles.validate(p, check_ffmpeg=False))  # profile high + 10 bit
    p.video.profile = "high10"
    of, _ = run(master, master_probe, p, out_dir / "x264-10.mkv")
    vs = video_stream(of.path)
    assert vs["pix_fmt"] == "yuv420p10le" and vs["r_frame_rate"] == "25/1" and vs["sample_aspect_ratio"] == "16:15"
    _check_video(Path(of.path), master, p, master_probe, "h264", frames=25)
    # 59.94 master: x264's own mp4 keeps the timing exact (no 1 ms matroska rounding)
    m = make_master(out_dir / "n.deint.mkv", fps="60000/1001", size="720x480", sar="8/9")
    p = profiles.load("mp4-x264cli-aac")
    p.video.preset = "ultrafast"
    of, _ = run(m, probe_file(m), p, out_dir / "n.mp4")
    vs = video_stream(of.path)
    assert vs["r_frame_rate"] == "60000/1001" and vs["time_base"] == "1/60000" and int(vs["nb_read_packets"]) == 60
    import subprocess

    pts = [int(x) for x in subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts", "-of", "csv=p=0", of.path], capture_output=True, text=True, check=True).stdout.split()]
    assert pts[0] == 0 and {b - a for a, b in zip(pts, pts[1:], strict=False)} == {1001}


@pytest.mark.needs_sandbox
@pytest.mark.needs_tool("x265")
def test_x265_cli_profile(master: Path, master_probe: ProbeInfo, out_dir: Path) -> None:
    p = profiles.load("mkv-x265cli10-opus")
    assert profiles.validate(p) == []
    p.video.preset = "medium"
    of, col = run(master, master_probe, p, out_dir / "x265.mkv")
    assert of.frames == 50
    _check_video(Path(of.path), master, p, master_probe, "hevc")
    assert video_stream(of.path)["pix_fmt"] == "yuv420p10le"
    assert audio_streams(of.path)[0]["codec_name"] == "opus"
    last = col.of("progress")[-1].data
    assert last["done"] == last["total"] == 150 and any(e.data["done"] == 100 for e in col.of("progress"))  # the decode pass reports too
    # mp4 target + lossless 8-bit
    p.id, p.container, p.extension = "x265-ll", "mp4", "mp4"
    p.video.pix_fmt, p.video.rate_control = "yuv420p", RateControl.LOSSLESS
    p.audio.encoder = "aac"
    of, _ = run(master, master_probe, p, out_dir / "x265-ll.mp4")
    _check_video(Path(of.path), master, p, master_probe, "hevc")
    assert video_stream(of.path)["codec_tag_string"] == "hvc1"
    n, mse = max_mse_vs_master(of.path, master, p, master_probe)
    assert mse < 1.0  # lossless (only the lanczos rescale separates it from the master)


@pytest.mark.needs_sandbox
@pytest.mark.needs_tool("x265")
@pytest.mark.slow
def test_x265_cli_cancel_removes_everything(out_dir: Path) -> None:
    import threading

    from qtgmc_6ui import events
    from qtgmc_6ui.errors import CancelledError
    from qtgmc_6ui.runtime import CancelToken

    m = make_master(out_dir / "long.deint.mkv", seconds=20)
    probe = probe_file(m)
    p = profiles.load("mkv-x265cli10-opus")
    p.video.preset = "veryslow"
    token = CancelToken()
    seen: list[int] = []

    class Trigger:
        def emit(self, ev: events.Event) -> None:
            if ev.ev == "progress":
                seen.append(ev.data["done"])
                if ev.data["done"] >= 10 and not token.cancelled:
                    threading.Thread(target=token.cancel, args=("test",)).start()

    with pytest.raises(CancelledError) as e:
        viewcopy.run_view(str(m), probe, p, str(out_dir / "long.mkv"), emitter=events.Emitter(Trigger()), cancel=token)
    assert e.value.details["removed"] == [] and not (out_dir / "long.mkv").exists()  # output never started
    assert not list(out_dir.glob(".long.mkv.tmp*")) and seen


@pytest.mark.needs_sandbox
@pytest.mark.needs_tool("kvazaar")
def test_kvazaar_profile(master: Path, master_probe: ProbeInfo, out_dir: Path) -> None:
    p = profiles.load("mkv-kvazaar-opus")
    assert profiles.validate(p) == []
    p.video.preset = "veryfast"
    of, col = run(master, master_probe, p, out_dir / "kvazaar.mkv")
    assert of.frames == 50
    _check_video(Path(of.path), master, p, master_probe, "hevc")
    assert audio_streams(of.path)[0]["codec_name"] == "opus"
    last = col.of("progress")[-1].data
    assert last["done"] == last["total"] == 150
    p.id, p.container, p.extension, p.audio.encoder = "kv-mp4", "mp4", "mp4", "aac"
    p.video.rate = ViewRateMode.SINGLE
    of, _ = run(master, master_probe, p, out_dir / "kvazaar.mp4")
    _check_video(Path(of.path), master, p, master_probe, "hevc", frames=25)
    vs = video_stream(of.path)
    assert vs["r_frame_rate"] == "25/1" and vs["codec_tag_string"] == "hvc1" and vs["time_base"] == "1/25"


@pytest.mark.needs_sandbox
@pytest.mark.needs_tool("sox")
def test_sox_audio_chain(master: Path, master_probe: ProbeInfo, out_dir: Path) -> None:
    p = profiles.load("mp4-h264-aac-sox")
    assert profiles.validate(p) == []
    p.video.preset = "ultrafast"
    p.audio.encoder = "flac"
    p.audio.sox.effects = ["rate", "-v", "44100", "gain", "-6", "dither", "-s"]
    p.container, p.extension, p.id = "matroska", "mkv", "sox-test"
    of, col = run(master, master_probe, p, out_dir / "sox.mkv")
    assert of.frames == 25
    a = audio_streams(of.path)
    assert len(a) == 1 and a[0]["codec_name"] == "flac" and a[0]["sample_rate"] == "44100" and a[0]["bits_per_raw_sample"] == "16"
    assert any("-t w64" in e.data["line"] for e in col.of("log"))
    assert abs(probe_file(of.path).audio[0].duration_s - 1.0) < 0.05
    assert any("sox a0" in e.data["line"] for e in col.of("log"))
    assert not list(out_dir.glob(".*tmp*"))
    # two tracks -> two sox passes, both mapped
    m2 = make_master(out_dir / "two.deint.mkv", audio_tracks=2)
    of, _ = run(m2, probe_file(m2), p, out_dir / "sox2.mkv")
    assert len(audio_streams(of.path)) == 2


def test_tool_profiles_refused_when_tool_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for t in ("ffmpeg", "ffprobe"):
        real = shutil.which(t)
        if real:
            os.symlink(real, bindir / t)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("QTGMC6UI_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", str(tmp_path / "cache"))
    assert {t.name for t in encoders.detect_tools() if not t.available} >= set(encoders.OPTIONAL_TOOLS)
    hidden = {"mp4-x264cli-aac", "mkv-x265cli10-opus", "mkv-kvazaar-opus", "mp4-h264-aac-sox"}
    assert not (hidden & {p.id for p in profiles.list_profiles()})
    for pid in hidden:
        p = profiles.load(pid)
        problems = profiles.validate(p, check_ffmpeg=False)
        assert any("is not installed" in x for x in problems), pid
    if shutil.which("ffmpeg"):
        m = make_master(tmp_path / "m.deint.mkv", seconds=0.2)
        with pytest.raises(ComponentError) as e:
            run(m, probe_file(m), profiles.load("mp4-x264cli-aac"), tmp_path / "o.mp4")
        assert e.value.code == ErrorCode.TOOL_MISSING and "x264" in e.value.message
