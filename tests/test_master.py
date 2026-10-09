"""Master ffmpeg command (fixed recipe) and stats parsing."""

from __future__ import annotations

from fractions import Fraction

from qtgmc_6ui import master
from qtgmc_6ui.models import Colorimetry


def test_master_command_recipe() -> None:
    c = Colorimetry("bt470bg", "bt470bg", "bt709", "tv", "default")
    cmd = master.master_command("/in/Ünïcode clip.avi", "/out/x.mkv", Fraction(16, 15), c, chroma_location="topleft")
    assert cmd[0] == "ffmpeg" and cmd[-1] == "/out/x.mkv" and cmd[-3:-1] == ["-f", "matroska"]
    assert "-nostdin" in cmd and "-n" in cmd and "-y" not in cmd and "-itsoffset" not in cmd
    i = cmd.index("-i")
    assert cmd[i + 1] == "pipe:0" and cmd[i + 3] == "/in/Ünïcode clip.avi"
    assert cmd[cmd.index("-map") + 1] == "0:v:0" and "1:a?" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "setsar=16/15,setparams=colorspace=bt470bg:color_primaries=bt470bg:color_trc=bt709:range=tv:chroma_location=topleft"
    assert cmd[cmd.index("-color_range") + 1] == "tv"
    assert cmd[cmd.index("-chroma_sample_location") + 1] == "3"
    for a in ("-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "16", "-slicecrc", "1", "-c:a", "flac", "-compression_level", "8"):
        assert a in cmd
    assert "-pix_fmt" not in cmd and "-strict" not in cmd  # source format is kept


def test_master_command_full_range_offset_32bit() -> None:
    c = Colorimetry("bt470bg", "bt470bg", "bt709", "pc", "tags")
    cmd = master.master_command("in.avi", "out.mkv", Fraction(1, 1), c, video_offset_s=0.53, flac_32bit=True)
    assert cmd[cmd.index("-color_range") + 1] == "pc" and ":range=pc" in cmd[cmd.index("-vf") + 1]
    i = cmd.index("-itsoffset")
    assert cmd[i + 1] == "-0.530000" and cmd[i + 2 : i + 4] == ["-i", "in.avi"] and cmd.index("pipe:0") < i
    assert cmd[cmd.index("-strict") + 1] == "experimental" and cmd.index("-strict") > cmd.index("flac")
    assert "-chroma_sample_location" not in cmd
    assert master.normalize_range("full") == "pc" and master.normalize_range("limited") == "tv" and master.normalize_range("x") == ""


def test_master_command_variants() -> None:
    c = Colorimetry("smpte170m", "unknown", "unknown", "pc")
    cmd = master.master_command("in.avi", "out.mkv", Fraction(1, 1), c, overwrite=True, audio_tracks=0, ffmpeg="/usr/bin/ffmpeg")
    assert cmd[0] == "/usr/bin/ffmpeg" and "-y" in cmd and "1:a?" not in cmd and "flac" not in cmd
    assert cmd[cmd.index("-vf") + 1] == "setsar=1/1,setparams=colorspace=smpte170m:range=pc"
    cmd = master.master_command("in.avi", "out.mkv", Fraction(0, 1), Colorimetry(), audio_tracks=2, video_offset_s=-0.2)
    assert cmd[cmd.index("-vf") + 1] == "setsar=1/1,setparams=range=tv" and "-itsoffset" not in cmd
    assert "1:a:0" in cmd and "1:a:1" in cmd and "flac" in cmd


def test_parse_ffmpeg_progress() -> None:
    d = master.parse_ffmpeg_progress("frame=  519 fps= 43 q=-0.0 size=   15360KiB time=00:00:10.36 bitrate=12142.4kbits/s speed=0.86x    ")
    assert d is not None and d["frame"] == "519" and d["fps"] == "43" and d["time"] == "00:00:10.36" and d["speed"] == "0.86x"
    assert master.parse_ffmpeg_progress("[matroska @ 0x1] Starting new cluster") is None
    assert master.parse_ffmpeg_progress("") is None
