"""Colour tag defaults by resolution / rate family (WP-A).

Defaults (ffmpeg vocabulary) when the source carries no usable tags:
  SD PAL  (height <= 625 and fps family 25)      matrix bt470bg  primaries bt470bg  transfer bt709
  SD NTSC (height <= 525 and fps family 30)      matrix smpte170m primaries smpte170m transfer smpte170m
  HD      (height > 625)                         matrix bt709    primaries bt709    transfer bt709
Range: tv unless the source is tagged pc.  Transfer for PAL is bt709 on purpose:
ffmpeg's "bt470bg" transfer means gamma 2.8 (BT.470 System B/G display), not the
BT.601 OETF, which is identical to bt709/smpte170m.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

from .models import Colorimetry, VideoProbe

UNKNOWN = {"", "unknown", "unspecified", "reserved"}

PAL_SD = Colorimetry(matrix="bt470bg", primaries="bt470bg", transfer="bt709", range="tv", source="default")
NTSC_SD = Colorimetry(matrix="smpte170m", primaries="smpte170m", transfer="smpte170m", range="tv", source="default")
HD = Colorimetry(matrix="bt709", primaries="bt709", transfer="bt709", range="tv", source="default")

HD_MIN_HEIGHT = 626  # above the 625-line PAL raster
HD_MIN_WIDTH = 1280
NTSC_MAX_HEIGHT = 525

_PAL_RATES = (Fraction(25), Fraction(50))
_NTSC_RATES = (Fraction(30000, 1001), Fraction(30), Fraction(60000, 1001), Fraction(60))
_RATE_TOLERANCE = Fraction(1, 100)


def is_hd(width: int, height: int) -> bool:
    return height >= HD_MIN_HEIGHT or width >= HD_MIN_WIDTH


def _near(a: Fraction, b: Fraction) -> bool:
    return abs(a - b) <= _RATE_TOLERANCE


def rate_family(fps: Fraction) -> str:
    """'pal' for 25/50, 'ntsc' for 30000/1001, 30, 60000/1001, 60; 'other' otherwise."""
    try:
        f = Fraction(fps)
    except (TypeError, ValueError, ZeroDivisionError):
        return "other"
    if f <= 0:
        return "other"
    if any(_near(f, r) for r in _PAL_RATES):
        return "pal"
    if any(_near(f, r) for r in _NTSC_RATES):
        return "ntsc"
    return "other"


def _known(v: str | None) -> bool:
    return bool(v) and str(v).strip().lower() not in UNKNOWN


def _range_of(video: VideoProbe) -> str:
    r = (video.color_tags.range or "").strip().lower()
    return "pc" if r in ("pc", "jpeg", "full") else "tv"


def default_colorimetry(video: VideoProbe) -> Colorimetry:
    if is_hd(video.width, video.height):
        base = HD
    else:
        fam = rate_family(video.fps)
        if fam == "pal":
            base = PAL_SD
        elif fam == "ntsc":
            base = NTSC_SD
        else:  # unusual rate: decide by the raster
            base = NTSC_SD if 0 < video.height <= NTSC_MAX_HEIGHT else PAL_SD
    return replace(base, range=_range_of(video), source="default")


def effective_colorimetry(video: VideoProbe, override: Colorimetry | None = None) -> Colorimetry:
    """Tags from the file when all three of matrix/primaries/transfer are known, else defaults.

    Partially tagged sources take the known tags and fill the rest from the defaults
    (source="tags" only when nothing was filled in).
    """
    defaults = default_colorimetry(video)
    tags = video.color_tags
    matrix = tags.matrix if _known(tags.matrix) else defaults.matrix
    primaries = tags.primaries if _known(tags.primaries) else defaults.primaries
    transfer = tags.transfer if _known(tags.transfer) else defaults.transfer
    complete = _known(tags.matrix) and _known(tags.primaries) and _known(tags.transfer)
    eff = Colorimetry(matrix, primaries, transfer, defaults.range, "tags" if complete else "default")
    if override is None:
        return eff
    return Colorimetry(
        matrix=override.matrix if _known(override.matrix) else eff.matrix,
        primaries=override.primaries if _known(override.primaries) else eff.primaries,
        transfer=override.transfer if _known(override.transfer) else eff.transfer,
        range=override.range if override.range in ("tv", "pc") else eff.range,
        source="override",
    )


def setparams_filter(c: Colorimetry) -> str:
    """``setparams=colorspace=..:color_primaries=..:color_trc=..:range=..`` (works where
    ``-color_primaries``/``-color_trc`` output options are silently dropped)."""
    rng = c.range if c.range in ("tv", "pc") else "tv"
    return f"setparams=colorspace={c.matrix}:color_primaries={c.primaries}:color_trc={c.transfer}:range={rng}"
