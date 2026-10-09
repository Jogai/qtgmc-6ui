"""viewcopy: command building (pure) and real encodes of every built-in profile from a
1 s master-like ffv1/flac clip (needs_sandbox).  Helpers are reused by test_viewcopy_tools.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from fractions import Fraction
from pathlib import Path

import pytest

from qtgmc_6ui import esmux, events, profiles, viewcopy
from qtgmc_6ui.errors import CancelledError, EncodeError, ErrorCode, OutputError
from qtgmc_6ui.models import (
    AudioProbe,
    Colorimetry,
    OutputFile,
    ProbeInfo,
    Profile,
    RateControl,
    ScaleMode,
    ScaleSettings,
    VideoBackend,
    VideoProbe,
    VideoSettings,
    ViewRateMode,
)
from qtgmc_6ui.runtime import CancelToken

PAL_TAGS = Colorimetry(matrix="bt470bg", primaries="bt470bg", transfer="bt709", range="tv", source="tags")


# --------------------------------------------------------------------------- helpers
def pal_probe(**over) -> ProbeInfo:  # noqa: ANN003
    v = VideoProbe(codec="ffv1", width=720, height=576, pix_fmt="yuv420p", fps=Fraction(50, 1), sar=Fraction(16, 15), nb_frames=548, nb_frames_exact=True, duration_s=10.96, color_tags=PAL_TAGS)
    for k, val in over.items():
        setattr(v, k, val)
    return ProbeInfo(path="/m/x.deint.mkv", container="matroska,webm", duration_s=10.96, video=v, audio=[AudioProbe(index=1, codec="flac", sample_rate=48000, channels=2, bit_depth=16, duration_s=10.96)])


def make_master(path: Path, *, seconds: float = 1.0, fps: str = "50", size: str = "720x576", sar: str = "16/15", pix_fmt: str = "yuv420p", audio_tracks: int = 1) -> Path:
    """A master-like clip exactly as WP-B writes it (ffv1 level 3 + flac, SAR + colour tags)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}"]
    for _ in range(audio_tracks):
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    cmd += ["-t", str(seconds), "-map", "0:v"]
    for i in range(audio_tracks):
        cmd += ["-map", f"{i + 1}:a"]
    cmd += ["-vf", f"setsar={sar},setparams=colorspace=bt470bg:color_primaries=bt470bg:color_trc=bt709:range=tv", "-pix_fmt", pix_fmt]
    cmd += ["-threads", "2", "-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "16", "-slicecrc", "1", "-ac", "2", "-c:a", "flac", "-compression_level", "8", "-f", "matroska", str(path)]
    subprocess.run(cmd, check=True)
    return path


def probe_file(path: str | Path) -> ProbeInfo:
    """Minimal ffprobe -> ProbeInfo (WP-A's ffprobe.probe may not exist yet)."""
    d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout)
    fmt = d["format"]
    p = ProbeInfo(path=str(path), container=fmt["format_name"], size_bytes=int(fmt["size"]), duration_s=float(fmt["duration"]))
    for s in d["streams"]:
        if s["codec_type"] == "video" and not p.video.codec:
            n, dd = (s.get("sample_aspect_ratio") or "1:1").split(":")
            a, b = s["r_frame_rate"].split("/")
            p.video = VideoProbe(
                codec=s["codec_name"], width=s["width"], height=s["height"], pix_fmt=s["pix_fmt"],
                bit_depth=int(s.get("bits_per_raw_sample") or 8), fps=Fraction(int(a), int(b)), sar=Fraction(int(n), int(dd)),
                duration_s=p.duration_s, container_field_order=s.get("field_order", "unknown"),
                color_tags=Colorimetry(matrix=s.get("color_space", "unknown"), primaries=s.get("color_primaries", "unknown"), transfer=s.get("color_transfer", "unknown"), range=s.get("color_range", "tv"), source="tags"),
            )
            p.video_stream_index = s["index"]
        elif s["codec_type"] == "audio":
            p.audio.append(AudioProbe(index=s["index"], codec=s["codec_name"], sample_rate=int(s["sample_rate"]), channels=int(s["channels"]), bit_depth=int(s.get("bits_per_raw_sample") or 16), duration_s=float(s.get("duration") or p.duration_s)))
    return p


def video_stream(path: str | Path) -> dict:
    d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout)
    return d["streams"][0]


def audio_streams(path: str | Path) -> list[dict]:
    d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout)
    return d["streams"]


def decodes_cleanly(path: str | Path) -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    return r.returncode == 0 and not r.stderr.strip()


