"""encoders: parsers against recorded ffmpeg n9.0.2 output (tests/data/ffmpeg), tool detection."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from qtgmc_6ui import encoders
from qtgmc_6ui.errors import ComponentError, UsageError

DATA = Path(__file__).parent / "data" / "ffmpeg"


def rec(name: str) -> str:
    return (DATA / name).read_text("utf-8")


def test_parse_version() -> None:
    assert encoders.parse_version(rec("version.txt")) == "n9.0.2"
    assert encoders.parse_version("garbage") == "unknown"


def test_parse_encoders_listing() -> None:
    encs = encoders.parse_encoders(rec("encoders.txt"))
    names = {e.name: e for e in encs}
    assert "libx264" in names and names["libx264"].kind == "video" and names["libx264"].codec == "h264"
    assert names["libx264"].description.startswith("libx264 H.264")
    assert names["aac"].kind == "audio" and names["aac"].codec == "aac"
    assert names["libopus"].codec == "opus" and names["flac"].codec == "flac"
    assert names["pcm_s16le"].kind == "audio"
    assert "libkvazaar" not in names  # Arch's ffmpeg lacks it (research)
    assert not any(n.startswith("ass") or n in ("srt", "webvtt", "dvdsub") for n in names)  # subtitles dropped
    assert not any(e.name in ("=", "Video", "Audio") for e in encs)  # legend lines never parsed
    assert len(encs) > 200


def test_parse_muxers_listing() -> None:
    muxers = encoders.parse_muxers(rec("muxers.txt"))
    names = {m.name: m for m in muxers}
    for n in ("matroska", "mp4", "mov", "webm", "avi", "mpegts", "nut", "ogg", "yuv4mpegpipe", "sox", "wav"):
        assert n in names, n
    assert names["mp4"].description == "MP4 (MPEG-4 Part 14)"
    assert "Formats:" not in names and "---" not in names
    assert len(muxers) > 150


def test_parse_encoder_help_libx264() -> None:
    info = encoders.parse_encoder_help(rec("h-encoder-libx264.txt"), "libx264")
    assert info.name == "libx264" and info.kind == "video"
    assert info.pix_fmts[:3] == ["yuv420p", "yuvj420p", "yuv422p"] and "yuv420p10le" in info.pix_fmts
    opts = {o.name: o for o in info.options}
    assert opts["crf"].type == "float" and opts["crf"].minimum == "-1" and opts["crf"].maximum == "FLT_MAX" and opts["crf"].default == "-1"
    assert opts["preset"].type == "string" and opts["preset"].default == "medium"
    # plain <string> options get the hard-coded choices
    assert opts["preset"].choices == list(encoders.X264_PRESETS)
    assert opts["tune"].choices == [t for t in encoders.X264_TUNES if t]
    assert opts["aq-mode"].choices == ["none", "variance", "autovariance", "autovariance-biased"]
    assert opts["direct-pred"].choices == ["none", "spatial", "temporal", "auto"]
    assert opts["partitions"].choices == []  # free-form string, no enum rows
    assert opts["a53cc"].type == "boolean" and opts["a53cc"].default == "true"
    assert opts["crf"].help == "Select the quality for constant quality mode"


def test_parse_encoder_help_libx265() -> None:
    info = encoders.parse_encoder_help(rec("h-encoder-libx265.txt"), "libx265")
    opts = {o.name: o for o in info.options}
    assert opts["preset"].choices == list(encoders.X265_PRESETS)
    assert opts["x265-params"].type == "dictionary"
    assert opts["dolbyvision"].choices == ["auto"]  # enum row without a numeric value column
    assert "yuv420p12le" in info.pix_fmts


def test_parse_encoder_help_audio() -> None:
    opus = encoders.parse_encoder_help(rec("h-encoder-libopus.txt"), "libopus")
    assert opus.kind == "audio"
    assert opus.sample_rates == [48000, 24000, 16000, 12000, 8000]
    assert opus.sample_fmts == ["s16", "flt"]
    opts = {o.name: o for o in opus.options}
    assert opts["vbr"].choices == ["off", "on", "constrained"] and opts["vbr"].default == "on"
    assert opts["application"].choices == ["voip", "audio", "lowdelay"]
    assert opts["frame_duration"].minimum == "2.5" and opts["frame_duration"].maximum == "120"
    aac = encoders.parse_encoder_help(rec("h-encoder-aac.txt"), "aac")
    assert 48000 in aac.sample_rates and aac.sample_fmts == ["fltp"]
    flac = encoders.parse_encoder_help(rec("h-encoder-flac.txt"), "flac")
    assert flac.kind == "audio" and flac.sample_fmts == ["s16", "s32"] and flac.sample_rates == []
    pcm = encoders.parse_encoder_help(rec("h-encoder-pcm_s16le.txt"), "pcm_s16le")
    assert pcm.options == []  # no AVOptions block at all


def test_parse_encoder_help_quirks() -> None:
    # flags options: enum rows carry no value column; int enums with negative/huge values
    vp9 = encoders.parse_encoder_help(rec("h-encoder-libvpx-vp9.txt"), "libvpx-vp9")
    opts = {o.name: o for o in vp9.options}
    assert opts["error-resilient"].type == "flags" and opts["error-resilient"].choices == ["default", "partitions"]
    assert opts["deadline"].choices == ["best", "good", "realtime"] and opts["deadline"].default == "good"
    assert opts["crf"].type == "int" and opts["crf"].maximum == "63"
    assert opts["arnr-type"].choices == ["backward", "forward", "centered"]
    assert "gbrp12le" in vp9.pix_fmts
    prores = encoders.parse_encoder_help(rec("h-encoder-prores_ks.txt"), "prores_ks")
    opts = {o.name: o for o in prores.options}
    assert opts["profile"].choices == ["auto", "proxy", "lt", "standard", "hq", "4444", "4444xq"]
    assert opts["profile"].default == "auto" and opts["profile"].minimum == "-1"
    assert opts["quant_mat"].choices[-1] == "default"
    assert prores.pix_fmts == ["yuv422p10le", "yuv444p10le", "yuva444p10le"]
    svt = encoders.parse_encoder_help(rec("h-encoder-libsvtav1.txt"), "libsvtav1")
    opts = {o.name: o for o in svt.options}
    assert opts["preset"].type == "int" and opts["preset"].minimum == "-2" and opts["preset"].maximum == "13"
    ffv1 = encoders.parse_encoder_help(rec("h-encoder-ffv1.txt"), "ffv1")
    assert ffv1.kind == "video" and "yuv420p" in ffv1.pix_fmts
    # the trailing "Exiting with exit code 0" line never becomes an option
    assert not any(o.name.startswith("Exiting") for o in ffv1.options)


def test_hardware_encoders_are_hidden() -> None:
    encs = encoders.parse_encoders(rec("encoders.txt"))
    hw = [e.name for e in encs if encoders.is_hw_encoder(e.name)]
    assert hw and all(n.endswith(("_vaapi", "_nvenc", "_qsv", "_amf", "_vulkan", "_v4l2m2m")) for n in hw)
    assert not encoders.is_hw_encoder("libx264") and not encoders.is_hw_encoder("libsvtav1")


def test_tool_info_is_memoised_per_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    a = encoders.tool_info("x264")
    assert not a.available
    assert encoders.tool_info("x264") is a
    monkeypatch.setenv("PATH", str(tmp_path / "other"))
    assert encoders.tool_info("x264") is not a  # a changed PATH re-detects


def test_unknown_encoder_is_detected_by_text() -> None:
    text = rec("h-encoder-nonexistent.txt")
    assert encoders.is_unknown_encoder_text(text)
    with pytest.raises(UsageError):
        encoders.parse_encoder_help(text, "nonexistent")


def test_parse_muxer_help() -> None:
    mp4 = encoders.parse_muxer_help(rec("h-muxer-mp4.txt"), "mp4")
    assert mp4.description == "MP4 (MPEG-4 Part 14)" and mp4.extensions == ["mp4"]
    assert mp4.default_video == "h264" and mp4.default_audio == "aac"
    mkv = encoders.parse_muxer_help(rec("h-muxer-matroska.txt"), "matroska")
    assert mkv.extensions == ["mkv"] and mkv.name == "matroska"
    mov = encoders.parse_muxer_help(rec("h-muxer-mov.txt"), "mov")
    assert mov.extensions == ["mov"]
    webm = encoders.parse_muxer_help(rec("h-muxer-webm.txt"), "webm")
    assert webm.default_audio in ("libopus", "opus", "libvorbis", "vorbis")


def test_detect_tools_without_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing in PATH
    tools = {t.name: t for t in encoders.detect_tools()}
    assert set(tools) == set(encoders.REQUIRED_TOOLS) | set(encoders.OPTIONAL_TOOLS)
    assert not any(t.available for t in tools.values())
    assert tools["ffmpeg"].note == "required" and "pacman -S x264" in tools["x264"].note
    with pytest.raises(ComponentError):
        encoders.list_encoders()


@pytest.mark.needs_sandbox
def test_detect_tools_real() -> None:
    tools = {t.name: t for t in encoders.detect_tools()}
    assert tools["ffmpeg"].available and tools["ffmpeg"].version.startswith("n")
    assert tools["ffprobe"].available
    for name in encoders.OPTIONAL_TOOLS:
        assert tools[name].available == bool(shutil.which(name))
        if tools[name].available:
            assert tools[name].version, name
    if tools["x265"].available:
        assert "bit" in tools["x265"].note


@pytest.mark.needs_sandbox
def test_runtime_discovery_and_cache(isolated_dirs: dict[str, Path]) -> None:
    encoders.invalidate_cache()
    muxers = encoders.muxer_names()
    assert {"matroska", "mp4", "mov", "yuv4mpegpipe"} <= muxers
    vid, aud = encoders.encoder_names("video"), encoders.encoder_names("audio")
    assert {"libx264", "libx265", "ffv1"} <= vid and {"aac", "flac", "libopus"} <= aud
    x264 = encoders.encoder_details("libx264")
    assert x264.kind == "video" and x264.codec == "h264" and "yuv420p" in x264.pix_fmts
    assert [o.name for o in x264.options][:3] == ["preset", "tune", "profile"]
    assert encoders.muxer_details("mp4").default_video == "h264"
    with pytest.raises(UsageError):
        encoders.encoder_details("nonexistent")
    cache_files = list((isolated_dirs["cache"] / "muxmatrix").glob("encoders-*.json"))
    assert len(cache_files) == 1 and cache_files[0].stat().st_size > 1000
    build = encoders._version_info("ffmpeg")
    assert build[1].startswith(build[0] + "-") and build[1] in cache_files[0].name  # keyed on the build, not just the version
    assert not any(encoders.is_hw_encoder(e.name) for e in encoders.list_encoders())
    # a second process-level lookup is served from the in-memory/disk cache (no ffmpeg needed)
    encoders._mem.clear()
    saved = os.environ["PATH"]
    try:
        os.environ["PATH"] = "/nonexistent"
        encoders._mem["version:ffmpeg"] = build
        assert encoders.encoder_details("libx264").codec == "h264"
    finally:
        os.environ["PATH"] = saved
        encoders.invalidate_cache()
