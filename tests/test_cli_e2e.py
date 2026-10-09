"""CLI end-to-end on the real stack (sandbox): analyze, deint with cancel, run-job, view copy.

QTGMC6UI_PLUGIN_DIR must point at the bundled plugins for the needs_qtgmc tests."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from qtgmc_6ui.events import Event
from test_cli import parse_events

CLI = [sys.executable, "-m", "qtgmc_6ui"]


def _env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "QTGMC6UI_CACHE_DIR": str(tmp_path / "cache"), "QTGMC6UI_CONFIG_DIR": str(tmp_path / "cfg")}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.needs_sandbox
def test_analyze_reference_events(reference_clip: Path, tmp_path: Path) -> None:
    r = subprocess.run([*CLI, "analyze", str(reference_clip), "--events"], capture_output=True, text=True, timeout=300, env=_env(tmp_path))
    assert r.returncode == 0, r.stderr
    evs = parse_events(r.stdout)
    res = [e for e in evs if e.ev == "result"][0].data["data"]
    assert res["classification"]["content"] == "interlaced_bff" and res["classification"]["field_order"] == "bff"
    assert {e.data["name"] for e in evs if e.ev == "stage"} >= {"probe", "idet", "classify"}


@pytest.mark.needs_sandbox
def test_dry_run_reference(reference_clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "m.mkv"
    r = subprocess.run([*CLI, "deint", str(reference_clip), "-o", str(out), "--dry-run", "--json"],
                       capture_output=True, text=True, timeout=300, env=_env(tmp_path))
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)
    assert plan["outputs"] == [str(out)] and not out.exists()


@pytest.mark.needs_qtgmc
@pytest.mark.slow
def test_deint_sigterm_removes_partial_master(reference_clip: Path, tmp_path: Path) -> None:
    before = _sha(reference_clip)
    out = tmp_path / "m.mkv"
    partial = out.parent / f".{out.name}.partial"
    p = subprocess.Popen([*CLI, "deint", str(reference_clip), "-o", str(out), "--preset", "slower", "--events"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(tmp_path))
    head = []
    assert p.stdout is not None
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        head.append(line)
        ev = Event.from_json(line)
        if ev and ev.ev == "progress" and ev.data["stage"] == "master" and ev.data["done"] >= 20:
            break
    assert partial.exists() and not out.exists(), "".join(head)
    p.send_signal(signal.SIGTERM)
    rest, err = p.communicate(timeout=60)
    evs = parse_events("".join(head) + rest)
    assert p.returncode == 130, err
    assert [e.ev for e in evs][-2:] == ["cancelled", "done"] and evs[-1].data["exit_code"] == 130
    assert str(partial) in evs[-2].data["removed"]
    assert not out.exists() and not partial.exists() and _sha(reference_clip) == before


@pytest.mark.needs_qtgmc
@pytest.mark.slow
def test_run_job_stdin_with_view(reference_clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "m.mkv"
    r = subprocess.run([*CLI, "deint", str(reference_clip), "-o", str(out), "--preset", "faster", "--view", "mp4-h264-aac", "--save-job", "-"],
                       capture_output=True, text=True, timeout=60, env=_env(tmp_path))
    assert r.returncode == 0, r.stderr
    r = subprocess.run([*CLI, "run-job", "-", "--events"], input=r.stdout, capture_output=True, text=True, timeout=1800, env=_env(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    evs = parse_events(r.stdout)
    res = [e for e in evs if e.ev == "result"][0].data["data"]
    kinds = {o["kind"]: o for o in res["outputs"]}
    assert kinds["master"]["path"] == str(out) and kinds["master"]["frames"] == 548
    assert Path(kinds["view"]["path"]).is_file()


@pytest.mark.needs_qtgmc
@pytest.mark.slow
def test_no_master_reference(reference_clip: Path, tmp_path: Path) -> None:
    """--no-master: only the viewing copy remains; a SIGKILLed run's temp master is removed by
    the next run in the same folder."""
    before = _sha(reference_clip)
    out = tmp_path / "o"
    args = [*CLI, "deint", str(reference_clip), "--out-dir", str(out), "--view", "mp4-h264-aac", "--no-master", "--events"]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True, env=_env(tmp_path))
    assert p.stdout is not None
    for line in p.stdout:
        ev = Event.from_json(line)
        if ev and ev.ev == "progress" and ev.data["stage"] == "master" and ev.data["done"] >= 20:
            break
    p.kill()
    p.wait(30)
    assert any(".nomaster." in x.name for x in out.iterdir())  # crash leftovers (hidden)
    r = subprocess.run(args, capture_output=True, text=True, timeout=1800, env=_env(tmp_path))
    assert r.returncode == 0, r.stderr[-3000:]
    evs = parse_events(r.stdout)
    assert evs[0].data["outputs"] == [str(out / "test_input.deint.mp4-h264-aac.mp4")]
    res = [e for e in evs if e.ev == "result"][0].data["data"]
    assert [(o["kind"], o["frames"]) for o in res["outputs"]] == [("view", 548)]
    assert sorted(x.name for x in out.iterdir()) == ["test_input.deint.mp4-h264-aac.mp4"]
    assert _sha(reference_clip) == before