def max_mse_vs_master(output: str | Path, master: str | Path, profile: Profile, probe: ProbeInfo) -> tuple[int, float]:
    """Per-frame mse of the viewing copy against the (equally scaled/decimated) master: a wrong
    frame order shows up as mse in the hundreds, a correct encode stays in the single digits."""
    w, h, _sar, scale_f = viewcopy.output_geometry(probe.video, profile.video.scale)
    chain = []
    if profile.video.rate == ViewRateMode.SINGLE and viewcopy.is_double_rate(probe.video.fps):
        chain.append("framestep=2")
    if scale_f:
        chain.append(scale_f)
    chain.append("format=yuv420p")
    stats = Path(str(output) + ".psnr.txt")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(output), "-i", str(master), "-filter_complex", f"[0:v]format=yuv420p[a];[1:v]{','.join(chain)}[b];[a][b]psnr=stats_file={stats}", "-f", "null", "-"], check=True)
    mses = [float(line.split()[1].split(":")[1]) for line in stats.read_text().splitlines() if line.strip()]
    stats.unlink()
    return len(mses), max(mses)


class Collector:
    def __init__(self) -> None:
        self.events: list[events.Event] = []

    def emit(self, ev: events.Event) -> None:
        self.events.append(ev)

    def of(self, kind: str) -> list[events.Event]:
        return [e for e in self.events if e.ev == kind]


def run(master: Path, probe: ProbeInfo, profile: Profile, out: Path, *, overwrite: bool = False, cancel: CancelToken | None = None) -> tuple[OutputFile, Collector]:
    col = Collector()
    of = viewcopy.run_view(str(master), probe, profile, str(out), emitter=events.Emitter(col), cancel=cancel or CancelToken(), overwrite=overwrite)
    return of, col


# --------------------------------------------------------------------------- pure tests
def test_output_geometry_square_pixels() -> None:
    v = pal_probe().video
    assert viewcopy.output_geometry(v, ScaleSettings(mode=ScaleMode.SQUARE)) == (768, 576, Fraction(1, 1), "scale=768:576:flags=lanczos")
    hdv = pal_probe(width=1440, height=1080, sar=Fraction(4, 3)).video
    assert viewcopy.output_geometry(hdv, ScaleSettings(mode=ScaleMode.SQUARE))[:3] == (1920, 1080, Fraction(1, 1))
    ntsc = pal_probe(width=720, height=480, sar=Fraction(8, 9)).video
    assert viewcopy.output_geometry(ntsc, ScaleSettings(mode=ScaleMode.SQUARE))[:3] == (640, 480, Fraction(1, 1))  # always the width
    sq = pal_probe(width=1920, height=1080, sar=Fraction(1, 1)).video
    assert viewcopy.output_geometry(sq, ScaleSettings(mode=ScaleMode.SQUARE)) == (1920, 1080, Fraction(1, 1), None)
    assert viewcopy.output_geometry(v, ScaleSettings(mode=ScaleMode.KEEP)) == (720, 576, Fraction(16, 15), None)
    assert viewcopy.output_geometry(v, ScaleSettings(mode=ScaleMode.CUSTOM, width=1024, height=0, algorithm="bicubic")) == (1024, 768, Fraction(1, 1), "scale=1024:768:flags=bicubic")
    assert viewcopy.output_geometry(v, ScaleSettings(mode=ScaleMode.CUSTOM, width=0, height=288))[:2] == (384, 288)
    assert viewcopy.output_geometry(v, ScaleSettings(mode=ScaleMode.CUSTOM, width=640, height=480))[:2] == (640, 480)


def test_setparams_and_rate_helpers() -> None:
    assert viewcopy.setparams_filter(PAL_TAGS) == "setparams=colorspace=bt470bg:color_primaries=bt470bg:color_trc=bt709:range=tv"
    assert viewcopy.setparams_filter(Colorimetry(matrix="bt709", range="pc")) == "setparams=colorspace=bt709:range=pc"
    assert viewcopy.setparams_filter(Colorimetry(matrix="unknown", primaries="", transfer="unspecified", range="")) is None
    assert viewcopy.is_double_rate(Fraction(50, 1)) and viewcopy.is_double_rate(Fraction(60000, 1001))
    assert not viewcopy.is_double_rate(Fraction(25, 1)) and not viewcopy.is_double_rate(Fraction(30000, 1001))
    assert viewcopy.bit_depth("yuv420p10le") == 10 and viewcopy.bit_depth("yuv420p") == 8 and viewcopy.bit_depth("gray16be") == 16
    assert viewcopy.snap_fps(Fraction(19001, 317)) == Fraction(60000, 1001)  # matroska 1 ms rounding of 59.94
    assert viewcopy.snap_fps(Fraction(50, 1)) == Fraction(50) and viewcopy.snap_fps(Fraction(24000, 1001)) == Fraction(24000, 1001)
    assert viewcopy.snap_fps(Fraction(33, 1)) == Fraction(33, 1)
    assert viewcopy.tool_pix_fmt(VideoBackend.X264_CLI, "yuv411p") == "yuv422p"
    assert viewcopy.tool_pix_fmt(VideoBackend.KVAZAAR_CLI, "yuv422p10le") == "yuv420p"
    assert viewcopy.tool_pix_fmt(VideoBackend.X264_CLI, "yuv420p12le") == "yuv420p10le"
    assert viewcopy.tool_pix_fmt(VideoBackend.X265_CLI, "yuv444p12le") == "yuv444p12le"
    assert viewcopy.tool_pix_fmt(VideoBackend.X265_CLI, "gray16le") == "gray12le"
    assert viewcopy.tool_pix_fmt(VideoBackend.X264_CLI, "yuv420p") == "yuv420p"


