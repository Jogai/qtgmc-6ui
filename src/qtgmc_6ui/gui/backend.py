"""The only bridge to the backend: QProcess running the qtgmc-6ui CLI.

``CliCall`` runs a one-shot ``--json`` command; ``EventProcess`` runs an ``--events`` command and
turns stdout lines into ``events.Event`` objects.  The executable is ``sys.executable -m
qtgmc_6ui`` (the same venv as the GUI); ``QTGMC6UI_GUI_BACKEND`` overrides it with a shell-style
command line (tests point it at tests/fake_backend.py).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QByteArray, QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .. import osdep, runtime
from ..events import Event

ENV_BACKEND = "QTGMC6UI_GUI_BACKEND"

# The CLI finishes the in-flight frame(s) and removes partial output itself, which can take
# several seconds per frame at slow presets on HD.  Only escalate when it is clearly stuck.
CANCEL_TERMINATE_MS = int(runtime.FRONTEND_TERMINATE_AFTER_S * 1000)
CANCEL_KILL_MS = int(runtime.FRONTEND_KILL_AFTER_S * 1000)


def backend_command() -> tuple[str, list[str]]:
    override = os.environ.get(ENV_BACKEND, "").strip()
    if override:
        parts = shlex.split(override)
        return parts[0], parts[1:]
    return sys.executable, ["-m", "qtgmc_6ui"]


def backend_display() -> str:
    prog, args = backend_command()
    return shlex.join([prog, *args])


def process_environment() -> QProcessEnvironment:
    """Child environment: force UTF-8 so non-ASCII paths survive LANG=C."""
    env = QProcessEnvironment.systemEnvironment()
    env.insert("PYTHONUTF8", "1")
    env.insert("PYTHONIOENCODING", "utf-8")
    env.remove("PYTHONCOERCECLOCALE")
    remove, put = osdep.utf8_locale_edits(lambda k: env.value(k))
    for k in remove:
        if env.contains(k):
            env.remove(k)
    for k, v in put.items():
        env.insert(k, v)
    return env


def _new_process(parent: QObject | None) -> QProcess:
    proc = QProcess(parent)
    proc.setProcessEnvironment(process_environment())
    return proc


def decode(data: bytes | QByteArray) -> str:
    return bytes(data).decode("utf-8", errors="replace")


@dataclass
class CliResult:
    args: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    data: Any = None  # parsed JSON document (None when stdout was not JSON)
    error: dict[str, Any] | None = None  # {"code","message","hint",...} from {"error": ...} or synthesised
    crashed: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.error is None and not self.crashed


def _parse_cli_result(args: list[str], exit_code: int, crashed: bool, out: str, err: str) -> CliResult:
    res = CliResult(args, exit_code, out, err, crashed=crashed)
    text = out.strip()
    if text:
        try:
            res.data = json.loads(text)
        except ValueError:
            res.data = None
    if isinstance(res.data, dict) and isinstance(res.data.get("error"), dict):
        res.error = res.data["error"]
    elif exit_code != 0 or crashed:
        tail = "\n".join(err.strip().splitlines()[-15:])
        res.error = {
            "code": "backend_failed",
            "message": f"The backend could not complete '{args[0] if args else ''}' (exit code {exit_code}).",
            "hint": "The log pane shows the backend output.",
            "details": {"stderr": tail},
            "exit_code": exit_code,
        }
    return res


class CliCall(QObject):
    """One-shot CLI command collecting stdout (JSON) and stderr; emits ``finished(CliResult)``."""

    finished = Signal(object)

    def __init__(self, args: list[str], parent: QObject | None = None, stdin: bytes | None = None) -> None:
        super().__init__(parent)
        self.args = list(args)
        self._stdin = stdin
        self._out = bytearray()
        self._err = bytearray()
        self._done = False
        self.proc = _new_process(self)
        self.proc.readyReadStandardOutput.connect(self._read_out)
        self.proc.readyReadStandardError.connect(self._read_err)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)

    def start(self) -> CliCall:
        prog, base = backend_command()
        self.proc.start(prog, [*base, *self.args])
        if self._stdin is not None:
            self.proc.write(self._stdin)
            self.proc.closeWriteChannel()
        return self

    def kill(self) -> None:
        """Stop the process and drop its result (used when the owner goes away)."""
        self._done = True
        if self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(1000)

    def _read_out(self) -> None:
        self._out.extend(bytes(self.proc.readAllStandardOutput()))

    def _read_err(self) -> None:
        self._err.extend(bytes(self.proc.readAllStandardError()))

    def _on_error(self, err: QProcess.ProcessError) -> None:
        if err == QProcess.ProcessError.FailedToStart:
            self._finish(127, True, f"cannot start backend: {backend_display()} ({self.proc.errorString()})")

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        if self._done:
            return
        self._read_out()
        self._read_err()
        self._finish(code, status == QProcess.ExitStatus.CrashExit, "")

    def _finish(self, code: int, crashed: bool, extra_err: str) -> None:
        if self._done:
            return
        self._done = True
        err = decode(bytes(self._err)) + extra_err
        self.finished.emit(_parse_cli_result(self.args, code, crashed, decode(bytes(self._out)), err))


class LineBuffer:
    """Splits a byte stream into complete UTF-8 lines (partial tail kept until the next chunk)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[str]:
        self._buf.extend(data)
        *lines, rest = self._buf.split(b"\n")
        self._buf = bytearray(rest)
        return [decode(ln).rstrip("\r") for ln in lines]

    def flush(self) -> list[str]:
        if not self._buf:
            return []
        line, self._buf = decode(bytes(self._buf)), bytearray()
        return [line]


