"""CLI process-level tests: exit codes, --events protocol conformance (what the GUI relies on),
--json documents, cancel by SIGTERM / stdin, input protection.

The lower layers (analysis, pipeline, profiles) are replaced by fakes inside the CLI
subprocess through a small driver script, so these tests run without ffmpeg/VapourSynth.
Real-stack variants live in test_cli_e2e.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from qtgmc_6ui.events import STAGES, Event

DRIVER = r'''
import json, os, sys, time
from qtgmc_6ui import analysis, cli, commands, pipeline, profiles, runtime
from qtgmc_6ui.errors import CancelledError, EncodeError, ErrorCode
from qtgmc_6ui.models import (AnalysisResult, Classification, ContentClass, FieldOrder, JobResult,
    OutputFile, Profile, Recommendation, RecommendedAction)

CFG = json.loads(os.environ.get("FAKE_CFG", "{}"))
commands.CANCEL_GRACE_S = CFG.get("grace", 3.0)

WRITER = r"""
import sys, time
import os
out, n, delay = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
sys.stderr.write(f"pid={os.getpid()}\n"); sys.stderr.flush()
with open(out, "wb") as f:
    for i in range(1, n + 1):
        f.write(b"x" * 1000); f.flush()
        sys.stderr.write(f"frame={i:5d} fps=10\r"); sys.stderr.flush()
        time.sleep(delay)
