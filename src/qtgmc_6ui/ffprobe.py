"""ffprobe wrapper (WP-A).  Container/stream facts only; never trusted for field order."""

from __future__ import annotations

from .models import ProbeInfo


def probe(path: str, ffprobe: str = "ffprobe") -> ProbeInfo:
    """Run ``ffprobe -v error -print_format json -show_format -show_streams`` and map it.

    Raises InputError(INPUT_NOT_FOUND / INPUT_UNREADABLE / INPUT_NO_VIDEO).
    ``nb_frames`` is exact when the container reports it, else estimated from
    duration * fps with ``nb_frames_exact=False``.
    """
    raise NotImplementedError


def count_frames(path: str, ffprobe: str = "ffprobe") -> int:
    """Exact frame count (``-count_frames``); slow on long files, used by tests/verify."""
    raise NotImplementedError


def ffmpeg_version(ffmpeg: str = "ffmpeg") -> str:
    """First token after 'ffmpeg version' (e.g. 'n9.0.2')."""
    raise NotImplementedError
