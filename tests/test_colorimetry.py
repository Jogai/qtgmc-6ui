"""Colour defaults by resolution/rate family, tag precedence, setparams string."""

from __future__ import annotations

from fractions import Fraction

import pytest

from qtgmc_6ui import colorimetry
from qtgmc_6ui.models import Colorimetry, VideoProbe


def video(w: int, h: int, fps: Fraction | str, **tags: str) -> VideoProbe:
    v = VideoProbe(width=w, height=h, fps=Fraction(fps))
    if tags:
        v.color_tags = Colorimetry(**{"source": "tags", **tags})
    return v


@pytest.mark.parametrize(
    "fps,family",
    [("25/1", "pal"), ("50/1", "pal"), ("30000/1001", "ntsc"), ("30/1", "ntsc"), ("60000/1001", "ntsc"),
     ("60/1", "ntsc"), ("24000/1001", "other"), ("24/1", "other"), ("15/1", "other"), ("0/1", "other")],
)
def test_rate_family(fps: str, family: str) -> None:
    assert colorimetry.rate_family(Fraction(fps)) == family


def test_is_hd() -> None:
    assert not colorimetry.is_hd(720, 576) and not colorimetry.is_hd(720, 480) and not colorimetry.is_hd(352, 288)
    assert colorimetry.is_hd(1920, 1080) and colorimetry.is_hd(1440, 1080) and colorimetry.is_hd(1280, 720)
    assert colorimetry.is_hd(960, 720)


def test_defaults_pal_ntsc_hd() -> None:
    c = colorimetry.default_colorimetry(video(720, 576, "25/1"))
    assert (c.matrix, c.primaries, c.transfer, c.range, c.source) == ("bt470bg", "bt470bg", "bt709", "tv", "default")
    c = colorimetry.default_colorimetry(video(720, 480, "30000/1001"))
    assert (c.matrix, c.primaries, c.transfer, c.range) == ("smpte170m", "smpte170m", "smpte170m", "tv")
    c = colorimetry.default_colorimetry(video(352, 240, "30/1"))
    assert c.matrix == "smpte170m"
    for w, h in ((1920, 1080), (1440, 1080), (1280, 720)):
        c = colorimetry.default_colorimetry(video(w, h, "25/1"))
        assert (c.matrix, c.primaries, c.transfer) == ("bt709", "bt709", "bt709")
    # unusual rate: decide by raster
    assert colorimetry.default_colorimetry(video(720, 480, "24000/1001")).matrix == "smpte170m"
    assert colorimetry.default_colorimetry(video(720, 576, "24000/1001")).matrix == "bt470bg"
    assert colorimetry.default_colorimetry(video(320, 240, "15/1")).matrix == "smpte170m"


def test_range_pc_carried() -> None:
    v = video(720, 576, "25/1", range="pc")
    assert colorimetry.default_colorimetry(v).range == "pc"
    assert colorimetry.effective_colorimetry(v).range == "pc"
    v = video(720, 576, "25/1", range="unknown")
    assert colorimetry.effective_colorimetry(v).range == "tv"


def test_effective_complete_tags_win() -> None:
    v = video(720, 576, "25/1", matrix="smpte170m", primaries="smpte170m", transfer="smpte170m", range="tv")
    c = colorimetry.effective_colorimetry(v)
    assert (c.matrix, c.primaries, c.transfer, c.source) == ("smpte170m", "smpte170m", "smpte170m", "tags")


def test_effective_partial_tags_filled_from_defaults() -> None:
    v = video(720, 576, "25/1", matrix="bt709", primaries="unknown", transfer="")
    c = colorimetry.effective_colorimetry(v)
    assert (c.matrix, c.primaries, c.transfer, c.source) == ("bt709", "bt470bg", "bt709", "default")
    v = video(1920, 1080, "25/1", matrix="unspecified", primaries="reserved", transfer="unknown")
    c = colorimetry.effective_colorimetry(v)
    assert (c.matrix, c.primaries, c.transfer, c.source) == ("bt709", "bt709", "bt709", "default")


def test_effective_override() -> None:
    v = video(720, 576, "25/1")
    c = colorimetry.effective_colorimetry(v, Colorimetry(matrix="smpte170m", primaries="smpte170m", transfer="smpte170m", range="pc"))
    assert (c.matrix, c.primaries, c.transfer, c.range, c.source) == ("smpte170m", "smpte170m", "smpte170m", "pc", "override")
    # partial override keeps the rest from tags/defaults
    c = colorimetry.effective_colorimetry(v, Colorimetry(transfer="smpte170m", range="bogus"))
    assert (c.matrix, c.primaries, c.transfer, c.range, c.source) == ("bt470bg", "bt470bg", "smpte170m", "tv", "override")


def test_setparams_filter() -> None:
    c = colorimetry.default_colorimetry(video(720, 576, "25/1"))
    assert colorimetry.setparams_filter(c) == "setparams=colorspace=bt470bg:color_primaries=bt470bg:color_trc=bt709:range=tv"
    c = Colorimetry("bt709", "bt709", "bt709", "pc")
    assert colorimetry.setparams_filter(c).endswith(":range=pc")
    assert colorimetry.setparams_filter(Colorimetry("bt709", "bt709", "bt709", "weird")).endswith(":range=tv")


@pytest.mark.needs_sandbox
def test_setparams_filter_accepted_by_ffmpeg(tmp_path) -> None:
    import subprocess

    for c in (colorimetry.PAL_SD, colorimetry.NTSC_SD, colorimetry.HD):
        out = tmp_path / f"{c.matrix}.mkv"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:size=64x48:rate=25", "-frames:v", "2",
             "-vf", colorimetry.setparams_filter(c), "-c:v", "ffv1", str(out)],
            check=True,
        )
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=color_space,color_primaries,color_transfer,color_range", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert probe == f"{c.range},{c.matrix},{c.transfer},{c.primaries}" or set(probe.split(",")) == {c.range, c.matrix, c.transfer, c.primaries}
