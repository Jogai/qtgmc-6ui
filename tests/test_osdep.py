"""osdep (POSIX host): real process-group stops, die-with-parent, aliases, liveness, paths,
library naming and the child environment policies."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from qtgmc_6ui import osdep
from qtgmc_6ui.osdep import StopLevel

PY = sys.executable
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX host")


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not osdep.process_alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_host_is_posix() -> None:
    assert isinstance(osdep.HOST, osdep.PosixHost)


def _tree(tmp_path: Path) -> tuple[subprocess.Popen[bytes], int]:
    """A child (own group) that starts a grandchild; returns (child, grandchild pid)."""
    pidfile = tmp_path / "gc.pid"
    code = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    p = subprocess.Popen([PY, "-c", code], **osdep.popen_kwargs())
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text():
            return p, int(pidfile.read_text())
        time.sleep(0.02)
    raise AssertionError("grandchild did not start")


def test_popen_kwargs() -> None:
    kw = osdep.popen_kwargs()
    assert kw["start_new_session"] is True and callable(kw["preexec_fn"])
    assert osdep.popen_kwargs(die_with_parent=False)["preexec_fn"] is None


@pytest.mark.parametrize("level,rc", [(StopLevel.GRACEFUL, -signal.SIGTERM), (StopLevel.FORCE, -signal.SIGKILL)])
def test_signal_tree_reaches_the_group(tmp_path: Path, level: StopLevel, rc: int) -> None:
    p, gc = _tree(tmp_path)
    osdep.signal_tree(p, level)
    assert p.wait(10) == rc and _gone(gc)
    osdep.signal_tree(p, level)  # finished: no-op, no error


def test_stop_tree_escalates(tmp_path: Path) -> None:
    code = "import signal, time, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ok', flush=True); time.sleep(60)"
    p = subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE, **osdep.popen_kwargs())
    assert p.stdout is not None and p.stdout.readline().strip() == b"ok"
    t0 = time.monotonic()
    assert osdep.stop_tree(p, 0.3) == -signal.SIGKILL and time.monotonic() - t0 < 5
    q = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"], **osdep.popen_kwargs())
    r = subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE, **osdep.popen_kwargs())
    assert r.stdout is not None and r.stdout.readline().strip() == b"ok"
    osdep.stop_trees([q, r], 0.3)
    assert q.wait(5) == -signal.SIGTERM and r.wait(5) == -signal.SIGKILL


def test_die_with_parent(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    code = (
        "import os, subprocess\n"
        "from qtgmc_6ui import osdep\n"
        f"p = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(60)'], **osdep.popen_kwargs())\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "os.kill(os.getpid(), 9)\n"
    )
    subprocess.run([PY, "-c", code], timeout=20)
    assert _gone(int(pidfile.read_text()))


def test_termination_handlers() -> None:
    code = (
        "import os, signal, time\n"
        "from qtgmc_6ui import osdep\n"
        "seen = []\n"
        "assert osdep.install_termination_handlers(lambda n, i: seen.append((n, i)))\n"
        "os.kill(os.getpid(), signal.SIGTERM); os.kill(os.getpid(), signal.SIGINT); time.sleep(0.1)\n"
        "print(seen)\n"
    )
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=20)
    assert r.stdout.strip() == "[('SIGTERM', False), ('SIGINT', True)]"
    import threading

    box = []
    t = threading.Thread(target=lambda: box.append(osdep.install_termination_handlers(lambda n, i: None)))
    t.start()
    t.join()
    assert box == [False]


def test_path_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui import paths

    for k in ("QTGMC6UI_PLUGIN_DIR", "QTGMC6UI_BUILD_INFO"):
        monkeypatch.delenv(k, raising=False)
    assert paths.plugin_dir() == Path("/usr/lib/qtgmc-6ui/plugins")
    assert paths.build_info_path() == Path("/usr/lib/qtgmc-6ui/build-info.json")
    monkeypatch.setenv("QTGMC6UI_CONFIG_DIR", "/c")
    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", "/k")
    monkeypatch.setenv("QTGMC6UI_PLUGIN_DIR", "/p")
    monkeypatch.setenv("QTGMC6UI_BUILD_INFO", "/b.json")
    assert (paths.config_dir(), paths.cache_dir(), paths.plugin_dir(), paths.build_info_path()) == (Path("/c"), Path("/k"), Path("/p"), Path("/b.json"))
    assert paths.profiles_dir() == Path("/c/profiles")


def test_library_naming() -> None:
    assert osdep.shared_lib_name("bestsource") == "libbestsource.so"
    assert osdep.plugin_glob() == "*.so"
    assert osdep.lib_component("libakarin") == "akarin" and osdep.lib_component("vsznedi3") == "vsznedi3"
    rx = osdep.variant_suffix_re()
    assert rx.search("mvutensils.avx2.so") and not rx.search("mvutensils.so")
    assert osdep.default_python_executable() == "/usr/bin/python3.14"


def test_child_env_policies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.setenv("PYTHONUTF8", "0")
    e = osdep.subprocess_env()
    assert (e["LC_ALL"], e["LC_CTYPE"], e["PYTHONUTF8"], e["AV_LOG_FORCE_NOCOLOR"]) == ("C.UTF-8", "C.UTF-8", "1", "1")
    t = osdep.tool_env()
    assert (t["LC_ALL"], t["LC_CTYPE"], t["PYTHONUTF8"]) == ("C.UTF-8", "C.UTF-8", "0")
    env = {"LANG": "C"}
    osdep.force_utf8_environ(env)
    assert env == {"LANG": "C", "PYTHONUTF8": "1", "LC_CTYPE": "C.UTF-8"}
    assert osdep.utf8_locale_edits(lambda k: {"LC_ALL": "C"}.get(k, "")) == (["LC_ALL"], {"LC_CTYPE": "C.UTF-8"})
    assert osdep.utf8_locale_edits(lambda k: {"LANG": "en_US.UTF-8"}.get(k, "")) == ([], {})


def test_ascii_alias(tmp_path: Path) -> None:
    raw = os.fsencode(str(tmp_path)) + b"/bad\xff name.avi"
    with open(raw, "wb") as f:
        f.write(b"data")
    p = os.fsdecode(raw)
    link = osdep.ascii_alias(p, tmp_path / "links")
    assert link.isascii() and link.endswith(".avi") and Path(link).read_bytes() == b"data"
    assert osdep.ascii_alias(p, tmp_path / "links") == link  # stable, replaced in place


def test_dir_writable(tmp_path: Path) -> None:
    assert osdep.dir_writable(tmp_path) and not osdep.dir_writable(tmp_path / "missing")


def test_find_plugin_posix(tmp_path: Path) -> None:
    (tmp_path / "libbestsource.so").write_bytes(b"")
    (tmp_path / "libmvutensils.avx2.so").write_bytes(b"")
    assert osdep.find_plugin(tmp_path, "bestsource") == tmp_path / "libbestsource.so"
    assert osdep.find_plugin(tmp_path, "mvutensils") is None  # a CPU variant is never picked
    assert osdep.find_plugin(tmp_path / "missing", "bestsource") is None


def test_graceful_stop_available_posix() -> None:
    assert osdep.graceful_stop_available() is True
