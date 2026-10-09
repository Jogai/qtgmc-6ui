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

from fractions import Fraction

from .models import Colorimetry, VideoProbe

UNKNOWN = {"", "unknown", "unspecified", "reserved"}


def is_hd(width: int, height: int) -> bool:
    raise NotImplementedError


def rate_family(fps: Fraction) -> str:
    """'pal' for 25/50, 'ntsc' for 30000/1001, 30, 60000/1001, 60; 'other' otherwise."""
    raise NotImplementedError


def default_colorimetry(video: VideoProbe) -> Colorimetry:
    raise NotImplementedError


def effective_colorimetry(video: VideoProbe, override: Colorimetry | None = None) -> Colorimetry:
    """Tags from the file when all three of matrix/primaries/transfer are known, else defaults.

    Partially tagged sources take the known tags and fill the rest from the defaults
    (source="tags" only when nothing was filled in).
    """
    raise NotImplementedError


def setparams_filter(c: Colorimetry) -> str:
    """``setparams=colorspace=..:color_primaries=..:color_trc=..:range=..`` (works where
    ``-color_primaries``/``-color_trc`` output options are silently dropped)."""
    raise NotImplementedError
