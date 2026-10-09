"""runtime: streaming runner, cancel escalation, process groups, env, partial cleanup."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from qtgmc_6ui import runtime
from qtgmc_6ui.errors import ComponentError, ErrorCode

PY = sys.executable


def test_stream_splits_cr_and_lf() -> None:
    lines: list[str] = []
    code = "import sys; e=sys.stderr; e.write('a\\rb\\r\\nc\\n\\nd'); e.flush()"
    r = runtime.run_streaming([PY, "-c", code], on_stderr_line=lines.append)
    assert r.returncode == 0 and not r.cancelled
    assert lines == ["a", "b", "c", "d"]
    assert r.stderr_tail == ["a", "b", "c", "d"]


def test_stdout_capture_and_devnull_default(capfd: pytest.CaptureFixture[str]) -> None:
    r = runtime.run_streaming([PY, "-c", "print('x'); print('y')"], capture_stdout=True)
    assert r.stdout.splitlines() == ["x", "y"]
    runtime.run_streaming([PY, "-c", "print('leak')"])
    assert "leak" not in capfd.readouterr().out  # never pollute our (event) stdout


def test_nonzero_exit_does_not_raise() -> None:
    r = runtime.run_streaming([PY, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"])
    assert r.returncode == 3 and r.stderr_tail == ["boom"]


def test_tail_is_bounded() -> None:
    r = runtime.run_streaming([PY, "-c", "import sys\nfor i in range(500): sys.stderr.write(f'{i}\\n')"])
    assert len(r.stderr_tail) == runtime.TAIL_LINES and r.stderr_tail[-1] == "499"


def test_missing_tool_is_component_error() -> None:
    with pytest.raises(ComponentError) as ei:
        runtime.run_streaming(["definitely-not-a-tool-qtgmc"])
    assert ei.value.code == ErrorCode.TOOL_MISSING
    with pytest.raises(ComponentError):
        runtime.run_capture(["definitely-not-a-tool-qtgmc"])


def test_cancel_sigterm() -> None:
    tok = runtime.CancelToken()
    threading.Timer(0.3, tok.cancel).start()
    t0 = time.monotonic()
    r = runtime.run_streaming([PY, "-c", "import time; time.sleep(30)"], cancel=tok)
    assert r.cancelled and r.returncode == -signal.SIGTERM
    assert time.monotonic() - t0 < 5


def test_cancel_escalates_to_sigkill() -> None:
    code = "import signal, time, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); sys.stderr.write('ready\\n'); sys.stderr.flush(); time.sleep(30)"
    tok = runtime.CancelToken()
    ready = threading.Event()

    def on_line(line: str) -> None:
        if line == "ready":
            ready.set()
            tok.cancel()

    t0 = time.monotonic()
    r = runtime.run_streaming([PY, "-c", code], cancel=tok, kill_grace_s=0.5, on_stderr_line=on_line)
    assert ready.is_set() and r.cancelled and r.returncode == -signal.SIGKILL
    assert time.monotonic() - t0 < 5


def test_cancel_kills_whole_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n"
    )
    tok = runtime.CancelToken()

    def cancel_when_ready() -> None:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.05)
        tok.cancel()

    threading.Thread(target=cancel_when_ready, daemon=True).start()
    r = runtime.run_streaming([PY, "-c", code], cancel=tok)
    assert r.cancelled
    gpid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            break
        # zombie reaped by init eventually; check /proc state
        st = Path(f"/proc/{gpid}/stat")
        if not st.exists() or st.read_text().split()[2] == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("grandchild survived the group SIGTERM")


def test_default_token_used() -> None:
    tok = runtime.CancelToken()
    runtime.set_default_cancel(tok)
    try:
        threading.Timer(0.2, tok.cancel).start()
        r = runtime.run_streaming([PY, "-c", "import time; time.sleep(30)"])
        assert r.cancelled
        tok2 = runtime.CancelToken()
        runtime.set_default_cancel(tok2)
        threading.Timer(0.2, tok2.cancel).start()
        r = runtime.run_capture([PY, "-c", "import time; time.sleep(30)"])
        assert r.cancelled
    finally:
        runtime.set_default_cancel(None)


def test_run_capture() -> None:
    r = runtime.run_capture([PY, "-c", "import sys; print('out'); sys.stderr.write('e1\\re2\\n')"])
    assert r.returncode == 0 and r.stdout == "out\n"
    assert r.stderr == "e1\re2\n" and r.stderr_tail == ["e1", "e2"]
    r = runtime.run_capture([PY, "-c", "import time; time.sleep(30)"], timeout=0.3)
    assert r.timed_out and r.returncode != 0


def test_stdin_feed_with_spawn() -> None:
    sp = runtime.spawn([PY, "-c", "import sys; print(len(sys.stdin.buffer.read()))"], stdin=subprocess.PIPE, capture_stdout=True)
    assert sp.stdin is not None
    sp.stdin.write(b"x" * 100000)
    sp.stdin.close()
    r = sp.wait()
    assert r.returncode == 0 and r.stdout.strip() == "100000"


def test_non_ascii_path_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("LC_ALL", "C")
    env = runtime.subprocess_env()
    assert env["LC_ALL"] == "C.UTF-8" and env["LC_CTYPE"] == "C.UTF-8"
    assert env["PYTHONUTF8"] == "1" and env["AV_LOG_FORCE_NOCOLOR"] == "1"
    d = tmp_path / "vidéo ü 日本"
    d.mkdir()
    lines: list[str] = []
    r = runtime.run_streaming([PY, "-c", "import os, sys; sys.stderr.write(os.getcwd() + '\\n')"], cwd=str(d), on_stderr_line=lines.append)
    assert r.returncode == 0 and lines == [str(d)]


def test_remove_partial(tmp_path: Path) -> None:
    a = tmp_path / "a.mkv"
    a.write_bytes(b"x")
    assert runtime.remove_partial([str(a), str(tmp_path / "missing.mkv"), "-", ""]) == [str(a)]
    assert not a.exists()


def test_stdin_watcher() -> None:
    tok = runtime.CancelToken()
    th = runtime.watch_stdin_for_cancel(tok, io.StringIO("hello\n cancel \n"))
    th.join(2)
    assert tok.cancelled and tok.reason == "stdin"
    tok2 = runtime.CancelToken()
    runtime.watch_stdin_for_cancel(tok2, io.StringIO("x\n")).join(2)
    assert not tok2.cancelled


def test_signal_handler_repeat_semantics(tmp_path: Path) -> None:
    """SIGTERM after a (stdin) cancel or a second SIGTERM is the same cancel; only SIGINT twice
    exits hard - after killing children and running the hard-exit hooks."""
    marker = tmp_path / "hook-ran"
    code = (
        "import os, signal, sys, time\n"
        "from qtgmc_6ui import runtime\n"
        "t = runtime.CancelToken(); runtime.install_signal_handlers(t)\n"
        f"runtime.on_hard_exit(lambda: open({str(marker)!r}, 'w').write('x'))\n"
        "t.cancel('stdin')\n"
        "os.kill(os.getpid(), signal.SIGTERM); time.sleep(0.1)\n"
        "os.kill(os.getpid(), signal.SIGTERM); time.sleep(0.1)\n"
        "print('alive', t.reason, flush=True)\n"
        "t2 = runtime.CancelToken(); runtime.install_signal_handlers(t2)\n"
        "os.kill(os.getpid(), signal.SIGINT); time.sleep(0.1)\n"
        "print('first', t2.cancelled, t2.reason, flush=True)\n"
        "os.kill(os.getpid(), signal.SIGINT); time.sleep(5)\n"
        "print('not reached')\n"
    )
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=20)
    assert r.stdout.split("\n")[:2] == ["alive stdin", "first True SIGINT"]
    assert "not reached" not in r.stdout and r.returncode == 130 and marker.exists()


def test_pdeathsig_kills_child_when_parent_dies(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    code = (
        "import sys, os\n"
        "from qtgmc_6ui import runtime\n"
        f"sp = runtime.spawn([{PY!r}, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(sp.proc.pid))\n"
        "os.kill(os.getpid(), 9)\n"
    )
    subprocess.run([PY, "-c", code], timeout=20)
    child = int(pidfile.read_text())
    for _ in range(100):
        st = Path(f"/proc/{child}/stat")
        if not st.exists() or st.read_text().split()[2] == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("child survived its parent's SIGKILL")


def test_log_level() -> None:
    assert runtime.log_level("frame=  519 fps=120 q=-0.0 size=  1kB time=00:00:10.38") == "debug"
    assert runtime.log_level("[matroska @ 0x1] Error writing trailer") == "error"
    assert runtime.log_level("Warning: something") == "warning"
    assert runtime.log_level("Stream #0:0: Video: ffv1") == "info"
