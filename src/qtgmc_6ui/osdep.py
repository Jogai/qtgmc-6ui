"""The one seam for OS-dependent behaviour (processes, signals, paths, native libraries,
child environment).  Everything else in the package calls the functions here and never
touches process groups, signal numbers, XDG/AppData paths or ``.so`` names itself
(tests/test_osdep_boundary.py enforces that).

The host is chosen at import time from ``sys.platform``: ``PosixHost`` (Linux, the real
target) or ``osdep_windows.WindowsHost`` (experimental).  Module-level functions delegate to
``HOST`` so tests can swap it.  Stop levels are abstract: ``GRACEFUL`` (SIGTERM on POSIX) and
``FORCE`` (SIGKILL).
"""

from __future__ import annotations

import os
import re
import sys
import threading
from collections.abc import Callable, MutableMapping
from enum import Enum
from pathlib import Path
from typing import Any

APP_ID = "qtgmc-6ui"


class StopLevel(Enum):
    GRACEFUL = "graceful"  # ask the child (tree) to stop: SIGTERM / CTRL_BREAK
    FORCE = "force"  # no negotiation: SIGKILL / TerminateJobObject


# "Termination" callback: (signal name for messages/reasons, interactive) where interactive is
# True for the terminal interrupt (Ctrl-C), whose repetition means "exit now".
TerminationCallback = Callable[[str, bool], None]


def _is_utf8_locale(value: str) -> bool:
    v = value.lower().replace("-", "")
    return "utf8" in v