def test_view_output_path() -> None:
    from qtgmc_6ui import jobs
    from qtgmc_6ui.errors import UsageError
    from qtgmc_6ui.models import JobSpec, ViewCopySpec

    p = profiles.load("mp4-h264-aac")
    f = viewcopy.view_output_path
    assert f("/in/clip.avi", p, "{stem}.deint", "/out") == "/out/clip.deint.mp4-h264-aac.mp4"
    assert f("/in/clip.avi", p, "x{stem}", "/out") == "/out/xclip.mp4-h264-aac.mp4"  # no xxclip
    assert f("/in/clip.avi", p, "{name}.deint", "/out") == "/out/clip.avi.deint.mp4-h264-aac.mp4"
    assert f("/in/clip.avi", p, "{profile}-{stem}", "/out") == "/out/mp4-h264-aac-clip.mp4"  # id not appended twice
    assert f("/in/my clip.avi", p, "fixed-name", "/out") == "/out/fixed-name.mp4-h264-aac.mp4"
    for bad in ("{nope}", "a/{stem}", "{}", ""):
        with pytest.raises(UsageError):
            f("/in/clip.avi", p, bad, "/out")
    # jobs.view_path is a thin wrapper: collision/overwrite checks see the path that is written
    job = JobSpec(input="/in/clip.avi", output_dir="/out", name_template="x{stem}", views=[ViewCopySpec("mp4-h264-aac")])
    assert jobs.view_path(job, job.views[0], p) == f("/in/clip.avi", p, "x{stem}", "/out")
    assert jobs.plan_outputs(job, [p])[1] == "/out/xclip.mp4-h264-aac.mp4"


def test_rate_control_args() -> None:
    vs = VideoSettings(encoder="libx264", rate_control=RateControl.CRF, quality=18)
    assert viewcopy.rate_control_args(vs, None) == ["-crf", "18"]
    vs = VideoSettings(encoder="libvpx-vp9", rate_control=RateControl.CRF, quality=31)
    assert viewcopy.rate_control_args(vs, None) == ["-crf", "31", "-b:v", "0"]
    vs = VideoSettings(encoder="libx264", rate_control=RateControl.BITRATE, bitrate_kbps=4000)
    assert viewcopy.rate_control_args(vs, None) == ["-b:v", "4000k"]
    vs = VideoSettings(encoder="libx265", rate_control=RateControl.LOSSLESS)
    assert viewcopy.rate_control_args(vs, None) == ["-x265-params", "lossless=1"]
    vs = VideoSettings(encoder="libx264", rate_control=RateControl.QP, quality=20)
    assert viewcopy.rate_control_args(vs, None) == ["-q:v", "20"]  # without encoder details the generic flag


