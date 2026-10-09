"""Container x codec validity by probe-mux (WP-C).

A static table is wrong for this ffmpeg (mp4 takes FLAC/Opus/PCM; mov rejects
them), so validity is established by a 0.1 s test mux per (muxer, encoder):
  ffmpeg -f lavfi -i testsrc2=size=64x48:rate=25 -t 0.1 -c:v ENC [-pix_fmt ..] -f MUXER /tmp/x.EXT
  ffmpeg -f lavfi -i sine=r=48000 -t 0.1 -c:a ENC -f MUXER ...
Verdict: rc 0 and non-empty output -> OK; stderr contains "may be unplayable" or
"private data stream" -> OK_WITH_WARNING; else FAIL (note = stderr tail).
Cached per ffmpeg version in paths.mux_matrix_cache_dir()/matrix-<version>.json;
``data/mux_seed.json`` (the research matrix for n9.0.2) seeds the cache so the
first start is instant when the version matches.
"""

from __future__ import annotations

from .models import MuxProbe, MuxVerdict


def probe(container: str, encoder: str, *, kind: str, ffmpeg: str = "ffmpeg") -> MuxProbe:
    """kind: 'video' | 'audio'.  Uses the cache; probes on demand."""
    raise NotImplementedError


def verdict(container: str, encoder: str, *, kind: str) -> MuxVerdict:
    raise NotImplementedError


def reason(container: str, encoder: str, *, kind: str) -> str:
    """Short text for a greyed-out option ("mov does not accept FLAC: ...")."""
    raise NotImplementedError


def probe_all(containers: list[str], video: list[str], audio: list[str], ffmpeg: str = "ffmpeg") -> list[MuxProbe]:
    """Bulk probe (``qtgmc-6ui encoders --probe``)."""
    raise NotImplementedError


def seed_from_package_data() -> None:
    raise NotImplementedError
