"""muxmatrix: seed, verdict classification, reasons; real probe muxes in the sandbox."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qtgmc_6ui import encoders, muxmatrix
from qtgmc_6ui.models import MuxVerdict

SEED_VERSION = "n9.0.2"


@pytest.fixture
def seeded(isolated_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the running ffmpeg is the researched n9.0.2 so the seed answers without probing."""
    muxmatrix.invalidate_cache()
    monkeypatch.setattr(encoders, "ffmpeg_version", lambda ffmpeg="ffmpeg": SEED_VERSION)
    monkeypatch.setattr(encoders, "ffmpeg_build_id", lambda ffmpeg="ffmpeg": SEED_VERSION + "-test")
    monkeypatch.setattr(muxmatrix, "run_probe", lambda *a, **k: pytest.fail("probe-mux executed although the seed has the answer"))
    yield
    muxmatrix.invalidate_cache()


def test_seed_file_is_consistent() -> None:
    d = json.loads(muxmatrix.SEED_PATH.read_text("utf-8"))
    assert d["schema"] == 1 and d["ffmpeg_version"] == SEED_VERSION
    rows = d["probes"]
    assert len(rows) > 150
    keys = {(r["container"], r["encoder"], r["kind"]) for r in rows}
    assert len(keys) == len(rows)
    assert all(r["verdict"] in {v.value for v in MuxVerdict} for r in rows)
    assert "mkv" not in {r["container"] for r in rows}  # muxer names, not extensions


def test_seed_verdicts(seeded: None) -> None:
    v = muxmatrix.verdict
    assert v("mp4", "flac", kind="audio") == MuxVerdict.OK
    assert v("mp4", "libopus", kind="audio") == MuxVerdict.OK
    assert v("mp4", "pcm_s16le", kind="audio") == MuxVerdict.OK
    assert v("mov", "flac", kind="audio") == MuxVerdict.FAIL
    assert v("mov", "libopus", kind="audio") == MuxVerdict.FAIL
    assert v("mov", "libvorbis", kind="audio") == MuxVerdict.FAIL
    assert v("mov", "ffv1", kind="video") == MuxVerdict.OK_WITH_WARNING
    assert v("mov", "libtheora", kind="video") == MuxVerdict.OK_WITH_WARNING
    # fooling cases (review M1): rc 0 but the file does not read back
    assert v("mpegts", "flac", kind="audio") == MuxVerdict.FAIL
    assert v("mpegts", "ffv1", kind="video") == MuxVerdict.FAIL
    assert v("mpegts", "prores_ks", kind="video") == MuxVerdict.FAIL
    assert v("avi", "libx265", kind="video") == MuxVerdict.FAIL
    assert v("mpegts", "libx264", kind="video") == MuxVerdict.OK
    assert v("mp4", "libx265", kind="video") == MuxVerdict.OK
    assert v("matroska", "libx265", kind="video") == MuxVerdict.OK
    assert v("webm", "libx264", kind="video") == MuxVerdict.FAIL
    assert v("webm", "libopus", kind="audio") == MuxVerdict.OK
    assert v("avi", "libopus", kind="audio") == MuxVerdict.FAIL
    assert v("ogg", "aac", kind="audio") == MuxVerdict.FAIL
    assert muxmatrix.cached("matroska", "unprobed_codec", kind="video").verdict == MuxVerdict.UNKNOWN


def test_reason_texts(seeded: None) -> None:
    assert muxmatrix.reason("mp4", "flac", kind="audio") == ""
    r = muxmatrix.reason("mov", "flac", kind="audio")
    assert r.startswith("mov does not accept flac") and "0x" not in r
    assert "muxer wrote nothing" in r
    r = muxmatrix.reason("mov", "ffv1", kind="video")
    assert "with a warning" in r and "may be unplayable" in r
    assert "reads back" in muxmatrix.reason("avi", "libx265", kind="video")
    assert "private data stream" in muxmatrix.reason("mpegts", "flac", kind="audio")


def test_classify_result() -> None:
    assert muxmatrix.classify_result(0, 10, "") == (MuxVerdict.OK, "")
    assert muxmatrix.classify_result(0, 10, "", "h264", "h264") == (MuxVerdict.OK, "")
    v, note = muxmatrix.classify_result(0, 10, "", "rawvideo", "hevc")
    assert v == MuxVerdict.FAIL and "reads back as 'rawvideo'" in note
    v, note = muxmatrix.classify_result(0, 10, "", "", "flac")
    assert v == MuxVerdict.FAIL and "no stream" in note
    v, _ = muxmatrix.classify_result(0, 10, "[mpegts @ 0x1] Stream 0, codec flac, is muxed as a private data stream and may not be recognized upon reading.\n", "flac", "flac")
    assert v == MuxVerdict.FAIL
    v, note = muxmatrix.classify_result(124, 0, "probe timed out")
    assert v == MuxVerdict.FAIL and note.startswith(muxmatrix.TRANSIENT)
    assert muxmatrix.is_transient(muxmatrix.MuxProbe("a", "b", MuxVerdict.FAIL, note))
    v, note = muxmatrix.classify_result(0, 10, "[mov @ 0x1] Using MS style video codec tag, the file may be unplayable!\n")
    assert v == MuxVerdict.OK_WITH_WARNING and "unplayable" in note
    v, note = muxmatrix.classify_result(0, 0, "x\n[out#0/mov @ 0x1] Nothing was written into output file, because at least one of its streams received no packets.\n")
    assert v == MuxVerdict.FAIL and "Nothing was written" in note
    v, note = muxmatrix.classify_result(1, 0, "")
    assert v == MuxVerdict.FAIL and "rc 1" in note


