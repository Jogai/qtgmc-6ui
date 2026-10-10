"""Subprocess runner with line streaming and cooperative cancel (WP-D).

Used for ffprobe/ffmpeg (analysis, master, views), x264/x265/kvazaar, sox.

Children run in their own process group (osdep.popen_kwargs) so a terminal Ctrl-C reaches
only this process; cancel then stops each child tree in order: GRACEFUL, FORCE after
``kill_grace_s`` (SIGTERM/SIGKILL on POSIX; all OS specifics live in osdep).  Long-running steps either pass a CancelToken explicitly or rely on the
process-wide default token (``set_default_cancel``), which the CLI installs for every command.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import IO, Any

from . import osdep
from .errors import ComponentError, ErrorCode
from .osdep import StopLevel

TAIL_LINES = 50
_POLL_S = 0.05

# Cancel timings, the single source for the CLI and the GUI's QProcess escalation.
KILL_GRACE_S = 5.0  # a stage's encoder: GRACEFUL stop, FORCE after this
CANCEL_GRACE_S = 15.0  # the CLI waits this long for a stage to clean up after a cancel
FRONTEND_TERMINATE_AFTER_S = 20.0  # a front end sends its terminate only after this
FRONTEND_KILL_AFTER_S = 45.0  # ... and kills only after this


class CancelToken:
    """Set by signal handlers / stdin watcher; polled by long-running steps."""

    def __init__(self) -> None:
        self._ev = threading.Event()
        self.reason: str = ""

    def cancel(self, reason: str = "signal") -> None:
        if not self._ev.is_set():
            self.reason = reason
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def wait(self, timeout: float) -> bool:
        return self._ev.wait(timeout)


_default_cancel: CancelToken | None = None


def set_default_cancel(token: CancelToken | None) -> None:
    """Token used by run_streaming/run_capture when the caller passes none (CLI sets it)."""
    global _default_cancel
    _default_cancel = token


def default_cancel() -> CancelToken | None:
    return _default_cancel


# --------------------------------------------------------------------------- live children
_live: set[subprocess.Popen[Any]] = set()
_live_lock = threading.Lock()


def _register(p: subprocess.Popen[Any]) -> None:
    with _live_lock:
        _live.add(p)
    osdep.adopt(p)


def _unregister(p: subprocess.Popen[Any]) -> None:
    with _live_lock:
        _live.discard(p)
    osdep.release(p)


def track(p: subprocess.Popen[Any]) -> None:
    """Register a child started outside this module (own Popen) so cancel timeouts and the
    hard-exit path kill it too; pair with untrack() after wait().  Start such children with
    ``**osdep.popen_kwargs()``."""
    _register(p)


def untrack(p: subprocess.Popen[Any]) -> None:
    _unregister(p)


def kill_all_children(level: StopLevel = StopLevel.FORCE) -> None:
    """Stop (``level``) every tracked child tree that is still running."""
    with _live_lock:
        procs = list(_live)
    for p in procs:
        osdep.signal_tree(p, level)


def terminate(p: subprocess.Popen[Any], grace_s: float = KILL_GRACE_S) -> int:
    """GRACEFUL stop of the child's tree, FORCE after ``grace_s``; returns the exit status."""
    return osdep.stop_tree(p, grace_s)


# --------------------------------------------------------------------------- signals / stdin
_hard_exit_hooks: list[Callable[[], None]] = []


def on_hard_exit(fn: Callable[[], None]) -> Callable[[], None]:
    """Register cleanup that must run even on a hard exit (after children are killed), e.g.
    removing the partial outputs of the running job.  Returns ``fn`` (for remove_hard_exit)."""
    _hard_exit_hooks.append(fn)
    return fn


def remove_hard_exit(fn: Callable[[], None]) -> None:
    try:
        _hard_exit_hooks.remove(fn)
    except ValueError:
        pass


def hard_exit(code: int = 130) -> None:
    """Kill every child, run the hard-exit hooks (partial-output cleanup, final events), exit."""
    kill_all_children(StopLevel.FORCE)
    for fn in list(_hard_exit_hooks):
        try:
            fn()
        except BaseException:  # noqa: BLE001 - exiting anyway
            pass
    os._exit(code)


