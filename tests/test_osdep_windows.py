"""osdep_windows: importable on Linux; paths/naming/env with a patched environment; process
control only through mocks (the module is experimental and untested on Windows)."""

from __future__ import annotations

import importlib
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qtgmc_6ui import osdep, osdep_windows
from qtgmc_6ui.osdep import StopLevel
from qtgmc_6ui.osdep_windows import WindowsHost


def test_import_safe_and_selected_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    importlib.reload(osdep_windows)
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(osdep._select(), osdep_windows.WindowsHost)
    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(osdep._select(), osdep.PosixHost)


def test_same_api_as_posix() -> None:
    posix = {n for n in dir(osdep.PosixHost) if not n.startswith("_")}
    win = {n for n in dir(WindowsHost) if not n.startswith("_")}
    assert posix - win == set()


def test_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui import paths

    w = WindowsHost()
    monkeypatch.setenv("APPDATA", r"C:\Users\u\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    monkeypatch.setenv("ProgramFiles", r"D:\Apps")
    for k in ("QTGMC6UI_CONFIG_DIR", "QTGMC6UI_CACHE_DIR"):
        monkeypatch.delenv(k, raising=False)
    assert w.config_base() == Path(r"C:\Users\u\AppData\Roaming")
    assert w.cache_base() == Path(r"C:\Users\u\AppData\Local")
    assert w.install_root() == Path(r"D:\Apps") / "qtgmc-6ui"
    monkeypatch.setattr(osdep, "HOST", w)
    assert paths.config_dir() == Path(r"C:\Users\u\AppData\Roaming") / "qtgmc-6ui"
    assert paths.cache_dir() == Path(r"C:\Users\u\AppData\Local") / "qtgmc-6ui"
    monkeypatch.setenv("QTGMC6UI_CONFIG_DIR", "/override")
    assert paths.config_dir() == Path("/override")
    monkeypatch.delenv("APPDATA")
    assert w.config_base().parts[-2:] == ("AppData", "Roaming")


def test_naming_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    w = WindowsHost()
    assert w.shared_lib_name("bestsource") == "bestsource.dll" and w.plugin_glob == "*.dll"
    assert w.variant_suffix_re.search("mvutensils.AVX2.dll") and not w.variant_suffix_re.search("mvutensils.dll")
    assert w.lib_component("libakarin") == "akarin"
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("PYTHONUTF8", "0")
    e = w.subprocess_env()
    assert e["PYTHONUTF8"] == "1" and e["LANG"] == "C" and e["AV_LOG_FORCE_NOCOLOR"] == "1"
    monkeypatch.delenv("LC_CTYPE", raising=False)
    assert "LC_CTYPE" not in w.subprocess_env()
    assert w.tool_env()["PYTHONUTF8"] == "0" and w.tool_env()["AV_LOG_FORCE_NOCOLOR"] == "1"
    env: dict[str, str] = {}
    w.force_utf8_environ(env)
    assert env == {"PYTHONUTF8": "1"}
    assert w.utf8_locale_edits(lambda k: "C") == ([], {})
    assert w.ascii_alias("C:\\x\\bad.avi", tmp_path) == "C:\\x\\bad.avi"
    assert w.popen_kwargs() == {"creationflags": 0x200} and w.die_with_parent_hook() is None
    assert "APPDATA" in w.diagnostic_env_keys


class _Proc:
    def __init__(self, alive: bool = True) -> None:
        self.pid = 4242
        self.alive = alive
        self.killed = False

    def poll(self) -> int | None:
        return None if self.alive else 0

    def kill(self) -> None:
        self.killed = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        import subprocess

        if self.alive and timeout is not None:
            raise subprocess.TimeoutExpired("x", timeout)
        return 1


def test_process_control_with_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    w = WindowsHost()
    calls: list[tuple[str, Any]] = []
    k32 = SimpleNamespace(
        OpenProcess=lambda *a: calls.append(("open", a)) or 7,
        AssignProcessToJobObject=lambda job, h: calls.append(("assign", job)) or 1,
        CloseHandle=lambda h: calls.append(("close", h)) or 1,
        TerminateJobObject=lambda job, code: calls.append(("terminate_job", job)) or 1,
    )
    monkeypatch.setattr(w, "_kernel32", lambda: k32)
    monkeypatch.setattr(w, "_new_kill_on_close_job", lambda: "JOB")
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 1, raising=False)
    monkeypatch.setattr(osdep_windows.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    p = _Proc()
    w.adopt(p)
    assert ("assign", "JOB") in calls and w._jobs[4242] == "JOB"
    w.signal_tree(p, StopLevel.GRACEFUL)
    assert sent == [(4242, 1)]
    w.signal_tree(p, StopLevel.FORCE)
    assert ("terminate_job", "JOB") in calls and not p.killed
    q = _Proc()  # not adopted: falls back to Popen.kill
    q.pid = 4343
    assert w.stop_tree(q, 0.01) == 1 and q.killed
    w.release(p)
    assert 4242 not in w._jobs and ("close", "JOB") in calls
    done = _Proc(alive=False)
    w.signal_tree(done, StopLevel.FORCE)  # finished: nothing sent
    assert not done.killed


def test_termination_handlers_with_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: dict[int, Any] = {}
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
    monkeypatch.setattr(signal, "signal", lambda s, h: installed.__setitem__(int(s), h))
    seen: list[tuple[str, bool]] = []
    assert WindowsHost().install_termination_handlers(lambda n, i: seen.append((n, i)))
    assert set(installed) == {int(signal.SIGINT), 21}
    installed[int(signal.SIGINT)](int(signal.SIGINT), None)
    assert seen == [("SIGINT", True)]


def test_call_time_values_follow_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui import doctor, paths, vsenv

    for k in ("QTGMC6UI_PLUGIN_DIR", "QTGMC6UI_BUILD_INFO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ProgramFiles", r"D:\Apps")
    monkeypatch.setattr(osdep, "HOST", WindowsHost())
    root = Path(r"D:\Apps") / "qtgmc-6ui"
    assert paths.INSTALL_ROOT == root and paths.plugin_dir() == root / "plugins"
    assert paths.build_info_path() == root / "build-info.json"
    assert vsenv.REPO_PLUGINS[0].file.endswith(".dll") and vsenv.repo_plugins()[0].file.endswith(".dll")
    assert osdep.install_hint("ffmpeg") != "pacman -S ffmpeg" and "Arch" not in osdep.package_ref("ffmpeg")
    monkeypatch.setenv("APPDATA", "x")
    assert doctor._environment().get("APPDATA") == "x"


def test_windows_module_imports_first_without_cycle() -> None:
    import subprocess

    code = "import sys; sys.platform = 'win32'; import qtgmc_6ui.osdep_windows as w; print(w.WindowsHost.name)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "windows", r.stderr


def test_posix_stop_level_is_typed() -> None:
    with pytest.raises(TypeError):
        osdep.PosixHost()._signal_for(9)  # type: ignore[arg-type]


def test_find_plugin_windows_versioned_names(tmp_path: Path) -> None:
    w = WindowsHost()
    for n in ("BestSource-R22-win64-msvc.dll", "MVUtensils.AVX2.dll", "MVUtensils.dll", "readme.txt"):
        (tmp_path / n).write_bytes(b"")
    assert w.find_plugin(tmp_path, "bestsource") == tmp_path / "BestSource-R22-win64-msvc.dll"
    assert w.find_plugin(tmp_path, "mvutensils") == tmp_path / "MVUtensils.dll"
    assert w.find_plugin(tmp_path, "akarin") is None and w.find_plugin(tmp_path / "x", "akarin") is None