def test_build_commands_ffmpeg_backend() -> None:
    p = profiles.load("mp4-h264-aac")
    cmds = viewcopy.build_commands("/m/x.deint.mkv", pal_probe(), p, "/o/x.mp4")
    assert len(cmds) == 1
    c = cmds[0]
    assert c[0] == "ffmpeg" and c[-3:] == ["-f", "mp4", "/o/x.mp4"] and "-nostdin" in c
    assert c[c.index("-i") + 1] == "/m/x.deint.mkv"  # reads the MASTER
    vf = c[c.index("-vf") + 1]
    assert vf == "scale=768:576:flags=lanczos,setpts=N/(50/1)/TB,setsar=1/1,setparams=colorspace=bt470bg:color_primaries=bt470bg:color_trc=bt709:range=tv"
    assert c[c.index("-r") + 1] == "50/1" and c[c.index("-video_track_timescale") + 1] == "50"
    assert c[c.index("-c:v") + 1] == "libx264" and c[c.index("-crf") + 1] == "18" and c[c.index("-preset") + 1] == "slow"
    assert c[c.index("-profile:v") + 1] == "high" and c[c.index("-pix_fmt") + 1] == "yuv420p"
    assert c[c.index("-c:a") + 1] == "aac" and c[c.index("-b:a") + 1] == "192k"
    assert ["-map", "0:v:0", "-map", "0:a?"] == c[c.index("-map") : c.index("-map") + 4]
    # keep SAR + flac quality + no scaling
    p = profiles.load("mkv-h264-flac")
    c = viewcopy.build_commands("/m/x.deint.mkv", pal_probe(), p, "/o/x.mkv")[0]
    assert c[c.index("-vf") + 1].startswith("setpts=N/(50/1)/TB,setsar=16/15,setparams=")
    assert c[c.index("-compression_level") + 1] == "8" and "-pix_fmt" not in c and "-video_track_timescale" not in c
    # single rate + custom extra args
    p.video.rate = ViewRateMode.SINGLE
    p.video.extra_args = ["-x264-params", "ref=3"]
    c = viewcopy.build_commands("/m/x.deint.mkv", pal_probe(), p, "/o/x.mkv")[0]
    assert c[c.index("-vf") + 1].startswith("framestep=2,setpts=N/(25/1)/TB,setsar=16/15") and c[c.index("-r") + 1] == "25/1"
    assert c[c.index("-x264-params") + 1] == "ref=3"
    # a single-rate master is not decimated again
    c = viewcopy.build_commands("/m/x.mkv", pal_probe(fps=Fraction(25, 1)), p, "/o/x.mkv")[0]
    assert "framestep" not in c[c.index("-vf") + 1]
    plan = viewcopy.build_plan("/m/x.mkv", pal_probe(), p, "/o/x.mkv")
    assert plan.total_frames == 274 and plan.fps == Fraction(25, 1) and plan.tools == ["ffmpeg"]
    # hevc in mp4 gets the hvc1 tag; a 59.94 master (probed as 19001/317 from mkv) is snapped
    p = profiles.load("mkv-hevc10-opus")
    p.container, p.extension = "mp4", "mp4"
    c = viewcopy.build_commands("/m/x.mkv", pal_probe(fps=Fraction(19001, 317), width=720, height=480, sar=Fraction(8, 9)), p, "/o/x.mp4")[0]
    assert c[c.index("-tag:v") + 1] == "hvc1" and c[c.index("-r") + 1] == "60000/1001" and c[c.index("-video_track_timescale") + 1] == "60000"
    assert "scale=640:480:flags=lanczos,setpts=N/(60000/1001)/TB" in c[c.index("-vf") + 1]