class PosixHost:
    """Linux/POSIX behaviour (what the package always did)."""

    name = "posix"
    shared_lib_prefix = "lib"
    shared_lib_suffix = ".so"
    plugin_glob = "*.so"
    # mvutensils builds three variants with one plugin id; only the baseline is loaded.
    variant_suffix_re = re.compile(r"\.(avx2|avx512|zn4|znver4|x86[-_]64[-_]v[34]|native)\.so$")
    diagnostic_env_keys = (
        "LANG", "LC_ALL", "LC_CTYPE", "PYTHONUTF8", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "QT_QPA_PLATFORM",
        "VAPOURSYNTH_EXTRA_PLUGIN_PATH", "QTGMC6UI_PLUGIN_DIR", "QTGMC6UI_BUILD_INFO", "QTGMC6UI_CONFIG_DIR",
        "QTGMC6UI_CACHE_DIR",
    )

    def __init__(self) -> None:
        self._prctl: Any = None

    # ------------------------------------------------------------------ process control
    def _signal_for(self, level: StopLevel) -> int:
        import signal

        return signal.SIGKILL if level == StopLevel.FORCE else signal.SIGTERM

    def _get_prctl(self) -> Any:
        """libc prctl, resolved in the parent: the forked child must not import or dlopen."""
        if self._prctl is None:
            try:
                import ctypes

                self._prctl = ctypes.CDLL(None, use_errno=True).prctl
            except (OSError, AttributeError):
                self._prctl = False
        return self._prctl

    def die_with_parent_hook(self, level: StopLevel = StopLevel.FORCE) -> Callable[[], None] | None:
        """preexec_fn that makes the child die (``level``) when its parent exits (Linux
        PR_SET_PDEATHSIG), so a killed CLI never leaves an orphan encoder behind.

        Caveat (kernel semantics): the signal fires when the *thread* that started the child
        exits, so start children from a thread that outlives them (callers wait on them)."""
        prctl = self._get_prctl()
        if not prctl:
            return None
        ppid = os.getpid()
        sig = int(self._signal_for(level))

        def preexec() -> None:
            prctl(1, sig, 0, 0, 0)  # PR_SET_PDEATHSIG
            if os.getppid() != ppid:  # parent already gone before prctl took effect
                os._exit(1)

        return preexec

    def popen_kwargs(self, die_with_parent: bool = True) -> dict[str, Any]:
        """Popen arguments for every child: its own session/process group (a terminal Ctrl-C
        reaches only us; stops address the whole tree) and, by default, death with its parent."""
        return {"start_new_session": True, "preexec_fn": self.die_with_parent_hook() if die_with_parent else None}

    def signal_tree(self, p: Any, level: StopLevel) -> None:
        """Signal a child started with popen_kwargs() and everything in its process group."""
        if p.poll() is not None:
            return
        sig = self._signal_for(level)
        try:
            os.killpg(p.pid, sig)  # pid == pgid because of start_new_session
        except (ProcessLookupError, PermissionError):
            try:
                p.send_signal(sig)
            except OSError:
                pass

    def stop_tree(self, p: Any, grace_s: float = 5.0) -> int:
        """GRACEFUL, then FORCE after ``grace_s``; returns the exit status."""
        import subprocess

        self.signal_tree(p, StopLevel.GRACEFUL)
        try:
            return p.wait(grace_s)
        except subprocess.TimeoutExpired:
            self.signal_tree(p, StopLevel.FORCE)
            return p.wait()

    def stop_trees(self, procs: list[Any], grace_s: float = 5.0) -> None:
        """GRACEFUL to all, wait up to ``grace_s`` for all, FORCE the rest (pipelines)."""
        import time

        for p in procs:
            self.signal_tree(p, StopLevel.GRACEFUL)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and any(p.poll() is None for p in procs):
            time.sleep(0.05)
        for p in procs:
            self.signal_tree(p, StopLevel.FORCE)

    def process_alive(self, pid: int) -> bool:
        """True while ``pid`` runs (zombies count as dead; unknown counts as alive)."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        st = Path(f"/proc/{pid}/stat")
        try:
            return st.read_text().split(") ")[-1].split()[0] != "Z"
        except OSError:
            return True

    def install_termination_handlers(self, callback: TerminationCallback) -> bool:
        """SIGINT -> callback("SIGINT", True); SIGTERM -> callback("SIGTERM", False).
        Returns False (nothing installed) outside the main thread."""
        import signal

        if threading.current_thread() is not threading.main_thread():
            return False

        def handler(signum: int, _frame: Any) -> None:
            callback(signal.Signals(signum).name, signum == signal.SIGINT)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
        return True

    # ------------------------------------------------------------------ paths
    def install_root(self) -> Path:
        return Path("/usr/lib/qtgmc-6ui")

    def config_base(self) -> Path:
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")

    def cache_base(self) -> Path:
        return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")

    def default_python_executable(self) -> str:
        return "/usr/bin/python3.14"

    def fallback_vs_plugin_dir(self) -> Path:
        """Where Arch's VapourSynth keeps repo plugins (for scripts when vapoursynth is absent)."""
        return Path("/usr/lib/python3.14/site-packages/vapoursynth/plugins")

    def system_vs_plugin_dir(self) -> Path | None:
        """The system VapourSynth plugin dir (<site-packages>/vapoursynth/plugins); None if VS is absent."""
        try:
            import vapoursynth  # noqa: F401  (import only; never touches vs.core)
        except Exception:
            return None
        return Path(vapoursynth.__file__).resolve().parent / "plugins"

    def dir_writable(self, p: Path) -> bool:
        return p.is_dir() and os.access(p, os.W_OK | os.X_OK)

    # ------------------------------------------------------------------ native libraries
    def shared_lib_name(self, name: str) -> str:
        return f"{self.shared_lib_prefix}{name}{self.shared_lib_suffix}"

    def lib_component(self, file_stem: str) -> str:
        return file_stem.removeprefix(self.shared_lib_prefix)

    # ------------------------------------------------------------------ child environment
    def subprocess_env(self) -> dict[str, str]:
        """os.environ + LC_CTYPE=C.UTF-8, PYTHONUTF8=1 (non-ASCII paths under LANG=C), AV_LOG_FORCE_NOCOLOR=1."""
        env = dict(os.environ)
        lc_all = env.get("LC_ALL", "")
        if lc_all and not _is_utf8_locale(lc_all):
            env["LC_ALL"] = "C.UTF-8"
        if not _is_utf8_locale(env.get("LC_CTYPE", "") or env.get("LANG", "")):
            env["LC_CTYPE"] = "C.UTF-8"
        env["PYTHONUTF8"] = "1"
        env["AV_LOG_FORCE_NOCOLOR"] = "1"
        return env

    def tool_env(self) -> dict[str, str]:
        """os.environ plus a UTF-8 ctype for children and colour-free ffmpeg logs (ffprobe flavour:
        an explicit PYTHONUTF8 is respected)."""
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        ctype = env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or ""
        if "utf-8" not in ctype.lower().replace("utf8", "utf-8"):
            env["LC_CTYPE"] = "C.UTF-8"
            if "LC_ALL" in env:
                # LC_ALL=C would override LC_CTYPE; narrow it to the same UTF-8 ctype
                env["LC_ALL"] = "C.UTF-8"
        env["AV_LOG_FORCE_NOCOLOR"] = "1"
        return env

    def force_utf8_environ(self, environ: MutableMapping[str, str]) -> None:
        """In place (the GUI's own environment, inherited by its children)."""
        environ.setdefault("PYTHONUTF8", "1")
        loc = environ.get("LC_ALL") or environ.get("LC_CTYPE") or environ.get("LANG") or ""
        if "utf" not in loc.lower().replace("-", ""):
            environ["LC_CTYPE"] = "C.UTF-8"

    def utf8_locale_edits(self, get: Callable[[str], str]) -> tuple[list[str], dict[str, str]]:
        """(variables to remove, variables to set) that give a child a UTF-8 ctype; ``get``
        reads the current value ("" when unset).  Used for QProcessEnvironment."""
        effective = get("LC_ALL") or get("LC_CTYPE") or get("LANG")
        if _is_utf8_locale(effective):
            return [], {}
        return ["LC_ALL"], {"LC_CTYPE": "C.UTF-8"}

    def ascii_alias(self, path: str, links_dir: Path) -> str:
        """A name that is reachable as UTF-8 for a path that is not valid UTF-8 (surrogate-escaped
        bytes): an ASCII-named symlink in ``links_dir``.  Raises OSError if it cannot be made."""
        import hashlib

        links_dir.mkdir(parents=True, exist_ok=True)
        raw = os.fsencode(os.path.abspath(path))
        ext = os.path.splitext(path)[1].encode("utf-8", "replace").decode("ascii", "ignore")
        link = links_dir / (hashlib.sha1(raw).hexdigest() + ext)
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(raw, link)
        return str(link)


