"""Bounded caches: one pruning helper (LRU by mtime) and the limits of every cache the app writes.

Cache locations (all under paths.cache_dir(), i.e. QTGMC6UI_CACHE_DIR or the platform cache dir):
  analysis/*.json          analysis results, keyed by path/size/mtime/schema; a changed or deleted
                           input orphans its entry -> age, count and size caps, touched on a hit
  bsindex/**/*.bsindex     bestsource indexes (bestsource never deletes them) -> age and size caps,
                           touched when a source is opened
  muxmatrix/{matrix,encoders}-<ffmpeg build>.json
                           one file per ffmpeg build -> the newest few per kind survive
  links/*                  ASCII aliases for non-UTF-8 input names -> dangling links and old links go
  preview/gui/*.png        GUI preview frames -> size and age caps, touched on a cache hit
  gui-jobs/job-*.json      JobSpec files the GUI hands to ``run-job`` -> age and count caps
  in every dir: ``.<name>.<hex>.tmp`` leftovers of an interrupted osdep.atomic_write_* (age cap)

Pruning is throttled per directory with a ``.pruned`` stamp (at most once per
PRUNE_INTERVAL_S, so it never slows a run down noticeably), never follows symlinks out of the
directory, only removes files matching the cache's own name pattern, and tolerates files that
vanish or cannot be removed (another process pruning or reading at the same time).
"""

from __future__ import annotations

import fnmatch
import os
import time
from collections.abc import Iterable
from pathlib import Path

DAY = 86400.0
PRUNE_INTERVAL_S = 3600.0  # per directory, shared by all processes through the stamp file
STAMP = ".pruned"
TMP_PATTERN = ".*.tmp"  # osdep.atomic_write_* temp files
TMP_MAX_AGE_S = DAY

ANALYSIS_LIMITS = {"max_age_s": 180 * DAY, "max_files": 5000, "max_bytes": 64 * 2**20}
BS_INDEX_LIMITS = {"max_age_s": 180 * DAY, "max_bytes": 1024 * 2**20}
FFMPEG_CACHE_KEEP = 3  # newest matrix-*/encoders-* files (ffmpeg builds) kept per kind
LINKS_MAX_AGE_S = 30 * DAY
PREVIEW_LIMITS = {"max_age_s": 30 * DAY, "max_bytes": 500 * 2**20}
GUI_JOBS_LIMITS = {"max_age_s": 7 * DAY, "max_files": 200}


def touch(path: str | os.PathLike[str]) -> None:
    """Mark a cache entry as used (mtime = now) so the LRU order is real; ignores errors."""
    try:
        os.utime(path)
    except OSError:
        pass


def _due(directory: Path, min_interval_s: float, now: float) -> bool:
    """True (and the stamp renewed) when this directory was not pruned within the interval."""
    stamp = directory / STAMP
    try:
        if now - stamp.stat().st_mtime < min_interval_s:
            return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        stamp.touch()
        os.utime(stamp, (now, now))
    except OSError:
        return False
    return True


def prune(
    directory: str | os.PathLike[str],
    pattern: str = "*",
    *,
    max_age_s: float | None = None,
    max_bytes: int | None = None,
    max_files: int | None = None,
    keep: Iterable[str | os.PathLike[str]] = (),
    recursive: bool = False,
    dangling_links: bool = False,
    min_interval_s: float = 0.0,
    now: float | None = None,
) -> list[Path]:
    """Delete owned files (name matches ``pattern``) in ``directory``, least recently modified
    first, until none is older than ``max_age_s`` and at most ``max_files`` / ``max_bytes`` remain.
    ``keep`` is never deleted; ``dangling_links`` also removes broken symlinks; ``recursive``
    walks subdirectories (never through symlinks) and removes those left empty.  Returns the
    removed paths.  Never raises for files that vanish or cannot be removed."""
    root = Path(directory)
    now = time.time() if now is None else now
    if not root.is_dir() or (min_interval_s and not _due(root, min_interval_s, now)):
        return []
    kept = {os.path.abspath(k) for k in keep}
    entries: list[tuple[float, int, Path]] = []
    dirs: list[Path] = []
    removed: list[Path] = []

    def scan(d: Path) -> None:
        try:
            it = list(os.scandir(d))
        except OSError:
            return
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False):
                    if recursive:
                        dirs.append(Path(e.path))
                        scan(Path(e.path))
                    continue
                if e.name == STAMP or not fnmatch.fnmatchcase(e.name, pattern) or os.path.abspath(e.path) in kept:
                    continue
                if dangling_links and e.is_symlink() and not os.path.exists(e.path):
                    if _remove(Path(e.path)):
                        removed.append(Path(e.path))
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:  # vanished while scanning
                continue
            entries.append((st.st_mtime, st.st_size, Path(e.path)))

    scan(root)
    entries.sort(key=lambda x: x[0])  # oldest first
    total = sum(size for _, size, _ in entries)
    count = len(entries)
    for mtime, size, path in entries:
        too_old = max_age_s is not None and now - mtime > max_age_s
        too_many = max_files is not None and count > max_files
        too_big = max_bytes is not None and total > max_bytes
        if not (too_old or too_many or too_big):
            continue
        if _remove(path):
            removed.append(path)
        count -= 1  # also when another process removed it first
        total -= size
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return removed


