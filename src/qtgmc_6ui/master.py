"""Archival master ffmpeg command (WP-B).  Fixed recipe:

  ffmpeg -hide_banner -nostdin -loglevel info -stats -stats_period 1 -y
         -i pipe:0 [-itsoffset -V] -i ORIGINAL -map 0:v:0 -map 1:a?
         -vf setsar=NUM/DEN,setparams=colorspace=..:color_primaries=..:color_trc=..:range=..[:chroma_location=..]
         -color_range tv|pc [-chroma_sample_location N]
         -c:v ffv1 -level 3 -g 1 -slices 16 -slicecrc 1
         -c:a flac -compression_level 8 [-strict experimental]
         -f matroska OUT

Audio comes straight from the original (all tracks, ``1:a?``); video pix_fmt is
whatever y4m carries (source format, no conversion).  SAR and colour tags go through
the filter chain because y4m carries neither and ``-color_primaries``/``-color_trc``
output options are silently dropped for ffv1 in mkv (research/arch-facts.md §0.5/§0.6).
``-color_range`` IS passed as an output option as well: without it ffmpeg n9 inserts an
auto_scale pc->tv conversion for full-range (yuvj) sources and alters the pixels
(reviews/core.md B1).  ``-itsoffset -V`` on the ORIGINAL keeps the A/V start offset when the
original's video starts V seconds after its earliest stream: the y4m video starts at 0, the
audio is moved to -V and the muxer shifts both back to >= 0 (reviews/core.md M1).  The offset
is applied to the original rather than to the y4m input because -itsoffset is quantised to the
input's timebase (1/fps for y4m = 20 ms at 50 fps; 1/90000 for the original).
"""

from __future__ import annotations

import re
from fractions import Fraction

from .models import Colorimetry

FFV1_ARGS: tuple[str, ...] = ("-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "16", "-slicecrc", "1")
FLAC_ARGS: tuple[str, ...] = ("-c:a", "flac", "-compression_level", "8")

CHROMA_LOCATIONS: tuple[str, ...] = ("left", "center", "topleft", "top", "bottomleft", "bottom")
_CHROMA_OPTION = {name: i + 1 for i, name in enumerate(CHROMA_LOCATIONS)}  # ffmpeg -chroma_sample_location ints

_UNKNOWN = {"", "unknown", "unspecified", "reserved"}
_PROGRESS = re.compile(r"(\w+)=\s*([^\s]+)")


def normalize_range(r: str) -> str:
    """'tv' | 'pc' | '' from ffmpeg/VS vocabulary."""
    return {"tv": "tv", "limited": "tv", "mpeg": "tv", "pc": "pc", "full": "pc", "jpeg": "pc"}.get((r or "").lower(), "")


def setparams_filter(c: Colorimetry, chroma_location: str = "") -> str:
    """``setparams=colorspace=..:color_primaries=..:color_trc=..:range=..[:chroma_location=..]``
    (unknown tags left out).

    Kept local on purpose: the master recipe is fixed and must not depend on how
    colorimetry.py renders tags for viewing copies."""
    parts = []
    if c.matrix not in _UNKNOWN:
        parts.append(f"colorspace={c.matrix}")
    if c.primaries not in _UNKNOWN:
        parts.append(f"color_primaries={c.primaries}")
    if c.transfer not in _UNKNOWN:
        parts.append(f"color_trc={c.transfer}")
    rng = normalize_range(c.range)
    if rng:
        parts.append(f"range={rng}")
    if chroma_location in CHROMA_LOCATIONS:
        parts.append(f"chroma_location={chroma_location}")
    return "setparams=" + ":".join(parts) if parts else ""


def video_filter(sar: Fraction, colorimetry: Colorimetry, chroma_location: str = "") -> str:
    sar = Fraction(sar) if sar and sar > 0 else Fraction(1, 1)
    chain = [f"setsar={sar.numerator}/{sar.denominator}"]
    sp = setparams_filter(colorimetry, chroma_location)
    if sp:
        chain.append(sp)
    return ",".join(chain)


def master_command(
    original: str,
    output: str,
    sar: Fraction,
    colorimetry: Colorimetry,
    *,
    overwrite: bool = False,
    ffmpeg: str = "ffmpeg",
    audio_tracks: int = -1,
    chroma_location: str = "",
    video_offset_s: float = 0.0,
    flac_32bit: bool = False,
) -> list[str]:
    """``audio_tracks``: -1 = every audio track of the original, 0 = none, n = the first n.
    ``video_offset_s``: how much later than the original's earliest stream its video starts
    (the original is shifted by -offset so the A/V offset survives, see the module docstring).
    ``flac_32bit``: the original has 32-bit integer PCM; FLAC keeps 32 bits with -strict experimental."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info", "-stats", "-stats_period", "1",
           "-y" if overwrite else "-n", "-i", "pipe:0"]
    if video_offset_s > 0 and audio_tracks != 0:
        cmd += ["-itsoffset", f"{-video_offset_s:.6f}"]
    cmd += ["-i", original, "-map", "0:v:0"]
    if audio_tracks < 0:
        cmd += ["-map", "1:a?"]
    else:
        for i in range(audio_tracks):
            cmd += ["-map", f"1:a:{i}"]
    cmd += ["-vf", video_filter(sar, colorimetry, chroma_location)]
    rng = normalize_range(colorimetry.range) or "tv"
    cmd += ["-color_range", rng]
    if chroma_location in _CHROMA_OPTION:
        cmd += ["-chroma_sample_location", str(_CHROMA_OPTION[chroma_location])]
    cmd += list(FFV1_ARGS)
    if audio_tracks != 0:
        cmd += list(FLAC_ARGS)
        if flac_32bit:
            cmd += ["-strict", "experimental"]
    cmd += ["-f", "matroska", output]
    return cmd


def parse_ffmpeg_progress(line: str) -> dict[str, str] | None:
    """Parse a ``frame=  519 fps=... time=...`` stats line (\\r-separated) into a dict, else None."""
    s = line.strip()
    if not s.startswith("frame="):
        return None
    d = dict(_PROGRESS.findall(s))
    return d or None