def _select() -> Any:
    if sys.platform == "win32":
        from .osdep_windows import WindowsHost

        return WindowsHost()
    return PosixHost()


HOST: Any = _select()


# ---------------------------------------------------------------------- module API (delegates)
def popen_kwargs(die_with_parent: bool = True) -> dict[str, Any]:
    return HOST.popen_kwargs(die_with_parent)


def die_with_parent_hook(level: StopLevel = StopLevel.FORCE) -> Callable[[], None] | None:
    return HOST.die_with_parent_hook(level)


def signal_tree(p: Any, level: StopLevel) -> None:
    HOST.signal_tree(p, level)


def stop_tree(p: Any, grace_s: float = 5.0) -> int:
    return HOST.stop_tree(p, grace_s)


def stop_trees(procs: list[Any], grace_s: float = 5.0) -> None:
    HOST.stop_trees(procs, grace_s)


def process_alive(pid: int) -> bool:
    return HOST.process_alive(pid)


def install_termination_handlers(callback: TerminationCallback) -> bool:
    return HOST.install_termination_handlers(callback)


def install_root() -> Path:
    return HOST.install_root()


def config_base() -> Path:
    return HOST.config_base()


def cache_base() -> Path:
    return HOST.cache_base()


def default_python_executable() -> str:
    return HOST.default_python_executable()


def fallback_vs_plugin_dir() -> Path:
    return HOST.fallback_vs_plugin_dir()


def system_vs_plugin_dir() -> Path | None:
    return HOST.system_vs_plugin_dir()


def dir_writable(p: Path) -> bool:
    return HOST.dir_writable(p)


def shared_lib_name(name: str) -> str:
    return HOST.shared_lib_name(name)


def lib_component(file_stem: str) -> str:
    return HOST.lib_component(file_stem)


def plugin_glob() -> str:
    return HOST.plugin_glob


def variant_suffix_re() -> re.Pattern[str]:
    return HOST.variant_suffix_re


def diagnostic_env_keys() -> tuple[str, ...]:
    return HOST.diagnostic_env_keys


def subprocess_env() -> dict[str, str]:
    return HOST.subprocess_env()


def tool_env() -> dict[str, str]:
    return HOST.tool_env()


def force_utf8_environ(environ: MutableMapping[str, str]) -> None:
    HOST.force_utf8_environ(environ)


def utf8_locale_edits(get: Callable[[str], str]) -> tuple[list[str], dict[str, str]]:
    return HOST.utf8_locale_edits(get)


def ascii_alias(path: str, links_dir: Path) -> str:
    return HOST.ascii_alias(path, links_dir)