sys.stderr.write("\nfinished\n")
"""

def fake_analyze(path, emitter=None, **kw):
    for i, st in enumerate(("probe", "idet", "classify")):
        emitter.stage(st, i, 3)
        emitter.progress(st, 1, 1, 0.0, None, 0.0)
    time.sleep(CFG.get("analyze_sleep", 0))
    content = ContentClass(CFG.get("content", "interlaced_bff"))
    fo = CFG.get("fo", "bff")
    action = {"interlaced_bff": "deinterlace", "interlaced_tff": "deinterlace", "telecined": "block_telecined",
              "mixed": "block_mixed", "progressive": "skip_progressive", "undetermined": "confirm_field_order"}[content.value]
    return AnalysisResult(input=path, classification=Classification(content, FieldOrder(fo) if fo else None, 0.97, ["fake"]),
                          recommendation=Recommendation(RecommendedAction(action), "fake explanation", True))

def fake_run_job(job, *, emitter, cancel):
    if CFG.get("crash"):
        raise RuntimeError("kaboom")
    outs = [("master", job.master_output, None)] + [("view", v.output, v.profile) for v in job.views]
    done = []
    for idx, (kind, out, prof) in enumerate(outs):
        emitter.stage(kind, idx, len(outs), prof or kind)
        n = CFG.get("frames", 20)
        if kind == "view" and CFG.get("fail_view"):
            open(out, "wb").write(b"half")
            raise EncodeError(ErrorCode.ENCODE_FAILED, "view encoder failed", "see the log")
        if CFG.get("stuck"):
            if CFG.get("hidden_partial"):
                d, nm = os.path.split(out)
                out = os.path.join(d, "." + nm + ".partial")
            with open(out, "wb") as f:
                for i in range(1000):
                    f.write(b"y"); f.flush(); time.sleep(0.05)
        def on_line(line, kind=kind, n=n):
            if line.startswith("frame="):
                emitter.progress(kind, int(line.split()[0].split("=")[1] or 0) if line.split()[0] != "frame=" else int(line.split()[1]), n, 10.0, 1.0, 0.1)
            else:
                emitter.log("ffmpeg", line)
        r = runtime.run_streaming([sys.executable, "-c", WRITER, out, str(n), str(CFG.get("delay", 0.01))], cancel=cancel, on_stderr_line=on_line)
        if r.cancelled:
            time.sleep(CFG.get("cleanup_delay", 0))
            raise CancelledError(runtime.remove_partial([out]), [d.path for d in done])
        done.append(OutputFile(kind=kind, path=out, profile=prof, frames=n, size_bytes=os.path.getsize(out)))
    return JobResult(input=job.input, analysis=job.analysis, outputs=done, elapsed_s=0.1, average_fps=10.0,
                     effective_master=job.master)

analysis.analyze = fake_analyze
pipeline.run_job = fake_run_job
profiles.load = lambda pid: Profile(id=pid, container="mp4", extension="mp4")
sys.exit(cli.main(sys.argv[1:]))
'''


@pytest.fixture
def driver(tmp_path: Path) -> Path:
    p = tmp_path / "driver.py"
    p.write_text(DRIVER)
    return p


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    d = tmp_path / "in dir é"
    d.mkdir()
    c = d / "clip ü.avi"
    c.write_bytes(os.urandom(4096))
    return c


def _env(cfg: dict[str, Any] | None, tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["FAKE_CFG"] = json.dumps(cfg or {})
    env["QTGMC6UI_CACHE_DIR"] = str(tmp_path / "cache")
    env["QTGMC6UI_CONFIG_DIR"] = str(tmp_path / "config")
    return env


def run_cli(driver: Path, tmp_path: Path, *argv: str, cfg: dict[str, Any] | None = None, input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(driver), *argv],
        capture_output=True, text=True, timeout=60, env=_env(cfg, tmp_path), input=input,
        stdin=None if input is not None else subprocess.DEVNULL,
    )


def parse_events(stdout: str) -> list[Event]:
    """Protocol conformance: EVERY stdout line is an event, ``t`` is monotonic, ``done`` is last
    and unique, ``start`` (if any) is first."""
    evs = []
    for line in stdout.splitlines():
        ev = Event.from_json(line)
        assert ev is not None, f"non-event line on stdout: {line!r}"
        evs.append(ev)
    assert evs, "no events"
    assert evs[-1].ev == "done" and [e.ev for e in evs].count("done") == 1
    ts = [e.t for e in evs]
    assert ts == sorted(ts)
    if any(e.ev == "start" for e in evs):
        assert evs[0].ev == "start"
    for e in evs:
        assert e.ev in {"start", "stage", "progress", "log", "warning", "result", "error", "cancelled", "done"}
        d = e.data
        if e.ev == "start":
            assert {"schema", "command", "input", "pid", "outputs"} <= d.keys()
        elif e.ev == "stage":
            assert {"name", "index", "total", "label"} <= d.keys() and d["name"] in STAGES
        elif e.ev == "progress":
            assert {"stage", "done", "total", "fps", "eta_s", "elapsed_s"} <= d.keys()
        elif e.ev == "log":
            assert {"level", "source", "line"} <= d.keys()
        elif e.ev == "warning":
            assert {"code", "message", "hint"} <= d.keys()
        elif e.ev == "result":
            assert isinstance(d["data"], dict)
        elif e.ev == "error":
            assert {"code", "message", "hint", "details", "exit_code"} <= d.keys()
        elif e.ev == "cancelled":
            assert isinstance(d["removed"], list) and isinstance(d["kept"], list)
    return evs


def _done(evs: list[Event]) -> int:
    return evs[-1].data["exit_code"]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- basics
def test_version_json(driver: Path, tmp_path: Path) -> None:
    r = run_cli(driver, tmp_path, "version", "--json")
    assert r.returncode == 0 and "version" in json.loads(r.stdout)


def test_usage_error_events(driver: Path, tmp_path: Path) -> None:
    r = run_cli(driver, tmp_path, "deint", "--events", "--rate", "triple", "x.avi")
    assert r.returncode == 2
    evs = parse_events(r.stdout)
    assert evs[0].ev == "error" and evs[0].data["code"] == "usage" and _done(evs) == 2


def test_analyze_events_and_json(driver: Path, tmp_path: Path, clip: Path) -> None:
    r = run_cli(driver, tmp_path, "analyze", str(clip), "--events")
    assert r.returncode == 0, r.stderr
    evs = parse_events(r.stdout)
    assert evs[0].data["command"] == "analyze" and evs[0].data["input"] == str(clip)
    res = [e for e in evs if e.ev == "result"]
    assert len(res) == 1 and res[0].data["data"]["classification"]["content"] == "interlaced_bff"
    r = run_cli(driver, tmp_path, "analyze", str(clip), "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["classification"]["field_order"] == "bff"
    r = run_cli(driver, tmp_path, "analyze", str(clip))
    assert r.returncode == 0 and "classification: interlaced_bff (bff)" in r.stdout


def test_missing_input(driver: Path, tmp_path: Path) -> None:
    r = run_cli(driver, tmp_path, "analyze", str(tmp_path / "nope.avi"), "--json")
    assert r.returncode == 5 and json.loads(r.stdout)["error"]["code"] == "input_not_found"
    r = run_cli(driver, tmp_path, "deint", str(tmp_path / "nope.avi"), "--events")
    evs = parse_events(r.stdout)
    assert r.returncode == 5 and _done(evs) == 5
    r = run_cli(driver, tmp_path, "deint", str(tmp_path / "nope.avi"))
    assert r.returncode == 5 and "input_not_found" in r.stderr and "Traceback" not in r.stderr


# --------------------------------------------------------------------------- deint
def test_deint_events_success(driver: Path, tmp_path: Path, clip: Path) -> None:
    before = _sha(clip)
    out = tmp_path / "out"
    r = run_cli(driver, tmp_path, "deint", str(clip), "--out-dir", str(out), "--view", "mp4-a", "--events")
    assert r.returncode == 0, r.stderr
    evs = parse_events(r.stdout)
    start = evs[0].data
    master = out / "clip ü.deint.mkv"
    view = out / "clip ü.deint.mp4-a.mp4"
    assert start["command"] == "deint" and start["outputs"] == [str(master), str(view)]
    assert master.is_file() and view.is_file()
    names = [e.data["name"] for e in evs if e.ev == "stage"]
    assert names[:3] == ["probe", "idet", "classify"] and "master" in names and "view" in names
    prog = [e for e in evs if e.ev == "progress" and e.data["stage"] == "master"]
    assert prog and prog[-1].data["done"] == 20
    res = [e for e in evs if e.ev == "result"][0].data["data"]
    assert [o["kind"] for o in res["outputs"]] == ["master", "view"]
    assert _done(evs) == 0 and _sha(clip) == before


def test_deint_text_mode(driver: Path, tmp_path: Path, clip: Path) -> None:
    r = run_cli(driver, tmp_path, "deint", str(clip), "-o", str(tmp_path / "m.mkv"))
    assert r.returncode == 0, r.stderr
    assert str(tmp_path / "m.mkv") in r.stdout and "master" in r.stdout


@pytest.mark.parametrize(
    "cfg,extra,code,err",
    [
        ({"content": "telecined", "fo": "tff"}, [], 3, "blocked_telecined"),
        ({"content": "mixed", "fo": "tff"}, [], 3, "blocked_mixed"),
        ({"content": "progressive", "fo": None}, [], 3, "blocked_progressive"),
        ({"content": "progressive", "fo": None}, ["--force"], 3, "blocked_undetermined"),
        ({"content": "undetermined", "fo": None}, [], 3, "blocked_undetermined"),
        ({"content": "undetermined", "fo": None}, ["--force"], 3, "blocked_undetermined"),
        ({"content": "telecined", "fo": "tff"}, ["--force"], 0, None),
        ({"content": "progressive", "fo": None}, ["--force", "--field-order", "tff"], 0, None),
        ({"content": "undetermined", "fo": None}, ["--field-order", "bff"], 0, None),
    ],
)
def test_blocking_rules(driver: Path, tmp_path: Path, clip: Path, cfg, extra, code, err) -> None:
    out = tmp_path / "m.mkv"
    r = run_cli(driver, tmp_path, "deint", str(clip), "-o", str(out), "--events", *extra, cfg=cfg)
    evs = parse_events(r.stdout)
    assert r.returncode == code == _done(evs), r.stderr
    if err:
        e = [e for e in evs if e.ev == "error"][0].data
        assert e["code"] == err and e["exit_code"] == 3 and e["hint"]
        assert not out.exists()
    else:
        assert out.exists()
        if "--force" in extra:
            assert any(e.ev == "warning" and e.data["code"] == "forced" for e in evs)


def test_refuses_output_equal_input(driver: Path, tmp_path: Path, clip: Path) -> None:
    before = _sha(clip)
    for extra in (["-o", str(clip)], ["-o", str(clip), "--overwrite"]):
        r = run_cli(driver, tmp_path, "deint", str(clip), "--json", *extra)
        assert r.returncode == 2 and "overwrite the input" in json.loads(r.stdout)["error"]["message"]
    # name template that resolves to the input itself
    r = run_cli(driver, tmp_path, "deint", str(clip), "--name", "{stem}", "--json", "-o", str(clip.with_suffix(".avi")))
    assert r.returncode == 2
    assert _sha(clip) == before


def test_output_exists_and_overwrite(driver: Path, tmp_path: Path, clip: Path) -> None:
    out = tmp_path / "m.mkv"
    out.write_bytes(b"old")
    r = run_cli(driver, tmp_path, "deint", str(clip), "-o", str(out), "--json")
    assert r.returncode == 7 and json.loads(r.stdout)["error"]["code"] == "output_exists"
    assert out.read_bytes() == b"old"
    r = run_cli(driver, tmp_path, "deint", str(clip), "-o", str(out), "--overwrite", "--json")
    assert r.returncode == 0 and out.read_bytes() != b"old"


def test_dry_run_writes_nothing(driver: Path, tmp_path: Path, clip: Path) -> None:
    out = tmp_path / "m.mkv"
    r = run_cli(driver, tmp_path, "deint", str(clip), "-o", str(out), "--view", "v1", "--dry-run", "--json")
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)
    assert plan["dry_run"] and plan["outputs"][0] == str(out) and plan["views"][0]["profile"] == "v1"
    assert not out.exists()


def test_internal_error_no_traceback(driver: Path, tmp_path: Path, clip: Path) -> None:
    r = run_cli(driver, tmp_path, "deint", str(clip), "-o", str(tmp_path / "m.mkv"), cfg={"crash": True})
    assert r.returncode == 1 and "[internal]" not in r.stdout
    assert "error [internal]" in r.stderr and "kaboom" in r.stderr and "Traceback" not in r.stderr
    r = run_cli(driver, tmp_path, "deint", str(clip), "-o", str(tmp_path / "m.mkv"), "--events", cfg={"crash": True})
    evs = parse_events(r.stdout)
    err = [e for e in evs if e.ev == "error"][0].data
    assert err["code"] == "internal" and "traceback" in err["details"] and _done(evs) == 1


# --------------------------------------------------------------------------- save-job / run-job
def test_save_job_and_run_job_stdin(driver: Path, tmp_path: Path, clip: Path) -> None:
    out = tmp_path / "m.mkv"
    r = run_cli(driver, tmp_path, "deint", str(clip), "-o", str(out), "--preset", "fast", "--save-job", "-")
    assert r.returncode == 0 and not out.exists()
    job = json.loads(r.stdout)
    assert job["master"]["preset"] == "fast" and job["master_output"] == str(out)
    r = run_cli(driver, tmp_path, "run-job", "-", "--events", input=r.stdout)
    assert r.returncode == 0, r.stderr
    evs = parse_events(r.stdout)
    assert evs[0].data["command"] == "run-job" and out.exists()
    jf = tmp_path / "job.json"
    r = run_cli(driver, tmp_path, "deint", str(clip), "-o", str(tmp_path / "m2.mkv"), "--save-job", str(jf))
    assert r.returncode == 0 and jf.is_file()
    r = run_cli(driver, tmp_path, "run-job", str(jf), "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["outputs"][0]["path"] == str(tmp_path / "m2.mkv")
    r = run_cli(driver, tmp_path, "run-job", "-", "--events", input="{bad json\n")
    evs = parse_events(r.stdout)
    assert r.returncode == 2 and _done(evs) == 2


# --------------------------------------------------------------------------- cancel
def _popen(driver: Path, tmp_path: Path, *argv: str, cfg: dict[str, Any]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(driver), *argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=_env(cfg, tmp_path),
    )


def _wait_for(p: subprocess.Popen[str], pred, timeout: float = 20) -> list[str]:
    lines: list[str] = []
    assert p.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        lines.append(line)
        ev = Event.from_json(line)
        if ev and pred(ev):
            return lines
    pytest.fail("condition not reached; got:\n" + "".join(lines))


def _cancel_run(driver: Path, tmp_path: Path, clip: Path, how: str, cfg: dict[str, Any] | None = None) -> tuple[int, list[Event], Path]:
    out = tmp_path / "m.mkv"
    cfg = {"frames": 2000, "delay": 0.01, **(cfg or {})}
    p = _popen(driver, tmp_path, "deint", str(clip), "-o", str(out), "--view", "v1", "--events", cfg=cfg)
    head = _wait_for(p, lambda e: (e.ev == "progress" and e.data["stage"] == "master" and e.data["done"] >= 5)
                     or (cfg.get("stuck") and e.ev == "stage" and e.data["name"] == "master"))
    if cfg.get("stuck"):
        time.sleep(0.3)
    assert out.exists()  # partial master is on disk
    t0 = time.monotonic()
    if how == "sigterm":
        p.send_signal(signal.SIGTERM)
    elif how == "sigint":
        p.send_signal(signal.SIGINT)
    else:
        assert p.stdin is not None
        p.stdin.write("cancel\n")
        p.stdin.flush()
    rest, _ = p.communicate(timeout=30)
    assert time.monotonic() - t0 < 10
    return p.returncode, parse_events("".join(head) + rest), out


@pytest.mark.parametrize("how", ["sigterm", "sigint", "stdin"])
def test_cancel_removes_partial(driver: Path, tmp_path: Path, clip: Path, how: str) -> None:
    before = _sha(clip)
    rc, evs, out = _cancel_run(driver, tmp_path, clip, how)
    assert rc == 130 and _done(evs) == 130
    assert [e.ev for e in evs][-2:] == ["cancelled", "done"]
    c = evs[-2].data
    assert c["removed"] == [str(out)] and c["kept"] == []
    assert not out.exists() and not any(e.ev == "result" for e in evs)
    assert _sha(clip) == before


def test_cancel_safety_net_when_pipeline_ignores_token(driver: Path, tmp_path: Path, clip: Path) -> None:
    rc, evs, out = _cancel_run(driver, tmp_path, clip, "sigterm", {"stuck": True, "grace": 0.5})
    assert rc == 130 and evs[-2].ev == "cancelled" and str(out) in evs[-2].data["removed"]
    assert not out.exists()


def test_cancel_during_analysis_keeps_preexisting_output(driver: Path, tmp_path: Path, clip: Path) -> None:
    out = tmp_path / "m.mkv"
    out.write_bytes(b"previous master")
    p = _popen(driver, tmp_path, "deint", str(clip), "-o", str(out), "--overwrite", "--events", cfg={"analyze_sleep": 30, "grace": 0.5})
    head = _wait_for(p, lambda e: e.ev == "stage" and e.data["name"] == "classify")
    p.send_signal(signal.SIGTERM)
    rest, _ = p.communicate(timeout=30)
    evs = parse_events("".join(head) + rest)
    assert p.returncode == 130 and evs[-2].ev == "cancelled" and evs[-2].data["removed"] == []
    assert out.read_bytes() == b"previous master"


def test_cancel_text_mode(driver: Path, tmp_path: Path, clip: Path) -> None:
    out = tmp_path / "m.mkv"
    p = subprocess.Popen([sys.executable, str(driver), "deint", str(clip), "-o", str(out)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env=_env({"frames": 2000}, tmp_path))
    for _ in range(200):
        if out.exists():
            break
        time.sleep(0.05)
    time.sleep(0.2)
    p.send_signal(signal.SIGINT)
    _, err = p.communicate(timeout=30)
    assert p.returncode == 130 and "cancelled" in err and not out.exists()


# --------------------------------------------------------------------------- review B1/B2
def _pid_gone(pid: int) -> bool:
    for _ in range(100):
        st = Path(f"/proc/{pid}/stat")
        if not st.exists() or st.read_text().split()[2] == "Z":
            return True
        time.sleep(0.05)
    return False


def _master_running(p: subprocess.Popen[str]) -> tuple[list[str], int]:
    """Read events until the fake master reports progress; returns (lines, writer pid)."""
    pid = 0
    lines: list[str] = []
    assert p.stdout is not None
    while True:
        line = p.stdout.readline()
        assert line, "".join(lines)
        lines.append(line)
        ev = Event.from_json(line)
        if ev and ev.ev == "log" and ev.data["line"].startswith("pid="):
            pid = int(ev.data["line"][4:])
        if ev and ev.ev == "progress" and ev.data["stage"] == "master" and ev.data["done"] >= 5:
            return lines, pid


def test_gui_escalation_sigterm_after_stdin_cancel_still_cleans(driver: Path, tmp_path: Path, clip: Path) -> None:
    """GUI: 'cancel' on stdin, SIGTERM shortly after, while the pipeline is still cleaning up."""
    out = tmp_path / "m.mkv"
    p = _popen(driver, tmp_path, "deint", str(clip), "-o", str(out), "--events", cfg={"frames": 5000, "cleanup_delay": 1.5})
    head, _ = _master_running(p)
    assert p.stdin is not None
    p.stdin.write("cancel\n")
    p.stdin.flush()
    time.sleep(0.3)
    p.send_signal(signal.SIGTERM)
    time.sleep(0.2)
    p.send_signal(signal.SIGTERM)
    rest, _ = p.communicate(timeout=30)
    evs = parse_events("".join(head) + rest)
    assert p.returncode == 130 and [e.ev for e in evs][-2:] == ["cancelled", "done"]
    assert any(e.ev == "log" and e.data["source"] == "app" and "cancelling" in e.data["line"] for e in evs)
    assert evs[-2].data["removed"] == [str(out)] and not out.exists()


def test_double_sigint_hard_exit_still_removes_partial(driver: Path, tmp_path: Path, clip: Path) -> None:
    out = tmp_path / "m.mkv"
    p = _popen(driver, tmp_path, "deint", str(clip), "-o", str(out), "--events", cfg={"stuck": True, "grace": 30})
    head = _wait_for(p, lambda e: e.ev == "stage" and e.data["name"] == "master")
    time.sleep(0.3)
    assert out.exists()
    p.send_signal(signal.SIGINT)
    time.sleep(0.3)
    p.send_signal(signal.SIGINT)
    rest, _ = p.communicate(timeout=30)
    evs = parse_events("".join(head) + rest)
    assert p.returncode == 130 and evs[-2].ev == "cancelled" and str(out) in evs[-2].data["removed"]
    assert not out.exists()


def test_sigkill_of_cli_leaves_no_orphan_encoder(driver: Path, tmp_path: Path, clip: Path) -> None:
    out = tmp_path / "m.mkv"
    p = _popen(driver, tmp_path, "deint", str(clip), "-o", str(out), "--events", cfg={"frames": 5000})
    _, pid = _master_running(p)
    assert pid and not _pid_gone_now(pid)
    p.kill()
    p.wait(10)
    assert _pid_gone(pid), "encoder child survived the CLI's SIGKILL"


def _pid_gone_now(pid: int) -> bool:
    st = Path(f"/proc/{pid}/stat")
    return not st.exists() or st.read_text().split()[2] == "Z"


def test_lost_reader_is_a_cancel(driver: Path, tmp_path: Path, clip: Path) -> None:
    """The GUI dies: our stdout pipe breaks -> cancel + cleanup, no traceback, exit 130."""
    out = tmp_path / "m.mkv"
    p = _popen(driver, tmp_path, "deint", str(clip), "-o", str(out), "--events", cfg={"frames": 5000})
    _, pid = _master_running(p)
    assert p.stdout is not None
    p.stdout.close()
    p.wait(30)
    err = p.stderr.read() if p.stderr else ""
    assert p.returncode == 130, err
    assert "Traceback" not in err and not out.exists() and _pid_gone(pid)


def test_failed_view_keeps_finished_master(driver: Path, tmp_path: Path, clip: Path) -> None:
    out = tmp_path / "m.mkv"
    r = run_cli(driver, tmp_path, "deint", str(clip), "-o", str(out), "--view", "v1", "--events", cfg={"fail_view": True})
    assert r.returncode == 6 and out.exists()
    assert not (tmp_path / "clip ü.deint.v1.mp4").exists()


def test_hard_exit_removes_hidden_partial(driver: Path, tmp_path: Path, clip: Path) -> None:
    out = tmp_path / "m.mkv"
    part = tmp_path / ".m.mkv.partial"
    p = _popen(driver, tmp_path, "deint", str(clip), "-o", str(out), "--events", cfg={"stuck": True, "hidden_partial": True, "grace": 30})
    head = _wait_for(p, lambda e: e.ev == "stage" and e.data["name"] == "master")
    time.sleep(0.3)
    assert part.exists()
    p.send_signal(signal.SIGINT)
    time.sleep(0.3)
    p.send_signal(signal.SIGINT)
    rest, _ = p.communicate(timeout=30)
    evs = parse_events("".join(head) + rest)
    assert p.returncode == 130 and str(part) in evs[-2].data["removed"] and not part.exists()
