"""ffprobe mapping on recorded JSON (no tools needed) + live probes in the sandbox."""

from __future__ import annotations

import json
import os
import shutil
from fractions import Fraction
from pathlib import Path

import pytest

from qtgmc_6ui import ffprobe
from qtgmc_6ui.errors import ComponentError, ErrorCode, InputError

from conftest import REFERENCE_DURATION_S, REFERENCE_FRAMES

# ffprobe -v error -print_format json -show_format -show_streams test_input.avi  (ffmpeg n9.0.2)
REF_JSON = json.loads(r"""
{
    "streams": [
        {
            "index": 0, "codec_name": "dvvideo", "codec_long_name": "DV (Digital Video)",
            "codec_type": "video", "codec_tag_string": "dvsd", "width": 720, "height": 576,
            "coded_width": 720, "coded_height": 576, "has_b_frames": 0,
            "sample_aspect_ratio": "16:15", "display_aspect_ratio": "4:3", "pix_fmt": "yuv420p",
            "level": -99, "chroma_location": "topleft", "r_frame_rate": "25/1",
            "avg_frame_rate": "25/1", "time_base": "1/25", "start_pts": 0, "start_time": "0.000000",
            "duration_ts": 274, "duration": "10.960000", "bit_rate": "28800000", "nb_frames": "274",
            "disposition": {"default": 0, "attached_pic": 0}
        },
        {
            "index": 1, "codec_name": "pcm_s16le", "codec_long_name": "PCM signed 16-bit little-endian",
            "codec_type": "audio", "sample_fmt": "s16", "sample_rate": "48000", "channels": 2,
            "bits_per_sample": 16, "r_frame_rate": "0/0", "avg_frame_rate": "0/0",
            "time_base": "1/48000", "bit_rate": "1536000", "nb_frames": "526080",
            "disposition": {"default": 0, "attached_pic": 0}
        }
    ],
    "format": {
        "filename": "/workspace/qtgmc/test_input.avi", "nb_streams": 2, "format_name": "avi",
        "format_long_name": "AVI (Audio Video Interleaved)", "start_time": "0.000000",
        "duration": "10.960000", "size": "41703544", "bit_rate": "30440543", "probe_score": 100
    }
}
""")

# ffv1 in Matroska: no nb_frames, duration only as a tag, RGB pixel format, pc range.
MKV_JSON = json.loads(r"""
{
    "streams": [
        {
            "index": 0, "codec_name": "ffv1", "codec_long_name": "FFmpeg video codec #1",
            "codec_type": "video", "width": 720, "height": 480, "sample_aspect_ratio": "1:1",
            "display_aspect_ratio": "3:2", "pix_fmt": "bgr0", "color_range": "pc",
            "color_space": "gbr", "field_order": "progressive", "r_frame_rate": "30000/1001",
            "avg_frame_rate": "30000/1001", "time_base": "1/1000", "bits_per_raw_sample": "8",
            "disposition": {"default": 0, "attached_pic": 0},
            "tags": {"ENCODER": "Lavc63.1.102 ffv1", "DURATION": "00:00:04.004000000"}
        }
    ],
    "format": {
        "filename": "c_tele.mkv", "nb_streams": 1, "format_name": "matroska,webm",
        "duration": "4.004000", "size": "39080255", "bit_rate": "78082427"
    }
}
""")


