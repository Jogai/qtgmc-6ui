"""Windows host for osdep - EXPERIMENTAL, UNTESTED on Windows (written for a later port).

Import-safe everywhere: nothing Windows-only is touched at import time; every win32 call
happens inside a method.  Mapping of the POSIX behaviour:
  own process group      CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW (a console Ctrl-C does not
                         reach children, and console tools open no window when the parent - the
                         GUI-started CLI - has no console)
  die with parent        each adopted child goes into its own Job Object with
                         JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (via the Popen handle, no pid lookup);
                         the job handle dies with this process
  GRACEFUL stop          NOT AVAILABLE.  CTRL_BREAK_EVENT needs a console shared with the child,
                         which a GUI-started CLI (and CREATE_NO_WINDOW children) lack, so it would
                         fail silently.  graceful_stop_available() is False: stops go straight to
                         FORCE (outputs are ``.partial`` until verified, so this is safe).  The
                         GUI cancels the CLI with the stdin ``cancel`` line; the master encoder
                         stops when its y4m pipe closes.
  FORCE stop (tree)      TerminateJobObject (falls back to Popen.kill)
  termination handlers   SIGINT (Ctrl-C, interactive) and SIGBREAK (Ctrl-Break / console close)
  paths                  %APPDATA%, %LOCALAPPDATA%, %ProgramFiles%\\qtgmc-6ui
  native libraries       *<stem>*.dll (versioned names), found with find_plugin()
  child environment      PYTHONUTF8=1 only (no POSIX locale variables); no ASCII alias needed
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

from .osdep_base import APP_ID, StopLevel, TerminationCallback

log = logging.getLogger(__name__)

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


class WindowsHost:
    """Windows behaviour (experimental, untested)."""

    name = "windows"
    shared_lib_prefix = ""
    shared_lib_suffix = ".dll"
    plugin_glob = "*.dll"
    # A guess at CPU-variant naming; verify the real Windows plugin builds before relying on it.
    variant_suffix_re = re.compile(r"\.(avx2|avx512|zn4|znver4|x86[-_]64[-_]v[34]|native)\.dll$", re.IGNORECASE)
    diagnostic_env_keys = (
        "PYTHONUTF8", "APPDATA", "LOCALAPPDATA", "ProgramFiles", "QT_QPA_PLATFORM", "VAPOURSYNTH_EXTRA_PLUGIN_PATH",
        "QTGMC6UI_PLUGIN_DIR", "QTGMC6UI_BUILD_INFO", "QTGMC6UI_CONFIG_DIR", "QTGMC6UI_CACHE_DIR",
    )

    def __init__(self) -> None:
        self._jobs: dict[int, Any] = {}  # pid -> job handle
        self._lock = threading.Lock()
        self._k32: Any = None

    # ------------------------------------------------------------------ win32 helpers
    def _kernel32(self) -> Any:
        """kernel32 with explicit prototypes: HANDLEs are pointer-sized (no int truncation)."""
        if self._k32 is None:
            import ctypes
            from ctypes import wintypes

            k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            H, B, D = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD
            k.CreateJobObjectW.restype, k.CreateJobObjectW.argtypes = H, [ctypes.c_void_p, wintypes.LPCWSTR]
            k.SetInformationJobObject.restype = B
            k.SetInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, D]
            k.AssignProcessToJobObject.restype, k.AssignProcessToJobObject.argtypes = B, [H, H]
            k.TerminateJobObject.restype, k.TerminateJobObject.argtypes = B, [H, wintypes.UINT]
            k.CloseHandle.restype, k.CloseHandle.argtypes = B, [H]
            k.OpenProcess.restype, k.OpenProcess.argtypes = H, [D, B, D]
            k.GetExitCodeProcess.restype, k.GetExitCodeProcess.argtypes = B, [H, ctypes.POINTER(D)]
            self._k32 = k
        return self._k32

    def _last_error(self) -> OSError:
        import ctypes

        return ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]

    def _new_kill_on_close_job(self) -> Any:
        """A Job Object that kills its processes when its last handle closes; OSError on failure."""
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k = self._kernel32()
        job = k.CreateJobObjectW(None, None)
        if not job:
            raise self._last_error()
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
            err = self._last_error()
            k.CloseHandle(job)
            raise err
        return job

    # ------------------------------------------------------------------ process control
    def die_with_parent_hook(self, level: StopLevel = StopLevel.FORCE) -> Callable[[], None] | None:
        return None  # no preexec on Windows; see adopt()

    def popen_kwargs(self, die_with_parent: bool = True) -> dict[str, Any]:
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW}

    def graceful_stop_available(self) -> bool:
        return False  # see the module docstring

    def adopt(self, p: Any) -> None:
        """Put a started child into its own kill-on-close Job Object (die with parent + tree
        kill), through Popen's own process handle (no pid-reuse race).  Failures are logged."""
        job = None
        try:
            job = self._new_kill_on_close_job()
            if not self._kernel32().AssignProcessToJobObject(job, int(p._handle)):
                raise self._last_error()
        except Exception as e:  # noqa: BLE001 - the child still runs, just without the job
            log.warning("could not put pid %s into a kill-on-close job (no die-with-parent): %s", p.pid, e)
            if job:
                self._kernel32().CloseHandle(job)
            return
        with self._lock:
            self._jobs[p.pid] = job

    def release(self, p: Any) -> None:
        with self._lock:
            job = self._jobs.pop(p.pid, None)
        if job:
            try:
                self._kernel32().CloseHandle(job)
            except Exception:  # noqa: BLE001
                pass

    def signal_tree(self, p: Any, level: StopLevel) -> None:
        """FORCE: TerminateJobObject (tree), else Popen.kill.  GRACEFUL: unavailable, no-op."""
        if not isinstance(level, StopLevel):
            raise TypeError(f"expected a StopLevel, got {level!r}")
        if p.poll() is not None or level == StopLevel.GRACEFUL:
            return
        with self._lock:
            job = self._jobs.get(p.pid)
        try:
            if job and self._kernel32().TerminateJobObject(job, 1):
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            p.kill()
        except OSError:
            pass

    def stop_tree(self, p: Any, grace_s: float = 5.0) -> int:
        """No graceful stop exists, so no grace wait: FORCE at once."""
        self.signal_tree(p, StopLevel.FORCE)
        return p.wait()

    def stop_trees(self, procs: list[Any], grace_s: float = 5.0) -> None:
        for p in procs:
            self.signal_tree(p, StopLevel.FORCE)

    def process_alive(self, pid: int) -> bool:
        """Running per GetExitCodeProcess (a process whose real exit code is 259 reads as alive)."""
        import ctypes
        from ctypes import wintypes

        k = self._kernel32()
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # exists, not ours
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            k.CloseHandle(h)

    def install_termination_handlers(self, callback: TerminationCallback) -> bool:
        import signal

        if threading.current_thread() is not threading.main_thread():
            return False

        def handler(signum: int, _frame: Any) -> None:
            callback(signal.Signals(signum).name, signum == signal.SIGINT)

        signal.signal(signal.SIGINT, handler)
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            signal.signal(sigbreak, handler)
        return True

    # ------------------------------------------------------------------ paths
    def install_root(self) -> Path:
        return Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / APP_ID

    def config_base(self) -> Path:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")

    def cache_base(self) -> Path:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")

    def default_python_executable(self) -> str:
        return sys.executable

    def fallback_vs_plugin_dir(self) -> Path:
        return Path(sys.prefix) / "Lib" / "site-packages" / "vapoursynth" / "plugins"

    def system_vs_plugin_dir(self) -> Path | None:
        try:
            import vapoursynth  # noqa: F401
        except Exception:
            return None
        return Path(vapoursynth.__file__).resolve().parent / "plugins"

    def dir_writable(self, p: Path) -> bool:
        return p.is_dir() and os.access(p, os.W_OK)  # read-only attribute only, not ACLs

    # ------------------------------------------------------------------ native libraries
    def shared_lib_name(self, name: str) -> str:
        return f"{name}.dll"

    def find_plugin(self, directory: Path, stem: str) -> Path | None:
        """Windows plugin DLLs carry versions/toolchains in their names
        (``BestSource-R22-win64-msvc.dll``): case-insensitive ``*<stem>*.dll``, CPU variants
        skipped, first match in sorted order."""
        try:
            names = sorted(directory.iterdir())
        except OSError:
            return None
        s = stem.lower()
        for f in names:
            n = f.name.lower()
            if n.endswith(".dll") and s in n and not self.variant_suffix_re.search(f.name) and f.is_file():
                return f
        return None

    def install_hint(self, package: str) -> str:
        return f"get {package} for Windows and put it on PATH"

    def package_ref(self, package: str) -> str:
        return f"'{package}'"

    def lib_component(self, file_stem: str) -> str:
        return file_stem.removeprefix("lib")  # some Windows builds keep the lib prefix

    # ------------------------------------------------------------------ child environment
    def subprocess_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["AV_LOG_FORCE_NOCOLOR"] = "1"
        return env

    def tool_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env["AV_LOG_FORCE_NOCOLOR"] = "1"
        return env

    def force_utf8_environ(self, environ: MutableMapping[str, str]) -> None:
        environ.setdefault("PYTHONUTF8", "1")

    def utf8_locale_edits(self, get: Callable[[str], str]) -> tuple[list[str], dict[str, str]]:
        return [], {}

    def ascii_alias(self, path: str, links_dir: Path) -> str:
        return path  # Windows paths are UTF-16: every name is representable
