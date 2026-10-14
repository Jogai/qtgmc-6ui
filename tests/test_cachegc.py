"""cachegc: LRU-by-mtime pruning with age/size/count caps, owned-files-only, races, throttling,
and the touch-on-hit of the caches that use it."""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from qtgmc_6ui import cachegc

NOW = time.time()


def _mk(d: Path, name: str, age_s: float, size: int = 10) -> Path:
    p = d / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (NOW - age_s, NOW - age_s))
    return p


def _names(d: Path) -> list[str]:
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() or p.is_symlink())


def test_lru_by_mtime_and_touch_on_hit(tmp_path: Path) -> None:
    for i, age in enumerate((50, 40, 30, 20, 10)):
        _mk(tmp_path, f"f{i}.json", age)
    os.utime(tmp_path / "f0.json", (NOW, NOW))  # a cache hit renews the oldest one
    removed = cachegc.prune(tmp_path, "*.json", max_files=3, now=NOW)
    assert sorted(p.name for p in removed) == ["f1.json", "f2.json"]
    assert _names(tmp_path) == ["f0.json", "f3.json", "f4.json"]


def test_touch_sets_mtime_now(tmp_path: Path) -> None:
    p = _mk(tmp_path, "a.png", 10_000)
    cachegc.touch(p)
    assert time.time() - p.stat().st_mtime < 5
    cachegc.touch(tmp_path / "missing.png")  # no error


def test_age_size_and_count_caps(tmp_path: Path) -> None:
    _mk(tmp_path, "old.json", 100 * 86400)
    for i in range(5):
        _mk(tmp_path, f"n{i}.json", 100 - i, size=100)
    cachegc.prune(tmp_path, "*.json", max_age_s=30 * 86400, now=NOW)
    assert "old.json" not in _names(tmp_path)
    cachegc.prune(tmp_path, "*.json", max_bytes=300, now=NOW)  # 5 x 100 bytes -> 3 newest
    assert _names(tmp_path) == ["n2.json", "n3.json", "n4.json"]
    cachegc.prune(tmp_path, "*.json", max_files=1, now=NOW)
    assert _names(tmp_path) == ["n4.json"]


def test_only_owned_files_and_keep(tmp_path: Path) -> None:
    _mk(tmp_path, "a.png", 99_999)
    _mk(tmp_path, "notes.txt", 99_999)
    _mk(tmp_path, "keep.png", 99_999)
    (tmp_path / "sub").mkdir()
    _mk(tmp_path / "sub", "b.png", 99_999)
    removed = cachegc.prune(tmp_path, "*.png", max_age_s=1, keep=[tmp_path / "keep.png"], now=NOW)
    assert [p.name for p in removed] == ["a.png"]
    assert _names(tmp_path) == ["keep.png", "notes.txt", "sub/b.png"]  # not recursive by default


def test_recursive_never_leaves_the_dir(tmp_path: Path) -> None:
    root, outside = tmp_path / "bs", tmp_path / "outside"
    _mk(root / "a" / "b", "x.avi.0.bsindex", 99_999)
    _mk(outside, "y.avi.0.bsindex", 99_999)
    (root / "link").symlink_to(outside, target_is_directory=True)
    cachegc.prune(root, "*.bsindex", recursive=True, max_age_s=1, now=NOW)
    assert not (root / "a").exists()  # emptied dirs removed
    assert (outside / "y.avi.0.bsindex").exists() and (root / "link").is_symlink()


def test_dangling_links(tmp_path: Path) -> None:
    target = _mk(tmp_path / "t", "real.avi", 0)
    links = tmp_path / "links"
    links.mkdir()
    (links / "ok.avi").symlink_to(target)
    (links / "gone.avi").symlink_to(tmp_path / "t" / "deleted.avi")
    cachegc.prune(links, "*", dangling_links=True, max_age_s=10 * 86400)
    assert _names(links) == ["ok.avi"] and target.exists()