def test_build_commands_cli_backends() -> None:
    probe = pal_probe()
    x264 = profiles.load("mp4-x264cli-aac")
    cmds = viewcopy.build_commands("/m/x.deint.mkv", probe, x264, "/o/x.mp4")
    assert [c[0] for c in cmds] == ["ffmpeg", "x264", "ffmpeg"]
    feeder, enc, mux = cmds
    assert feeder[feeder.index("-f") + 1] == "yuv4mpegpipe" and feeder[-1] == "-" and "-strict" in feeder and "-an" in feeder
    assert feeder[feeder.index("-vf") + 1] == "scale=768:576:flags=lanczos,setpts=N/(50/1)/TB" and feeder[feeder.index("-r") + 1] == "50/1"
    assert enc[:5] == ["x264", "--demuxer", "y4m", "--muxer", "mp4"] and enc[-1] == "-" and enc[enc.index("--crf") + 1] == "18"
    assert enc[enc.index("--sar") + 1] == "1:1" and enc[enc.index("--colorprim") + 1] == "bt470bg" and enc[enc.index("--transfer") + 1] == "bt709"
    assert enc[enc.index("--colormatrix") + 1] == "bt470bg" and enc[enc.index("--range") + 1] == "tv" and enc[enc.index("--profile") + 1] == "high"
    assert enc[enc.index("--output-depth") + 1] == "8"
    plan = viewcopy.build_plan("/m/x.deint.mkv", probe, x264, "/o/x.mp4")
    assert [s.label for s in plan.steps] == ["encode (x264)", "mux"] and plan.tools == ["ffmpeg", "x264"]  # no esmux for x264
    assert len(plan.temps) == 1 and plan.temps[0].endswith(".mp4") and enc[enc.index("-o") + 1] == plan.temps[0]
    assert mux[mux.index("-i") + 1] == plan.temps[0] and mux[mux.index("-c:v") + 1] == "copy" and mux[-3:] == ["-f", "mp4", "/o/x.mp4"]
    assert "/m/x.deint.mkv" in mux and ["-map", "0:v:0", "-map", "1:a?"] == mux[mux.index("-map") : mux.index("-map") + 4]
    assert mux[mux.index("-video_track_timescale") + 1] == "50" and "-tag:v" not in mux
    assert plan.total_frames == 548 * 2 and plan.steps[1].progress_offset == 548  # continuous progress over both steps

    x265 = profiles.load("mkv-x265cli10-opus")
    feeder, enc, mux = viewcopy.build_commands("/m/x.deint.mkv", probe, x265, "/o/x.mkv")
    plan = viewcopy.build_plan("/m/x.deint.mkv", probe, x265, "/o/x.mkv")
    assert [s.label for s in plan.steps] == ["encode (x265)", "timestamps", "mux"] and plan.steps[1].func is not None
    assert feeder[feeder.index("-pix_fmt") + 1] == "yuv420p10le" and feeder[feeder.index("-strict") + 1] == "-1"
    assert enc[:4] == ["x265", "--y4m", "--input", "-"] and enc[enc.index("--output-depth") + 1] == "10"
    assert enc[enc.index("--range") + 1] == "limited" and enc[enc.index("--preset") + 1] == "slow" and enc[-2:] == ["-o", plan.temps[0]]
    assert plan.temps[0].endswith(".hevc") and plan.temps[1].endswith(".ts")
    assert mux[mux.index("-f") + 1] == "mpegts" and mux[mux.index("-i") + 1] == plan.temps[1] and mux[mux.index("-c:a") + 1] == "libopus"
    assert "-tag:v" not in mux and "-video_track_timescale" not in mux
    assert plan.total_frames == 548 * 3 and [s.progress_offset for s in plan.steps] == [0, 548, 1096]
    x265.container, x265.extension = "mp4", "mp4"
    mux = viewcopy.build_commands("/m/x.deint.mkv", probe, x265, "/o/x.mp4")[-1]
    assert mux[mux.index("-tag:v") + 1] == "hvc1" and mux[mux.index("-video_track_timescale") + 1] == "50"

    kv = profiles.load("mkv-kvazaar-opus")
    kv.video.rate = ViewRateMode.SINGLE
    kv.video.level = "4.1"
    feeder, enc, mux = viewcopy.build_commands("/m/x.deint.mkv", probe, kv, "/o/x.mkv")
    assert enc[:2] == ["kvazaar", "-i"] and enc[enc.index("--input-res") + 1] == "768x576" and enc[enc.index("--input-fps") + 1] == "25/1"
    assert enc[enc.index("--qp") + 1] == "24" and enc[enc.index("--input-file-format") + 1] == "y4m" and "--tune" not in enc and enc[enc.index("--level") + 1] == "4.1"
    assert feeder[feeder.index("-vf") + 1] == "framestep=2,scale=768:576:flags=lanczos,setpts=N/(25/1)/TB"
    # keep-pix_fmt profiles on masters the tool cannot take get the nearest format, not a crash
    kv.video.pix_fmt = ""
    feeder = viewcopy.build_commands("/m/x.deint.mkv", pal_probe(pix_fmt="yuv422p10le"), kv, "/o/x.mkv")[0]
    assert feeder[feeder.index("-pix_fmt") + 1] == "yuv420p"
    x264.video.pix_fmt = ""
    feeder, enc, _ = viewcopy.build_commands("/m/x.deint.mkv", pal_probe(pix_fmt="yuv411p"), x264, "/o/x.mp4")
    assert feeder[feeder.index("-pix_fmt") + 1] == "yuv422p" and enc[enc.index("--output-depth") + 1] == "8"


def test_build_commands_sox_chain() -> None:
    p = profiles.load("mp4-h264-aac-sox")
    probe = pal_probe()
    probe.audio.append(AudioProbe(index=2, codec="flac", sample_rate=48000, channels=2, bit_depth=24))
    plan = viewcopy.build_plan("/m/x.deint.mkv", probe, p, "/o/x.mp4")
    assert [s.label for s in plan.steps] == ["sox a0", "sox a1", "encode"] and plan.tools == ["ffmpeg", "sox"]
    f0, s0 = plan.steps[0].cmds
    assert f0[f0.index("-map") + 1] == "0:a:0" and f0[-3:] == ["-f", "sox", "-"]
    assert s0[:4] == ["sox", "-t", "sox", "-"] and s0[s0.index("-b") + 1] == "16" and s0[s0.index("-t", 3) + 1] == "w64"
    assert s0[-5:] == ["rate", "-v", "48000", "dither", "-s"]
    s1 = plan.steps[1].cmds[1]
    assert s1[s1.index("-b") + 1] == "24"
    enc = plan.steps[2].cmds[0]
    inputs = [enc[i + 1] for i, a in enumerate(enc) if a == "-i"]
    assert inputs[0] == "/m/x.deint.mkv" and inputs[1:] == plan.temps
    maps = [enc[i + 1] for i, a in enumerate(enc) if a == "-map"]
    assert maps == ["0:v:0", "1:a:0", "2:a:0"]
    assert enc[enc.index("-map_metadata") + 1] == "0" and "-map_metadata:s:a:1" in enc  # metadata still from the master
    probe.audio[1].language = "nld"
    enc = viewcopy.build_plan("/m/x.deint.mkv", probe, p, "/o/x.mp4").steps[2].cmds[0]
    assert enc[enc.index("-metadata:s:a:1") + 1] == "language=nld"