@dataclass
class ProcessOutcome:
    exit_code: int
    crashed: bool = False
    start_error: str = ""
    stderr_tail: list[str] = field(default_factory=list)


class EventProcess(QObject):
    """Long-running ``--events`` command.

    Signals: ``event(Event)`` per protocol line, ``text(str, str)`` for non-protocol output
    (stream name, line), ``finished(ProcessOutcome)`` exactly once.
    Cancel follows the CLI semantics: ``cancel`` on stdin (the CLI stops, deletes partial output and
    confirms with a ``cancelled`` event); terminate() only after 20 s, kill() after 45 s.
    ``escalated(str)`` reports "terminate" / "kill" so the UI can say so honestly.
    """

    event = Signal(object)
    text = Signal(str, str)
    finished = Signal(object)
    escalated = Signal(str)

    def __init__(self, args: list[str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.args = list(args)
        self._out = LineBuffer()
        self._err = LineBuffer()
        self._err_tail: list[str] = []
        self._done = False
        self.cancel_requested = False
        self.killed = False
        self.proc = _new_process(self)
        self.proc.readyReadStandardOutput.connect(self._read_out)
        self.proc.readyReadStandardError.connect(self._read_err)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self._term_timer = QTimer(self, singleShot=True, interval=CANCEL_TERMINATE_MS)
        self._term_timer.timeout.connect(self._terminate)
        self._kill_timer = QTimer(self, singleShot=True, interval=CANCEL_KILL_MS)
        self._kill_timer.timeout.connect(self._kill)

    def start(self) -> EventProcess:
        prog, base = backend_command()
        self.proc.start(prog, [*base, *self.args])
        return self

    def is_running(self) -> bool:
        return self.proc.state() != QProcess.ProcessState.NotRunning

    def cancel(self) -> None:
        if not self.is_running() or self.cancel_requested:
            return
        self.cancel_requested = True
        self.proc.write(b"cancel\n")
        self._term_timer.start()
        self._kill_timer.start()

    def _terminate(self) -> None:
        if self.is_running():
            self.escalated.emit("terminate")
            self.proc.terminate()

    def _kill(self) -> None:
        if self.is_running():
            self.killed = True
            self.escalated.emit("kill")
            self.proc.kill()

    def _read_out(self) -> None:
        for line in self._out.feed(bytes(self.proc.readAllStandardOutput())):
            self._dispatch_out(line)

    def _read_err(self) -> None:
        for line in self._err.feed(bytes(self.proc.readAllStandardError())):
            self._dispatch_err(line)

    def _dispatch_out(self, line: str) -> None:
        if not line.strip():
            return
        ev = Event.from_json(line)
        if ev is None:
            self.text.emit("stdout", line)
        else:
            self.event.emit(ev)

    def _dispatch_err(self, line: str) -> None:
        if not line.strip():
            return
        self._err_tail = (self._err_tail + [line])[-20:]
        self.text.emit("stderr", line)

    def _on_error(self, err: QProcess.ProcessError) -> None:
        if err == QProcess.ProcessError.FailedToStart:
            self._finish(ProcessOutcome(127, True, f"cannot start backend: {backend_display()} ({self.proc.errorString()})"))

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._read_out()
        self._read_err()
        for line in self._out.flush():
            self._dispatch_out(line)
        for line in self._err.flush():
            self._dispatch_err(line)
        self._finish(ProcessOutcome(code, status == QProcess.ExitStatus.CrashExit, "", list(self._err_tail)))

    def _finish(self, outcome: ProcessOutcome) -> None:
        if self._done:
            return
        self._done = True
        self._term_timer.stop()
        self._kill_timer.stop()
        self.finished.emit(outcome)