def test_reference_mapping() -> None:
    p = ffprobe.probe_from_json(REF_JSON, "/workspace/qtgmc/test_input.avi")
    v = p.video
    assert p.container == "avi" and p.size_bytes == 41703544
    assert p.duration_s == pytest.approx(REFERENCE_DURATION_S)
    assert (v.width, v.height) == (720, 576)
    assert v.codec == "dvvideo" and v.pix_fmt == "yuv420p" and v.bit_depth == 8
    assert v.fps == Fraction(25, 1) and v.sar == Fraction(16, 15) and v.dar == Fraction(4, 3)
    assert v.nb_frames == REFERENCE_FRAMES and v.nb_frames_exact
    assert v.container_field_order == "unknown"
    assert v.color_tags.matrix == "unknown" and v.color_tags.range == "unknown" and v.color_tags.source == "unknown"
    assert v.avg_fps == Fraction(25, 1) and v.start_time_s == 0.0 and p.start_time_s == 0.0
    assert p.video_stream_index == 0
    assert len(p.audio) == 1
    a = p.audio[0]
    assert (a.index, a.codec, a.sample_rate, a.channels, a.bit_depth) == (1, "pcm_s16le", 48000, 2, 16)
    assert a.duration_s == pytest.approx(REFERENCE_DURATION_S)
    # JSON round trip of the contract
    assert type(p).from_dict(json.loads(json.dumps(p.to_dict()))) == p


def test_mkv_estimated_frames_and_tags() -> None:
    p = ffprobe.probe_from_json(MKV_JSON, "c_tele.mkv")
    v = p.video
    assert v.fps == Fraction(30000, 1001)
    assert v.duration_s == pytest.approx(4.004)
    assert v.nb_frames == 120 and not v.nb_frames_exact
    assert v.container_field_order == "progressive"
    assert v.color_tags.matrix == "gbr" and v.color_tags.range == "pc" and v.color_tags.source == "tags"
    assert v.color_tags.primaries == "unknown"
    assert p.audio == []


def test_attached_picture_is_skipped() -> None:
    data = json.loads(json.dumps(MKV_JSON))
    cover = {
        "index": 0, "codec_name": "mjpeg", "codec_type": "video", "width": 600, "height": 600,
        "r_frame_rate": "90000/1", "disposition": {"attached_pic": 1},
    }
    data["streams"][0]["index"] = 1
    data["streams"].insert(0, cover)
    p = ffprobe.probe_from_json(data, "x.mkv")
    assert p.video.codec == "ffv1" and p.video_stream_index == 1


def test_start_times_and_avg_fps() -> None:
    data = json.loads(json.dumps(REF_JSON))
    data["streams"][0]["start_time"] = "1.930000"
    data["streams"][0]["avg_frame_rate"] = "4250/99"
    data["streams"][1]["start_time"] = "1.400000"
    data["format"]["start_time"] = "1.400000"
    p = ffprobe.probe_from_json(data, "avoffset.ts")
    assert p.video.start_time_s == pytest.approx(1.93) and p.audio[0].start_time_s == pytest.approx(1.4)
    assert p.start_time_s == pytest.approx(1.4)
    assert p.video.fps == Fraction(25, 1) and p.video.avg_fps == Fraction(4250, 99)
    data["streams"][0]["avg_frame_rate"] = "0/0"
    assert ffprobe.probe_from_json(data, "x").video.avg_fps is None


def test_no_video_stream() -> None:
    data = {"streams": [REF_JSON["streams"][1]], "format": REF_JSON["format"]}
    with pytest.raises(InputError) as ei:
        ffprobe.probe_from_json(data, "audio.wav")
    assert ei.value.code == ErrorCode.INPUT_NO_VIDEO
    assert ei.value.exit_code == 5


@pytest.mark.parametrize(
    "pix_fmt,depth",
    [("yuv420p", 8), ("yuv420p10le", 10), ("yuv422p10le", 10), ("yuv444p12le", 12), ("yuv444p16be", 16),
     ("gray", 8), ("gray16le", 16), ("yuv411p", 8), ("yuva420p", 8), ("rgb24", 8), ("rgb48le", 16),
     ("bgr0", 8), ("", 8)],
)
def test_bit_depth_from_pix_fmt(pix_fmt: str, depth: int) -> None:
    assert ffprobe.bit_depth_from_pix_fmt(pix_fmt) == depth