def install_signal_handlers(token: CancelToken) -> None:
    """SIGINT/SIGTERM/stdin "cancel" all set the same token (one cooperative cancel).

    Repeats are the same cancel: a SIGTERM after a stdin ``cancel`` (the GUI's escalation)
    or a second SIGTERM does NOT abort the cleanup.  Only a second SIGINT after a SIGINT
    (impatient Ctrl-C Ctrl-C in a terminal) exits at once - and even then children are
    killed and the hard-exit hooks remove the partial outputs first (hard_exit).

    No-op outside the main thread (signal handlers can only be set there)."""

    def on_signal(name: str, interactive: bool) -> None:
        if token.cancelled:
            if interactive and token.reason == name:
                hard_exit(130)
            return
        token.cancel(name)

    osdep.install_termination_handlers(on_signal)


def watch_stdin_for_cancel(token: CancelToken, stream: IO[str] | None = None) -> threading.Thread:
    """Daemon thread: a line ``cancel`` on stdin cancels (used by the GUI via QProcess).
    EOF ends the watch without cancelling."""
    src = stream if stream is not None else sys.stdin

    def run() -> None:
        try:
            for line in src:
                if line.strip().lower() == "cancel":
                    token.cancel("stdin")
                    return
        except (OSError, ValueError):
            return

    th = threading.Thread(target=run, name="stdin-cancel", daemon=True)
    th.start()
    return th


# --------------------------------------------------------------------------- running
@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr_tail: list[str] = field(default_factory=list)  # last ~50 lines for error reports
    cancelled: bool = False
    stderr: str = ""  # full stderr: run_capture always, run_streaming with keep_stderr=True
    timed_out: bool = False


LineCallback = Callable[[str], None]

_LINE_SPLIT = re.compile(rb"\r\n|\r|\n")


def _pump_lines(stream: IO[bytes], on_line: Callable[[str], None]) -> None:
    """Read raw chunks, split on \\r and \\n, call ``on_line`` per non-empty decoded line."""
    buf = b""
    fd = stream.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            break
        buf += chunk
        parts = _LINE_SPLIT.split(buf)
        buf = parts.pop()  # "\r" + "\n" split across chunks only yields an (ignored) empty line
        for part in parts:
            if part:
                on_line(part.decode("utf-8", "replace"))
    if buf:
        on_line(buf.decode("utf-8", "replace"))
    try:
        stream.close()
    except OSError:
        pass


def _tool_missing(cmd: Sequence[str], exc: OSError) -> ComponentError:
    tool = os.path.basename(str(cmd[0]))
    hints = {
        "ffmpeg": "install ffmpeg (pacman -S ffmpeg)",
        "ffprobe": "install ffmpeg (pacman -S ffmpeg)",
        "x264": "install x264 (pacman -S x264) or choose a profile that uses ffmpeg's libx264",
        "x265": "install x265 (pacman -S x265) or choose a profile that uses ffmpeg's libx265",
        "kvazaar": "install kvazaar (pacman -S kvazaar) or choose another profile",
        "sox": "install sox (pacman -S sox) or disable the sox step in the profile",
    }
    return ComponentError(
        ErrorCode.TOOL_MISSING,
        f"{tool} could not be started: {exc.strerror or exc}",
        hints.get(tool, f"install {tool} or check PATH"),
        {"command": list(map(str, cmd))},
    )


def pdeathsig_preexec(level: StopLevel = StopLevel.FORCE) -> Callable[[], None] | None:
    """Compatibility name for osdep.die_with_parent_hook()."""
    return osdep.die_with_parent_hook(level)