def test_esmux_ts_primitives() -> None:
    assert esmux._crc32_mpeg(b"123456789") == 0x0376E6E7
    pat = esmux._psi_packet(esmux.PAT_PID, esmux._pat(), 0)
    assert len(pat) == 188 and pat[0] == 0x47 and pat[1] & 0x40
    assert esmux._ts5(3, 0) == bytes([0x31, 0x00, 0x01, 0x00, 0x01])
    assert esmux._ts5(1, 90000) == bytes([0x11, 0x00, 0x05, 0xBF, 0x21])
    aus = [esmux.AccessUnit(pos=p, size=1, key=i == 0) for i, p in enumerate((0, 10, 20, 30))]
    aus, delay = esmux.annotate(aus, [0, 20, 10, 30])  # I P B P style reordering
    assert [a.display for a in aus] == [0, 2, 1, 3] and delay == 1
    with pytest.raises(EncodeError):
        esmux.annotate(aus, [0, 20, 10])


# --------------------------------------------------------------------------- real encodes
@pytest.fixture(scope="module")
def master(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("needs ffmpeg")
    return make_master(tmp_path_factory.mktemp("master") / "clip.deint.mkv")


@pytest.fixture(scope="module")
def master_probe(master: Path) -> ProbeInfo:
    p = probe_file(master)
    assert p.video.sar == Fraction(16, 15) and p.video.fps == Fraction(50, 1) and p.video.color_tags.matrix == "bt470bg"
    assert p.audio and p.audio[0].codec == "flac"
    return p


FFMPEG_ONLY = ["mp4-h264-aac", "mkv-hevc10-opus", "mkv-h264-flac"]


@pytest.mark.needs_sandbox
@pytest.mark.parametrize("pid", FFMPEG_ONLY)
def test_builtin_profile_encodes_and_decodes(master: Path, master_probe: ProbeInfo, out_dir: Path, pid: str) -> None:
    p = profiles.load(pid)
    out = Path(viewcopy.view_output_path(str(out_dir / "clip.avi"), p, "{stem}.deint", str(out_dir)))
    assert out.name == f"clip.deint.{pid}.{p.extension}"
    of, col = run(master, master_probe, p, out)
    assert of.kind == "view" and of.profile == pid and of.path == str(out) and of.frames == 50 and of.size_bytes and of.size_bytes > 1000
    assert abs((of.duration_s or 0) - 1.0) < 0.05
    assert decodes_cleanly(out)
    vs = video_stream(out)
    w, h, sar, _ = viewcopy.output_geometry(master_probe.video, p.video.scale)
    assert (vs["width"], vs["height"]) == (w, h) and vs["sample_aspect_ratio"] == f"{sar.numerator}:{sar.denominator}"
    assert vs["r_frame_rate"] == "50/1" and int(vs["nb_read_packets"]) == 50
    assert (vs["color_space"], vs["color_primaries"], vs["color_transfer"], vs["color_range"]) == ("bt470bg", "bt470bg", "bt709", "tv")
    assert vs["codec_name"] == ("hevc" if "hevc" in pid else "h264")
    if p.video.pix_fmt:
        assert vs["pix_fmt"] == p.video.pix_fmt
    a = audio_streams(out)
    assert len(a) == 1 and a[0]["codec_name"] == {"aac": "aac", "libopus": "opus", "flac": "flac"}[p.audio.encoder]
    assert a[0]["sample_rate"] == "48000" and a[0]["channels"] == 2
    frames, mse = max_mse_vs_master(out, master, p, master_probe)
    assert frames == 50 and mse < 10.0
    if p.extension == "mp4":
        assert vs["codec_tag_string"] == ("hvc1" if vs["codec_name"] == "hevc" else "avc1")
    # events: progress to the end, logs carry the command, no warnings for the built-ins
    prog = col.of("progress")
    assert prog and prog[-1].data["done"] == 50 and prog[-1].data["total"] == 50 and prog[-1].data["stage"] == "view"
    assert any("view " + pid in e.data["line"] for e in col.of("log"))
    assert not list(out_dir.glob(".*tmp*"))


@pytest.mark.needs_sandbox
def test_single_rate_and_keep_sar(master: Path, master_probe: ProbeInfo, out_dir: Path) -> None:
    p = profiles.load("mp4-h264-aac")
    p.video.rate = ViewRateMode.SINGLE
    p.video.scale.mode = ScaleMode.KEEP
    p.video.preset = "ultrafast"
    of, col = run(master, master_probe, p, out_dir / "single.mp4")
    assert of.frames == 25
    vs = video_stream(of.path)
    assert vs["r_frame_rate"] == "25/1" and vs["sample_aspect_ratio"] == "16:15" and (vs["width"], vs["height"]) == (720, 576)
    frames, mse = max_mse_vs_master(of.path, master, p, master_probe)
    assert frames == 25 and mse < 10.0  # the kept frames are the first of each pair
    assert col.of("progress")[-1].data["total"] == 25


@pytest.mark.needs_sandbox
def test_multi_track_audio_and_copy(out_dir: Path) -> None:
    m = make_master(out_dir / "two.deint.mkv", audio_tracks=2)
    probe = probe_file(m)
    assert len(probe.audio) == 2
    p = profiles.load("mkv-h264-flac")
    p.video.preset = "ultrafast"
    p.audio.encoder = "copy"
    of, _ = run(m, probe, p, out_dir / "two.mkv")
    a = audio_streams(of.path)
    assert len(a) == 2 and all(s["codec_name"] == "flac" for s in a)


@pytest.mark.needs_sandbox
def test_output_exists_and_overwrite(master: Path, master_probe: ProbeInfo, out_dir: Path) -> None:
    p = profiles.load("mp4-h264-aac")
    p.video.preset = "ultrafast"
    out = out_dir / "x.mp4"
    out.write_bytes(b"old")
    with pytest.raises(OutputError) as e:
        run(master, master_probe, p, out)
    assert e.value.code == ErrorCode.OUTPUT_EXISTS and out.read_bytes() == b"old"
    of, _ = run(master, master_probe, p, out, overwrite=True)
    assert of.size_bytes and of.size_bytes > 1000
    with pytest.raises(OutputError) as e:
        run(master, master_probe, p, out_dir / "nope" / "x.mp4")
    assert e.value.code == ErrorCode.OUTPUT_NOT_WRITABLE


@pytest.mark.needs_sandbox
def test_invalid_combination_is_refused_before_encoding(master: Path, master_probe: ProbeInfo, out_dir: Path) -> None:
    p = profiles.load("mkv-h264-flac")
    p.id, p.container, p.extension = "mov-flac", "mov", "mov"
    with pytest.raises(EncodeError) as e:
        run(master, master_probe, p, out_dir / "x.mov")
    assert e.value.code == ErrorCode.PROFILE_INVALID and "mov does not accept flac" in e.value.message
    assert not (out_dir / "x.mov").exists()


@pytest.mark.needs_sandbox
def test_encoder_failure_reports_tool_and_removes_output(master: Path, master_probe: ProbeInfo, out_dir: Path) -> None:
    p = profiles.load("mp4-h264-aac")
    p.video.extra_args = ["-not_an_ffmpeg_option", "1"]
    out = out_dir / "fail.mp4"
    with pytest.raises(EncodeError) as e:
        run(master, master_probe, p, out)
    assert e.value.code == ErrorCode.ENCODE_FAILED and "ffmpeg failed" in e.value.message
    assert e.value.details["stderr_tail"] and not out.exists()


@pytest.mark.needs_sandbox
@pytest.mark.slow
def test_cancel_removes_partial_output(out_dir: Path) -> None:
    m = make_master(out_dir / "long.deint.mkv", seconds=20)
    probe = probe_file(m)
    p = profiles.load("mkv-h264-flac")
    p.video.preset = "veryslow"  # slow enough to be cancelled mid-way
    out = out_dir / "long.mkv"
    token = CancelToken()
    col = Collector()

    class Trigger:
        def emit(self, ev: events.Event) -> None:
            col.emit(ev)
            if ev.ev == "progress" and ev.data["done"] >= 10:
                threading.Thread(target=token.cancel, args=("test",)).start()

    with pytest.raises(CancelledError) as e:
        viewcopy.run_view(str(m), probe, p, str(out), emitter=events.Emitter(Trigger()), cancel=token)
    assert str(out) in e.value.details["removed"]
    assert not out.exists() and not list(out_dir.glob(".long.mkv.*"))
    assert col.of("progress") and col.of("progress")[-1].data["done"] < 1000


@pytest.mark.needs_sandbox
def test_5994_master_gets_exact_cfr_timestamps(out_dir: Path) -> None:
    """(review m3) a Matroska 59.94 master probes as 19001/317; the mp4 copy must be exactly 60000/1001."""
    m = make_master(out_dir / "n.deint.mkv", seconds=2, fps="60000/1001", size="720x480", sar="8/9")
    probe = probe_file(m)
    assert probe.video.fps == Fraction(19001, 317)
    p = profiles.load("mp4-h264-aac")
    p.video.preset = "ultrafast"
    of, _ = run(m, probe, p, out_dir / "n.mp4")
    vs = video_stream(of.path)
    assert vs["r_frame_rate"] == "60000/1001" and vs["avg_frame_rate"] == "60000/1001" and vs["time_base"] == "1/60000"
    assert (vs["width"], vs["height"]) == (640, 480) and int(vs["nb_read_packets"]) == 120
    pts = [int(x) for x in subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts", "-of", "csv=p=0", of.path], capture_output=True, text=True, check=True).stdout.split()]
    assert pts[0] == 0 and {b - a for a, b in zip(pts, pts[1:], strict=False)} == {1001}
    p.video.rate = ViewRateMode.SINGLE
    of, _ = run(m, probe, p, out_dir / "n30.mp4")
    vs = video_stream(of.path)
    assert vs["r_frame_rate"] == "30000/1001" and int(vs["nb_read_packets"]) == 60


@pytest.mark.needs_sandbox
def test_esmux_timestamps_cancel_and_remux(master: Path, out_dir: Path) -> None:
    """esmux without the x265 CLI: a raw .hevc from libx265 (B-frames) -> TS -> mkv in display order;
    a cancel during the decode pass raises CancelledError within a second."""
    es = out_dir / "v.hevc"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "2", "-i", str(master), "-an", "-c:v", "libx265", "-preset", "ultrafast", "-crf", "10", "-x265-params", "bframes=4:b-pyramid=1:log-level=none", "-f", "hevc", str(es)], check=True)
    token = CancelToken()
    threading.Timer(0.05, token.cancel).start()
    with pytest.raises(CancelledError):
        esmux.es_to_ts(str(es), "hevc", Fraction(50), str(out_dir / "v.ts"), cancel=token)
    seen: list[int] = []
    n = esmux.es_to_ts(str(es), "hevc", Fraction(50), str(out_dir / "v.ts"), cancel=CancelToken(), progress=seen.append)
    assert n == 50 and seen and seen[-1] == 50 and not es.exists()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "mpegts", "-i", str(out_dir / "v.ts"), "-c:v", "copy", "-f", "matroska", str(out_dir / "v.mkv")], check=True)
    p = profiles.load("mkv-h264-flac")
    frames, mse = max_mse_vs_master(out_dir / "v.mkv", master, p, probe_file(master))
    assert frames == 50 and mse < 10.0