def test_fraction_and_duration_parsing() -> None:
    assert ffprobe.parse_fraction("30000/1001") == Fraction(30000, 1001)
    assert ffprobe.parse_fraction("16:15") == Fraction(16, 15)
    assert ffprobe.parse_fraction("0/0") is None
    assert ffprobe.parse_fraction("N/A", Fraction(1)) == Fraction(1)
    assert ffprobe.parse_fraction("0:1", Fraction(1)) == Fraction(1)
    assert ffprobe.parse_duration("00:00:04.004000000") == pytest.approx(4.004)
    assert ffprobe.parse_duration("10.96") == pytest.approx(10.96)
    assert ffprobe.parse_duration("N/A") is None


def test_missing_input_raises_before_running_tools(tmp_path: Path) -> None:
    with pytest.raises(InputError) as ei:
        ffprobe.probe(str(tmp_path / "nope.avi"), ffprobe="/nonexistent/ffprobe")
    assert ei.value.code == ErrorCode.INPUT_NOT_FOUND


def test_missing_tool_is_component_error(tmp_path: Path) -> None:
    f = tmp_path / "x.avi"
    f.write_bytes(b"not a video")
    with pytest.raises(ComponentError) as ei:
        ffprobe.probe(str(f), ffprobe="/nonexistent/ffprobe")
    assert ei.value.code == ErrorCode.TOOL_MISSING


def test_tool_env_forces_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "C")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    env = ffprobe.tool_env()
    assert env["LC_CTYPE"] == "C.UTF-8" and env["AV_LOG_FORCE_NOCOLOR"] == "1"
    monkeypatch.setenv("LC_ALL", "C")
    assert ffprobe.tool_env()["LC_ALL"] == "C.UTF-8"
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    assert ffprobe.tool_env()["LC_ALL"] == "en_US.UTF-8"


def test_version_regex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffprobe, "run_tool", lambda cmd, timeout=None: (0, "ffmpeg version n9.0.2 Copyright (c) 2000-2026\n", ""))
    assert ffprobe.ffmpeg_version() == "n9.0.2"


# --------------------------------------------------------------------------- live
@pytest.mark.needs_sandbox
def test_probe_reference_clip(reference_clip: Path) -> None:
    p = ffprobe.probe(str(reference_clip))
    assert p.video.nb_frames == REFERENCE_FRAMES and p.video.nb_frames_exact
    assert p.video.sar == Fraction(16, 15) and (p.video.width, p.video.height) == (720, 576)
    assert p.video.codec == "dvvideo" and p.audio[0].codec == "pcm_s16le"
    assert ffprobe.count_frames(str(reference_clip)) == REFERENCE_FRAMES
    assert ffprobe.ffmpeg_version().startswith("n")


@pytest.mark.needs_sandbox
def test_probe_non_utf8_file_name(reference_clip: Path, tmp_path: Path) -> None:
    # latin-1 bytes in the name: Python sees a surrogate-escaped str, argv must carry the raw bytes
    raw = os.fsencode(str(tmp_path)) + b"/caf\xe9 latin1.avi"
    os.symlink(os.fsencode(str(reference_clip)), raw)
    target = os.fsdecode(raw)
    assert "\udce9" in target
    p = ffprobe.probe(target)
    assert p.video.nb_frames == REFERENCE_FRAMES and p.path == target
    assert ffprobe.count_frames(target) == REFERENCE_FRAMES


@pytest.mark.needs_sandbox
def test_probe_unreadable_file(tmp_path: Path) -> None:
    f = tmp_path / "garbage.avi"
    f.write_bytes(b"\0" * 4096)
    with pytest.raises(InputError) as ei:
        ffprobe.probe(str(f))
    assert ei.value.code == ErrorCode.INPUT_UNREADABLE


@pytest.mark.needs_sandbox
def test_probe_non_ascii_path(reference_clip: Path, tmp_path: Path) -> None:
    d = tmp_path / "tést ビデオ"
    d.mkdir()
    target = d / "référence ö.avi"
    try:
        target.symlink_to(reference_clip)
    except OSError:
        shutil.copyfile(reference_clip, target)
    p = ffprobe.probe(str(target))
    assert p.video.nb_frames == REFERENCE_FRAMES
    assert p.path == str(target)
    assert ffprobe.count_frames(str(target)) == REFERENCE_FRAMES
