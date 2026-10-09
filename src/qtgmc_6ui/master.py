"""Archival master ffmpeg command (WP-B).  Fixed recipe:

  ffmpeg -hide_banner -nostdin -loglevel info -stats_period 0.5 -y|-n
         -i pipe:0 -i ORIGINAL -map 0:v:0 -map 1:a?
         -vf setsar=NUM/DEN,setparams=colorspace=..:color_primaries=..:color_trc=..:range=..
         -c:v ffv1 -level 3 -g 1 -slices 16 -slicecrc 1
         -c:a flac -compression_level 8
         -f matroska OUT.mkv

Audio comes straight from the original (all tracks, ``1:a?``); video pix_fmt is
whatever y4m carries (source format, no conversion).
"""

from __future__ import annotations

from fractions import Fraction

from .models import Colorimetry


def master_command(
    original: str,
    output: str,
    sar: Fraction,
    colorimetry: Colorimetry,
    *,
    overwrite: bool = False,
    ffmpeg: str = "ffmpeg",
    audio_tracks: int = -1,
) -> list[str]:
    raise NotImplementedError


def parse_ffmpeg_progress(line: str) -> dict[str, str] | None:
    """Parse a ``frame=  519 fps=... time=...`` stats line (\\r-separated) into a dict, else None."""
    raise NotImplementedError
