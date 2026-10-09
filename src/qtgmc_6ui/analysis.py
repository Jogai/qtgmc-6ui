"""Analysis orchestration (WP-A): probe -> idet windows -> telecine confirmation -> classify.

Emits stages probe/idet/fieldmatch/classify and progress per window.
Results are cached in paths.analysis_cache_dir() keyed by (path, size, mtime, schema).
"""

from __future__ import annotations

from .events import Emitter
from .models import AnalysisResult


def analyze(
    path: str,
    *,
    emitter: Emitter | None = None,
    full_scan: bool = False,
    windows: int | None = None,
    window_frames: int | None = None,
    use_cache: bool = True,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> AnalysisResult:
    raise NotImplementedError


def cache_key(path: str) -> str:
    raise NotImplementedError


def load_cached(path: str) -> AnalysisResult | None:
    raise NotImplementedError


def store_cached(result: AnalysisResult) -> None:
    raise NotImplementedError
