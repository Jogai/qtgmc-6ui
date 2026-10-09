"""Client for ``qtgmc-6ui preview-serve INPUT``: one long-lived renderer per previewed file.

Requests are JSON lines on stdin (``{"id", "frame", "mode", "output", ...}``); replies are events:
``result{data:{kind:"info"}}`` once, then ``result{data:{kind:"frame", id, ...}}`` or
``error{id, ...}`` per request.  The server answers in order, so the widget keeps one request in
flight and only ever sends the newest wanted frame (stale positions are never requested).
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from ..events import Event
from .backend import EventProcess, ProcessOutcome


class PreviewServer(QObject):
    ready = Signal(dict)  # source info
    frameDone = Signal(object, dict)  # request id, PreviewResult dict
    frameFailed = Signal(object, dict)  # request id, error dict
    died = Signal(str)  # message; emitted when the process ends unexpectedly

    def __init__(self, path: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.is_ready = False
        self.info: dict[str, Any] = {}
        self._stopping = False
        self._err: dict[str, Any] | None = None
        self.proc = EventProcess(["preview-serve", path], self)
        self.proc.event.connect(self._on_event)
        self.proc.finished.connect(self._on_finished)
        self._kill_timer = QTimer(self, singleShot=True, interval=3000)
        self._kill_timer.timeout.connect(lambda: self.proc.is_running() and self.proc.proc.kill())

    def start(self) -> PreviewServer:
        self.proc.start()
        return self

    def request(self, rid: Any, req: dict[str, Any]) -> None:
        self.proc.proc.write((json.dumps({"id": rid, **req}, ensure_ascii=False) + "\n").encode("utf-8"))

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self.proc.is_running():
            self.proc.proc.write(b"quit\n")
            self.proc.proc.closeWriteChannel()
            self._kill_timer.start()

    def kill(self) -> None:
        self._stopping = True
        if self.proc.is_running():
            self.proc.proc.kill()
            self.proc.proc.waitForFinished(1000)

    def _on_event(self, ev: Event) -> None:
        d = ev.data
        if ev.ev == "result" and isinstance(d.get("data"), dict):
            data = d["data"]
            if data.get("kind") == "info":
                self.is_ready, self.info = True, data
                self.ready.emit(data)
            elif data.get("kind") == "frame":
                self.frameDone.emit(data.get("id"), data)
        elif ev.ev == "error":
            if d.get("id") is not None:
                self.frameFailed.emit(d.get("id"), dict(d))
            else:
                self._err = dict(d)

    def _on_finished(self, outcome: ProcessOutcome) -> None:
        self._kill_timer.stop()
        if self._stopping:
            return
        err = self._err or {}
        msg = err.get("message") or outcome.start_error or f"preview renderer exited (code {outcome.exit_code})"
        self.died.emit(str(msg))