@pytest.mark.needs_sandbox
def test_collision_check_sees_the_written_path(master: Path, master_probe: ProbeInfo, out_dir: Path) -> None:
    """(review M5) jobs plans the view path with viewcopy.view_output_path; run_view writes exactly it."""
    from qtgmc_6ui import jobs
    from qtgmc_6ui.models import JobSpec, ViewCopySpec

    p = profiles.load("mp4-h264-aac")
    p.video.preset = "ultrafast"
    job = JobSpec(input=str(master), output_dir=str(out_dir), name_template="x{stem}", views=[ViewCopySpec("mp4-h264-aac")])
    planned = jobs.plan_outputs(job, [p])
    assert jobs.job_problems(job, planned) == []
    of, _ = run(master, master_probe, p, Path(job.views[0].output))
    assert of.path == planned[1] == str(out_dir / "xclip.deint.mp4-h264-aac.mp4") and Path(of.path).is_file()
    probs = jobs.job_problems(job, planned)
    assert probs and any(planned[1] in e.message for e in probs)


@pytest.mark.needs_sandbox
@pytest.mark.slow
def test_sigkill_of_cli_leaves_no_orphan_and_no_final_file(out_dir: Path) -> None:
    """SIGKILL the ``qtgmc-6ui view`` process mid-encode: the ffmpeg child dies with it
    (pdeathsig), the copy only ever existed under its hidden .partial name, and the next run
    removes the stale partial."""
    import sys
    import time

    m = make_master(out_dir / "long.deint.mkv", seconds=20)
    out = out_dir / "long.mkv"
    cmd = [sys.executable, "-m", "qtgmc_6ui", "view", str(m), "--profile", "mkv-h264-flac", "-o", str(out), "--events"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env={**os.environ, "QTGMC6UI_CONFIG_DIR": str(out_dir / "cfg"), "QTGMC6UI_CACHE_DIR": str(out_dir / "cache")})
    assert proc.stdout is not None
    deadline = time.monotonic() + 120
    started = False
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        ev = events.Event.from_json(line)
        if ev and ev.ev == "progress" and ev.data.get("done", 0) >= 5:
            started = True
            break
    assert started, "the view never reported progress"
    partial = Path(viewcopy.partial_path(str(out)))
    assert partial.exists() and not out.exists()
    children = _children_of(proc.pid)
    assert children, "no ffmpeg child found"
    proc.kill()
    proc.wait()
    for _ in range(50):
        if all(not _alive(c) for c in children):
            break
        time.sleep(0.1)
    assert all(not _alive(c) for c in children), "ffmpeg survived the SIGKILL of the CLI"
    assert not out.exists()
    # the stale partial is cleaned up by the next attempt
    p = profiles.load("mkv-h264-flac")
    p.video.preset = "ultrafast"
    of, _ = run(m, probe_file(m), p, out)
    assert Path(of.path).is_file() and not partial.exists()


def _children_of(pid: int) -> list[int]:
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text().split()
            if int(stat[3]) == pid:
                out.append(int(d.name))
                out += _children_of(int(d.name))
        except (OSError, ValueError, IndexError):
            pass
    return out


def _alive(pid: int) -> bool:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text().split()
        return stat[2] not in ("Z", "X")
    except OSError:
        return False