def test_probe_command_shape() -> None:
    cmd = muxmatrix._probe_cmd("mp4", "flac", "audio", "/tmp/x", "ffmpeg")
    assert cmd[:2] == ["ffmpeg", "-hide_banner"] and cmd[-3:] == ["-f", "mp4", "/tmp/x"] and "-c:a" in cmd and "-vn" in cmd
    cmd = muxmatrix._probe_cmd("matroska", "ffv1", "video", "/tmp/x", "ffmpeg")
    assert "-c:v" in cmd and "-an" in cmd and "testsrc2=size=320x240:rate=25" in cmd
    assert muxmatrix.video_tag_args("mp4", "hevc") == ["-tag:v", "hvc1"] and muxmatrix.video_tag_args("mov", "hevc") == ["-tag:v", "hvc1"]
    assert muxmatrix.video_tag_args("matroska", "hevc") == [] and muxmatrix.video_tag_args("mp4", "h264") == []


def test_transient_failures_are_not_cached(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake(container, encoder, *, kind, ffmpeg="ffmpeg"):  # noqa: ANN001
        calls.append(encoder)
        return muxmatrix.MuxProbe(container, encoder, MuxVerdict.FAIL, muxmatrix.TRANSIENT + "rc 124")

    monkeypatch.setattr(muxmatrix, "run_probe", fake)
    assert muxmatrix.probe("matroska", "nonexistent-enc", kind="video").verdict == MuxVerdict.FAIL
    assert muxmatrix.cached("matroska", "nonexistent-enc", kind="video").verdict == MuxVerdict.UNKNOWN
    muxmatrix.probe("matroska", "nonexistent-enc", kind="video")
    assert calls == ["nonexistent-enc", "nonexistent-enc"]  # probed again, never persisted
    assert "retried next time" in muxmatrix.reason("matroska", "nonexistent-enc", kind="video")


@pytest.mark.needs_sandbox
def test_real_probes_and_cache(isolated_dirs: dict[str, Path]) -> None:
    muxmatrix.invalidate_cache()
    encoders.invalidate_cache()
    p = muxmatrix.run_probe("mov", "flac", kind="audio")
    assert p.verdict == MuxVerdict.FAIL and p.note
    assert muxmatrix.run_probe("mp4", "flac", kind="audio").verdict == MuxVerdict.OK
    assert muxmatrix.run_probe("mp4", "libopus", kind="audio").verdict == MuxVerdict.OK
    assert muxmatrix.run_probe("mov", "ffv1", kind="video").verdict == MuxVerdict.OK_WITH_WARNING
    assert muxmatrix.run_probe("matroska", "libx264", kind="video").verdict == MuxVerdict.OK
    # read-back catches "successful" muxes nobody can play (review M1)
    p = muxmatrix.run_probe("avi", "libx265", kind="video")
    assert p.verdict == MuxVerdict.FAIL and "reads back" in p.note
    assert muxmatrix.run_probe("mpegts", "flac", kind="audio").verdict == MuxVerdict.FAIL
    assert muxmatrix.run_probe("mpegts", "ffv1", kind="video").verdict == MuxVerdict.FAIL
    assert muxmatrix.run_probe("mp4", "libx265", kind="video").verdict == MuxVerdict.OK  # tagged hvc1
    assert muxmatrix.run_probe("mkv", "libx264", kind="video").verdict == MuxVerdict.FAIL  # not a muxer name
    # a pair the seed does not know is probed on demand and lands in the per-version cache file
    assert muxmatrix.cached("matroska", "ffvhuff", kind="video").verdict == MuxVerdict.UNKNOWN
    pr = muxmatrix.probe("matroska", "ffvhuff", kind="video")
    assert pr.verdict == MuxVerdict.OK
    assert muxmatrix.probe("avi", "libopus", kind="audio").verdict == MuxVerdict.FAIL  # seeded (research matrix)
    files = list((isolated_dirs["cache"] / "muxmatrix").glob("matrix-*.json"))
    assert len(files) == 1
    d = json.loads(files[0].read_text())
    assert d["ffmpeg_version"] == encoders.ffmpeg_build_id()
    assert any(r["container"] == "matroska" and r["encoder"] == "ffvhuff" for r in d["probes"])
    # bulk probe returns one entry per pair and never re-probes cached ones
    res = muxmatrix.probe_all(["mp4", "mov"], ["libx264"], ["flac", "aac"])
    assert len(res) == 6
    got = {(r.container, r.encoder): r.verdict for r in res}
    assert got[("mov", "flac")] == MuxVerdict.FAIL and got[("mp4", "flac")] == MuxVerdict.OK and got[("mov", "aac")] == MuxVerdict.OK
    muxmatrix.invalidate_cache()