class StreamingProcess:
    """A started child whose stderr (and optionally stdout) is pumped line-wise on threads.

    For callers that must feed or read the pipes themselves (pipeline: y4m into ffmpeg stdin;
    viewcopy: ffmpeg | x264).  ``run_streaming`` is the blocking convenience on top of it.
    ``die_with_parent``: the child is killed when this process dies (osdep.popen_kwargs)."""

    def __init__(
        self,
        cmd: Sequence[str],
        *,
        on_stderr_line: LineCallback | None = None,
        on_stdout_line: LineCallback | None = None,
        stdin: IO[bytes] | int | None = None,
        stdout: IO[bytes] | int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        capture_stdout: bool = False,
        keep_stderr: bool = False,
        die_with_parent: bool = True,
    ) -> None:
        self.cmd = [str(c) for c in cmd]
        self.tail: deque[str] = deque(maxlen=TAIL_LINES)
        self._stderr_all: list[str] | None = [] if keep_stderr else None
        self._stdout_lines: list[str] = []
        self._on_err = on_stderr_line
        self._on_out = on_stdout_line
        want_stdout_pipe = capture_stdout or on_stdout_line is not None
        if want_stdout_pipe:
            out: Any = subprocess.PIPE
        elif stdout is not None:
            out = stdout
        else:
            out = subprocess.DEVNULL  # never let a child write into our (event) stdout
        try:
            self.proc: subprocess.Popen[bytes] = subprocess.Popen(
                self.cmd,
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.PIPE,
                env=env if env is not None else subprocess_env(),
                cwd=cwd,
                **osdep.popen_kwargs(die_with_parent),
            )
        except (FileNotFoundError, PermissionError, NotADirectoryError) as e:
            raise _tool_missing(cmd, e) from None
        _register(self.proc)
        self._threads: list[threading.Thread] = []
        assert self.proc.stderr is not None
        self._threads.append(self._spawn(self.proc.stderr, self._err_line, "stderr"))
        if want_stdout_pipe:
            assert self.proc.stdout is not None
            self._threads.append(self._spawn(self.proc.stdout, self._out_line, "stdout"))

    def _spawn(self, stream: IO[bytes], cb: Callable[[str], None], name: str) -> threading.Thread:
        th = threading.Thread(target=_pump_lines, args=(stream, cb), name=f"pump-{name}", daemon=True)
        th.start()
        return th

    def _err_line(self, line: str) -> None:
        self.tail.append(line)
        if self._stderr_all is not None:
            self._stderr_all.append(line)
        if self._on_err:
            self._on_err(line)

    def _out_line(self, line: str) -> None:
        self._stdout_lines.append(line)
        if self._on_out:
            self._on_out(line)

    @property
    def stdin(self) -> IO[bytes] | None:
        return self.proc.stdin

    @property
    def stdout(self) -> IO[bytes] | None:
        return self.proc.stdout

    def poll(self) -> int | None:
        return self.proc.poll()

    def terminate(self, grace_s: float = KILL_GRACE_S) -> int:
        return terminate(self.proc, grace_s)

    def wait(self, cancel: CancelToken | None = None, kill_grace_s: float = KILL_GRACE_S, timeout: float | None = None) -> RunResult:
        """Wait for exit while polling ``cancel``; drains the pump threads; never raises."""
        cancelled = timed_out = False
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                self.proc.wait(_POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.cancelled:
                cancelled = True
                self.terminate(kill_grace_s)
                break
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                self.terminate(kill_grace_s)
                break
        for th in self._threads:
            th.join(2.0)
        _unregister(self.proc)
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        tail = list(self.tail)
        if timed_out:
            tail.append(f"(timed out after {timeout}s)")
        return RunResult(
            returncode=self.proc.returncode,
            stdout="\n".join(self._stdout_lines) + ("\n" if self._stdout_lines else ""),
            stderr_tail=tail,
            cancelled=cancelled,
            stderr="\n".join(self._stderr_all) if self._stderr_all is not None else "",
            timed_out=timed_out,
        )


def spawn(cmd: Sequence[str], **kw: Any) -> StreamingProcess:
    """Start ``cmd`` (see StreamingProcess for keyword arguments); raises TOOL_MISSING."""
    return StreamingProcess(cmd, **kw)


def run_streaming(
    cmd: Sequence[str],
    *,
    on_stderr_line: LineCallback | None = None,
    on_stdout_line: LineCallback | None = None,
    stdin: IO[bytes] | int | None = None,
    cancel: CancelToken | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    kill_grace_s: float = KILL_GRACE_S,
    capture_stdout: bool = False,
    stdout: IO[bytes] | int | None = None,
    keep_stderr: bool = False,
    timeout: float | None = None,
    die_with_parent: bool = True,
) -> RunResult:
    """Popen, stream stderr split on both \\r and \\n (ffmpeg progress uses \\r), poll ``cancel``
    (GRACEFUL stop, then FORCE after ``kill_grace_s``).  Never raises for non-zero exit; raises
    ComponentError(TOOL_MISSING) when the executable is absent.

    ``cancel`` defaults to the process-wide token (set_default_cancel).  ``stdout`` may be a
    file/fd to redirect into; without it (and without capture) stdout goes to /dev/null."""
    sp = StreamingProcess(
        cmd,
        on_stderr_line=on_stderr_line,
        on_stdout_line=on_stdout_line,
        stdin=stdin,
        stdout=stdout,
        env=env,
        cwd=cwd,
        capture_stdout=capture_stdout,
        keep_stderr=keep_stderr,
        die_with_parent=die_with_parent,
    )
    tok = cancel if cancel is not None else _default_cancel
    return sp.wait(tok, kill_grace_s, timeout)


def run_capture(cmd: Sequence[str], *, timeout: float | None = None, env: dict[str, str] | None = None) -> RunResult:
    """Short commands (ffprobe, -version, -encoders): captured stdout/stderr, text, utf-8/replace.

    Honours the process-wide cancel token; a timeout kills the child and sets ``timed_out``."""
    try:
        p = subprocess.Popen(
            [str(c) for c in cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env if env is not None else subprocess_env(),
            **osdep.popen_kwargs(),
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as e:
        raise _tool_missing(cmd, e) from None
    _register(p)
    deadline = None if timeout is None else time.monotonic() + timeout
    cancelled = timed_out = False
    out_b = err_b = b""
    try:
        while True:
            try:
                o, e = p.communicate(timeout=0.1)
                out_b += o or b""
                err_b += e or b""
                break
            except subprocess.TimeoutExpired:
                tok = _default_cancel
                if tok is not None and tok.cancelled:
                    cancelled = True
                elif deadline is not None and time.monotonic() > deadline:
                    timed_out = True
                else:
                    continue
                osdep.signal_tree(p, StopLevel.FORCE)
                o, e = p.communicate()
                out_b += o or b""
                err_b += e or b""
                break
    finally:
        _unregister(p)
    err = err_b.decode("utf-8", "replace")
    lines = [ln for ln in _LINE_SPLIT.split(err_b) if ln]
    tail = [ln.decode("utf-8", "replace") for ln in lines[-TAIL_LINES:]]
    if timed_out:
        tail.append(f"(timed out after {timeout}s)")
    return RunResult(
        returncode=p.returncode,
        stdout=out_b.decode("utf-8", "replace"),
        stderr_tail=tail,
        cancelled=cancelled,
        stderr=err,
        timed_out=timed_out,
    )


def subprocess_env() -> dict[str, str]:
    """Child environment with a UTF-8 locale (osdep.subprocess_env)."""
    return osdep.subprocess_env()


def remove_partial(paths: Sequence[str]) -> list[str]:
    """Delete partial outputs (ignore missing); return the removed ones."""
    removed = []
    for p in paths:
        if not p or p == "-":
            continue
        try:
            if os.path.isfile(p) or os.path.islink(p):
                os.unlink(p)
                removed.append(str(p))
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------- log capture
_STATS_RE = re.compile(r"^\s*(frame=|size=|progress=|\[\s*\d+(\.\d+)?%\]|\d+ frames:)")


def log_level(line: str) -> str:
    """Heuristic level for a tool's stderr line: error | warning | debug (progress) | info."""
    low = line.lower()
    if _STATS_RE.match(line):
        return "debug"
    if "error" in low or "invalid" in low or "failed" in low or low.startswith("conversion failed"):
        return "error"
    if "warning" in low or "deprecated" in low:
        return "warning"
    return "info"


def log_forwarder(emitter: Any, source: str, also: LineCallback | None = None) -> LineCallback:
    """LineCallback that forwards each line as a ``log`` event (and optionally to ``also``)."""

    def cb(line: str) -> None:
        if emitter is not None:
            emitter.log(source, line, log_level(line))
        if also is not None:
            also(line)

    return cb
