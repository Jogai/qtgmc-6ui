"""Master metadata: SAR, colour tags, frame rate, audio tracks/duration, frame-count rules."""

from __future__ import annotations

from fractions import Fraction

import pytest

from fixtures_gen import NAMED, get_clip
from helpers import audio_duration, deint_master, media_info

pytestmark = [pytest.mark.needs_sandbox, pytest.mark.needs_qtgmc]


@pytest.fixture(scope="module")
def masters(tmp_path_factory):
    """Memoised master encodes: (clip name, rate, extra args) -> path."""
    base = tmp_path_factory.mktemp("masters")
    cache: dict[tuple, object] = {}

    def make(name: str, rate: str = "double", *extra: str, env=None, out_name: str | None = None):
        key = (name, rate, extra, tuple(sorted((env or {}).items())), out_name)
        if key not in cache:
            spec = NAMED[name]
            out = base / f"{len(cache)}" / (out_name or "master.mkv")
            out.parent.mkdir()
            deint_master(get_clip(name), out, *extra, rate=rate,
                         field_order="tff" if spec.kind == "tff" else "bff", env=env)
            cache[key] = out
        return cache[key]

    return make


@pytest.fixture(autouse=True)
def _dirs(isolated_dirs):
    pass


def _sar(s: str) -> Fraction:
    n, d = s.replace("/", ":").split(":")
    return Fraction(int(n), int(d))


@pytest.mark.parametrize("name", ["res_720x576_tff", "res_720x480_bff", "res_1440x1080_tff", "sar_pal_43",
                                  "pal_tff"])
def test_sar_preserved(name, masters):
    src = media_info(get_clip(name))
    out = media_info(masters(name))
    assert _sar(out.sar) == _sar(src.sar), (src.sar, out.sar)


@pytest.mark.parametrize("name,expected", [
    ("tagged_709_tff", ("bt709", "bt709", "bt709", "tv")),
    ("tagged_601_bff", ("bt470bg", "bt470bg", "bt709", "tv")),
])
def test_explicit_colour_tags_kept(name, expected, masters):
    assert media_info(masters(name)).color == expected


@pytest.mark.parametrize("name,expected", [  # untagged sources get resolution defaults
    ("pal_tff", ("bt470bg", "bt470bg", "bt709", "tv")),
    ("ntsc_bff", ("smpte170m", "smpte170m", "smpte170m", "tv")),
])
def test_untagged_gets_defaults(name, expected, masters):
    assert media_info(get_clip(name)).color[:3] == ("", "", "")
    assert media_info(masters(name)).color == expected


@pytest.mark.parametrize("name,rate,frames_mult,fps", [
    ("ntsc_tff", "double", 2, Fraction(60000, 1001)),
    ("ntsc_tff", "single", 1, Fraction(30000, 1001)),
    ("ntsc30_bff", "double", 2, Fraction(60)),
    ("pal_bff", "double", 2, Fraction(50)),
    ("pal_bff", "single", 1, Fraction(25)),
])
def test_frame_rate_and_count(name, rate, frames_mult, fps, masters):
    src = media_info(get_clip(name))
    out = media_info(masters(name, rate))
    assert out.fps == fps, f"{out.fps} != {fps}"
    assert out.frames == src.frames * frames_mult
    assert abs(out.duration - src.duration) < 0.05


def test_ffv1_master_keeps_pix_fmt(masters):
    out = media_info(masters("fmt_422p10_tff"))
    assert out.codec == "ffv1" and out.pix_fmt == "yuv422p10le"


@pytest.mark.parametrize("name,tracks", [("audio1_pal_tff", 1), ("audio2_pal_bff", 2), ("pal_tff", 0)])
def test_audio_tracks_and_duration(name, tracks, masters):
    src = media_info(get_clip(name))
    out = media_info(masters(name))
    assert len(src.audio) == tracks
    assert len(out.audio) == tracks
    for i in range(tracks):
        assert out.audio[i]["codec_name"] == "flac"
        assert int(out.audio[i]["sample_rate"]) == 48000
        assert abs(audio_duration(masters(name), i) - audio_duration(get_clip(name), i)) < 0.03
        assert out.audio[i]["channels"] == src.audio[i]["channels"]


@pytest.mark.parametrize("env", [
    None,
    {"LANG": "C", "LC_ALL": None},
])
def test_non_ascii_names(env, masters):
    out = masters("unicode_tff", "double", env=env, out_name="résultat ñ – 日本語.mkv")
    assert out.name == "résultat ñ – 日本語.mkv" and out.stat().st_size > 0
    assert media_info(out).frames == 2 * media_info(get_clip("unicode_tff")).frames
    assert len(media_info(out).audio) == 1


def test_dv_ntsc_411_master(masters):
    out = media_info(masters("dv_ntsc_bff"))
    src = media_info(get_clip("dv_ntsc_bff"))
    assert out.pix_fmt == "yuv411p" and _sar(out.sar) == _sar(src.sar)
    assert out.frames == 2 * src.frames and out.fps == Fraction(60000, 1001)
