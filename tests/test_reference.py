"""The reference clip /workspace/qtgmc/test_input.avi (274 frames, BFF, 720x576, SAR 16:15,
PCM 48k stereo; NOT the 176 frames of the brief)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from conftest import (
    REFERENCE_AUDIO,
    REFERENCE_DURATION_S,
    REFERENCE_FRAMES,
    REFERENCE_FRAMES_BOBBED,
    REFERENCE_SAR,
    REFERENCE_SIZE,
)
from helpers import audio_duration, cli_json, deint_master, media_info

pytestmark = pytest.mark.needs_sandbox


def test_reference_clip_facts(reference_clip):
    mi = media_info(reference_clip)
    assert mi.frames == REFERENCE_FRAMES
    assert (mi.width, mi.height) == REFERENCE_SIZE
    assert mi.sar == REFERENCE_SAR
    assert mi.fps == Fraction(25)
    assert len(mi.audio) == 1 and mi.audio[0]["codec_name"] == REFERENCE_AUDIO["codec"]


def test_reference_analysis_is_bff(reference_clip, isolated_dirs):
    rc, doc, err = cli_json("analyze", str(reference_clip), "--no-cache")
    assert rc == 0, err
    assert doc["classification"]["content"] == "interlaced_bff"
    assert doc["recommendation"]["action"] == "deinterlace"


def _check_master(out, ref):
    mi = media_info(out)
    assert mi.frames == REFERENCE_FRAMES_BOBBED
    assert mi.fps == Fraction(50)
    assert (mi.width, mi.height) == REFERENCE_SIZE
    assert mi.sar == REFERENCE_SAR
    assert mi.pix_fmt == "yuv420p"
    assert mi.codec == "ffv1"
    assert len(mi.audio) == 1
    a = mi.audio[0]
    assert a["codec_name"] == "flac"
    assert int(a["sample_rate"]) == REFERENCE_AUDIO["sample_rate"]
    assert a["channels"] == REFERENCE_AUDIO["channels"]
    assert abs(audio_duration(out) - REFERENCE_DURATION_S) <= 0.02
    assert abs(audio_duration(out) - audio_duration(ref)) <= 0.02


@pytest.mark.needs_qtgmc
@pytest.mark.slow
def test_reference_master_fast_preset(reference_clip, tmp_path, isolated_dirs):
    out = tmp_path / "ref.deint.mkv"
    deint_master(reference_clip, out, preset="draft")  # field order from analysis (auto)
    _check_master(out, reference_clip)


@pytest.mark.needs_qtgmc
@pytest.mark.slow
def test_reference_master_default_preset(reference_clip, tmp_path, isolated_dirs):
    out = tmp_path / "ref.deint.mkv"
    deint_master(reference_clip, out, preset=None)  # resolution default preset
    _check_master(out, reference_clip)