def test_file_vanishing_mid_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(4):
        _mk(tmp_path, f"f{i}.json", 10 - i)
    real_unlink = Path.unlink

    def racing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == "f0.json":  # another process got there first
            real_unlink(self)
            raise FileNotFoundError(self)
        if self.name == "f1.json":
            raise PermissionError(self)
        real_unlink(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", racing_unlink)
    removed = cachegc.prune(tmp_path, "*.json", max_files=1, now=NOW)
    assert [p.name for p in removed] == ["f2.json"]
    assert _names(tmp_path) == ["f1.json", "f3.json"]  # f1 could not be removed; nothing raised
    assert cachegc.prune(tmp_path / "missing", "*") == []


def _prune_worker(d: str) -> None:
    for _ in range(20):
        cachegc.prune(d, "*.json", max_files=10)


def test_concurrent_prunes(tmp_path: Path) -> None:
    for i in range(200):
        _mk(tmp_path, f"f{i:03}.json", 200 - i)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            _prune_worker(str(tmp_path))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    procs = [multiprocessing.get_context("spawn").Process(target=_prune_worker, args=(str(tmp_path),)) for _ in range(2)]
    for t in threads + procs:
        t.start()
    for t in threads:
        t.join()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    assert not errors
    assert _names(tmp_path) == [f"f{i:03}.json" for i in range(190, 200)]  # the 10 newest


def test_throttle_stamp(tmp_path: Path) -> None:
    _mk(tmp_path, "a.json", 99_999)
    assert cachegc.prune(tmp_path, "*.json", max_age_s=1, min_interval_s=3600)
    _mk(tmp_path, "b.json", 99_999)
    assert cachegc.prune(tmp_path, "*.json", max_age_s=1, min_interval_s=3600) == []  # pruned recently
    assert (tmp_path / cachegc.STAMP).exists() and (tmp_path / "b.json").exists()


def test_tmp_leftovers(tmp_path: Path) -> None:
    _mk(tmp_path, ".x.json.0a1b2c3d.tmp", 2 * 86400)
    _mk(tmp_path, ".y.json.0a1b2c3e.tmp", 10)  # a writer may still be busy
    cachegc.prune_tmp(tmp_path, now=NOW)
    assert _names(tmp_path) == [".y.json.0a1b2c3e.tmp"]


def test_ffmpeg_caches_keep_newest_and_current(isolated_dirs) -> None:
    from qtgmc_6ui import paths

    d = paths.mux_matrix_cache_dir()
    now = time.time()
    for i in range(6):
        for kind in ("matrix", "encoders"):
            p = _mk(d, f"{kind}-n{i}.json", 0)
            os.utime(p, (now - 100 + i, now - 100 + i))
    cachegc.prune_ffmpeg_caches([d / "matrix-n0.json"])
    assert _names(d) == ["encoders-n3.json", "encoders-n4.json", "encoders-n5.json",
                         "matrix-n0.json", "matrix-n3.json", "matrix-n4.json", "matrix-n5.json"]


def test_bs_index_touch_and_layout(isolated_dirs, tmp_path: Path) -> None:
    from qtgmc_6ui import paths

    src = tmp_path / "in dir" / "clip [1].avi"
    idx = paths.bs_index_dir().joinpath(*src.parts[1:-1]) / "clip [1].avi.0.bsindex"
    _mk(idx.parent, idx.name, 99_999)
    cachegc.touch_bs_index(str(src))
    assert time.time() - idx.stat().st_mtime < 5


def test_analysis_cache_hit_touches_and_store_prunes(isolated_dirs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui import analysis, paths
    from qtgmc_6ui.models import AnalysisResult

    clip = tmp_path / "c.avi"
    clip.write_bytes(b"x")
    analysis.store_cached(AnalysisResult(input=str(clip)))
    p = analysis.cache_path(str(clip))
    os.utime(p, (NOW, NOW))
    assert analysis.load_cached(str(clip)) is not None
    assert time.time() - p.stat().st_mtime < 5
    d = paths.analysis_cache_dir()
    _mk(d, "0" * 64 + ".json", 400 * 86400)  # an entry for a long gone / changed input
    (d / cachegc.STAMP).unlink(missing_ok=True)
    analysis.store_cached(AnalysisResult(input=str(clip)))
    assert not (d / ("0" * 64 + ".json")).exists() and p.exists()


@pytest.mark.gui
def test_gui_preview_cache_hit_touches(isolated_dirs, qapp) -> None:
    from qtgmc_6ui import preview
    from qtgmc_6ui.gui import preview_widget

    p = preview_widget.preview_cache() / "hit.png"
    preview.write_png(str(p), 1, 1, [b"\x00\x00\x00"])
    os.utime(p, (NOW - 10 * 86400, NOW - 10 * 86400))
    assert not preview_widget.load_cached(str(p)).isNull()
    assert time.time() - p.stat().st_mtime < 5
    assert preview_widget.load_cached(str(p.with_name("none.png"))).isNull()
    for i in range(3):
        _mk(p.parent, f"big{i}.png", 1000 - i, size=1000)
    preview_widget.prune_preview_cache(limit=2100)
    assert sorted(x.name for x in p.parent.glob("*.png")) == ["big1.png", "big2.png", "hit.png"]


def test_viewcopy_stale_temps_of_dead_processes(tmp_path: Path) -> None:
    import subprocess
    import sys

    from qtgmc_6ui import viewcopy

    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True).stdout.strip()
    partial = viewcopy.partial_path(str(tmp_path / "v.mp4"))
    stale = tmp_path / f".{Path(partial).name}.tmp{dead}.video.hevc"
    live = tmp_path / f".{Path(partial).name}.tmp{os.getpid()}.video.hevc"
    other = tmp_path / ".other.mp4.partial.tmp1.video.hevc"
    for f in (stale, live, other):
        f.write_bytes(b"x")
    assert viewcopy._stale_temps(partial) == [str(stale)]