def _remove(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def prune_tmp(directory: str | os.PathLike[str], *, now: float | None = None) -> list[Path]:
    """Leftover atomic-write temp files older than a day (a live writer needs milliseconds)."""
    return prune(directory, TMP_PATTERN, max_age_s=TMP_MAX_AGE_S, now=now)


# ---------------------------------------------------------------------- per-cache entry points
def _throttled(directory: Path, min_interval_s: float) -> bool:
    return directory.is_dir() and (not min_interval_s or _due(directory, min_interval_s, time.time()))


def prune_analysis_cache(*, min_interval_s: float = PRUNE_INTERVAL_S) -> list[Path]:
    """Called after an analysis result is stored."""
    from . import paths

    d = paths.analysis_cache_dir()
    if not _throttled(d, min_interval_s):
        return []
    return prune(d, "*.json", **ANALYSIS_LIMITS) + prune_tmp(d)


def prune_bs_index(*, min_interval_s: float = PRUNE_INTERVAL_S) -> list[Path]:
    """Called when a source is opened (after touch_bs_index)."""
    from . import paths

    d = paths.bs_index_dir()
    if not _throttled(d, min_interval_s):
        return []
    return prune(d, "*.bsindex", recursive=True, **BS_INDEX_LIMITS)


def touch_bs_index(source: str) -> None:
    """Mark bestsource's index of ``source`` as used (bestsource stores it under the cache dir at
    the source's absolute path, ``<name>.<track>.bsindex``)."""
    from . import paths

    p = Path(os.path.abspath(source))
    d = paths.bs_index_dir().joinpath(*p.parts[1:-1])
    try:
        for f in d.glob(f"{_glob_escape(p.name)}.*.bsindex"):
            touch(f)
    except OSError:
        pass


def _glob_escape(name: str) -> str:
    return "".join(f"[{c}]" if c in "*?[" else c for c in name)


def prune_ffmpeg_caches(current: Iterable[str | os.PathLike[str]] = ()) -> list[Path]:
    """Called when a matrix/encoders cache file is written: drops those of old ffmpeg builds
    (the newest FFMPEG_CACHE_KEEP per kind and ``current`` stay)."""
    from . import paths

    d = paths.mux_matrix_cache_dir()
    out: list[Path] = []
    for kind in ("matrix-*.json", "encoders-*.json"):
        out += prune(d, kind, max_files=FFMPEG_CACHE_KEEP, keep=current)
    return out + prune_tmp(d)


def prune_links(*, min_interval_s: float = PRUNE_INTERVAL_S) -> list[Path]:
    """Called when an alias is created: dangling aliases and those older than LINKS_MAX_AGE_S."""
    from . import paths

    d = paths.cache_dir() / "links"
    if not _throttled(d, min_interval_s):
        return []
    return prune(d, "*", dangling_links=True, max_age_s=LINKS_MAX_AGE_S)


def prune_preview_cache(directory: str | os.PathLike[str], *, max_bytes: int | None = None) -> list[Path]:
    """GUI preview PNGs (run off the GUI thread at startup)."""
    limits = dict(PREVIEW_LIMITS)
    if max_bytes is not None:
        limits["max_bytes"] = max_bytes
    return prune(directory, "*.png", **limits) + prune_tmp(directory)


def prune_gui_jobs(directory: str | os.PathLike[str], keep: Iterable[str | os.PathLike[str]] = ()) -> list[Path]:
    """GUI job files, after a new one is written (``keep`` = the one about to run)."""
    return prune(directory, "job-*.json", keep=keep, **GUI_JOBS_LIMITS) + prune_tmp(directory)
