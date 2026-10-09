"""Subprocess runner with line streaming and cooperative cancel (WP-D).

Used for ffprobe/ffmpeg (analysis, master, views), x264/x265/kvazaar, sox.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import IO


class CancelToken:
    """Set by signal handlers / stdin watcher; polled by long-running steps."""

    def __init__(self) -> None:
        self._ev = threading.Event()
        self.reason: str = ""

    def cancel(self, reason: str = "signal") -> None:
        self.reason = reason
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def wait(self, timeout: float) -> bool:
        return self._ev.wait(timeout)


def install_signal_handlers(token: CancelToken) -> None:
    """SIGINT/SIGTERM -> token.cancel(); second signal -> immediate exit 130."""
    raise NotImplementedError


def watch_stdin_for_cancel(token: CancelToken, stream: IO[str] | None = None) -> threading.Thread:
    """Daemon thread: a line ``cancel`` on stdin cancels (used by the GUI via QProcess)."""
    raise NotImplementedError


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr_tail: list[str] = field(default_factory=list)  # last ~50 lines for error reports
    cancelled: bool = False


LineCallback = Callable[[str], None]


def run_streaming(
    cmd: Sequence[str],
    *,
    on_stderr_line: LineCallback | None = None,
    on_stdout_line: LineCallback | None = None,
    stdin: IO[bytes] | int | None = None,
    cancel: CancelToken | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    kill_grace_s: float = 5.0,
    capture_stdout: bool = False,
) -> RunResult:
    """Popen, stream stderr split on both \\r and \\n (ffmpeg progress uses \\r), poll ``cancel``
    (SIGTERM, then SIGKILL after ``kill_grace_s``).  Never raises for non-zero exit; raises
    ComponentError(TOOL_MISSING) when the executable is absent."""
    raise NotImplementedError


def run_capture(cmd: Sequence[str], *, timeout: float | None = None, env: dict[str, str] | None = None) -> RunResult:
    """Short commands (ffprobe, -version, -encoders): captured stdout/stderr, text, utf-8/replace."""
    raise NotImplementedError


def subprocess_env() -> dict[str, str]:
    """os.environ + LC_CTYPE=C.UTF-8, PYTHONUTF8=1 (non-ASCII paths under LANG=C), AV_LOG_FORCE_NOCOLOR=1."""
    raise NotImplementedError


def remove_partial(paths: Sequence[str]) -> list[str]:
    """Delete partial outputs (ignore missing); return the removed ones."""
    raise NotImplementedError
