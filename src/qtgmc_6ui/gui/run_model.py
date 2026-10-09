"""Event stream -> run state (pure Python, no Qt): what the run panel and queue display."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..events import Event

STAGE_LABELS = {
    "probe": "Probing",
    "index": "Indexing source",
    "idet": "Detecting interlacing",
    "fieldmatch": "Checking for telecine",
    "classify": "Classifying",
    "setup": "Preparing VapourSynth",
    "master": "Archival master (QTGMC + FFV1)",
    "view": "Viewing copy",
    "verify": "Verifying outputs",
}


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "–"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def error_text(err: dict[str, Any] | None) -> str:
    if not err:
        return ""
    msg = str(err.get("message") or err.get("code") or "error")
    hint = err.get("hint")
    return f"{msg}\n{hint}" if hint else msg


@dataclass
class RunState:
    command: str = ""
    input: str = ""
    planned_outputs: list[str] = field(default_factory=list)
    stage: str = ""
    stage_label: str = ""
    stage_index: int = 0
    stage_total: int = 0
    done: int = 0
    total: int | None = None
    fps: float = 0.0
    eta_s: float | None = None
    elapsed_s: float = 0.0
    warnings: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    cancelled: bool = False  # confirmed by the backend's ``cancelled`` event
    cancelling: bool = False  # cancel requested, not yet confirmed
    cancel_unconfirmed: bool = False  # the process ended without confirming cleanup
    keep_master: bool = True  # False: the master is temporary (viewing copies only)
    exit_code: int | None = None

    @property
    def finished(self) -> bool:
        return self.exit_code is not None

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return max(0.0, min(1.0, self.done / self.total))

    @property
    def overall_fraction(self) -> float | None:
        """Progress across stages: stage_index is 1-based, frames refine the current stage."""
        if not self.stage_total:
            return self.fraction
        base = max(self.stage_index - 1, 0) / self.stage_total
        return base + (self.fraction or 0.0) / self.stage_total

    def stage_text(self) -> str:
        label = self.stage_label or STAGE_LABELS.get(self.stage, self.stage)
        if self.stage == "view" and self.stage_label and self.stage_label != "view":
            label = f"Viewing copy: {self.stage_label}"
        elif self.stage in STAGE_LABELS and self.stage_label in ("", self.stage):
            label = STAGE_LABELS[self.stage]
        if self.stage == "master" and not self.keep_master:
            label = "Temporary master (QTGMC; deleted after the viewing copies)"
        if self.stage_total > 1:
            label += f"  ({self.stage_index}/{self.stage_total})"
        return label

    def frames_text(self) -> str:
        if self.total:
            return f"{self.done} / {self.total} frames"
        return f"{self.done} frames" if self.done else ""

    def apply(self, ev: Event) -> str | None:
        """Update from one event; return a log line for it, or None."""
        d = ev.data
        if ev.ev == "start":
            self.command = str(d.get("command", ""))
            self.input = str(d.get("input", ""))
            self.planned_outputs = [str(p) for p in d.get("outputs") or []]
            return f"started {self.command} {self.input}".rstrip()
        if ev.ev == "stage":
            self.stage = str(d.get("name", ""))
            self.stage_label = str(d.get("label") or "")
            self.stage_index = int(d.get("index") or 0)
            self.stage_total = int(d.get("total") or 0)
            self.done, self.total, self.fps, self.eta_s = 0, None, 0.0, None
            return f"== {self.stage_text()}"
        if ev.ev == "progress":
            if d.get("stage"):
                self.stage = str(d["stage"])
            self.done = int(d.get("done") or 0)
            tot = d.get("total")
            self.total = int(tot) if tot else None
            self.fps = float(d.get("fps") or 0.0)
            eta = d.get("eta_s")
            self.eta_s = float(eta) if isinstance(eta, (int, float)) else None
            self.elapsed_s = float(d.get("elapsed_s") or 0.0)
            return None
        if ev.ev == "log":
            level = d.get("level", "info")
            prefix = f"[{d.get('source', 'app')}]"
            if level not in ("info", "debug"):
                prefix += f" {level}:"
            return f"{prefix} {d.get('line', '')}"
        if ev.ev == "warning":
            self.warnings.append(dict(d))
            hint = f" ({d['hint']})" if d.get("hint") else ""
            return f"WARNING [{d.get('code', '')}] {d.get('message', '')}{hint}"
        if ev.ev == "result":
            self.result = d.get("data") if isinstance(d.get("data"), dict) else None
            return None
        if ev.ev == "error":
            self.error = dict(d)
            hint = f"\n  hint: {d['hint']}" if d.get("hint") else ""
            return f"ERROR [{d.get('code', '')}] {d.get('message', '')}{hint}"
        if ev.ev == "cancelled":
            self.cancelled = True
            self.removed = [str(p) for p in d.get("removed") or []]
            self.kept = [str(p) for p in d.get("kept") or []]
            lines = ["cancelled"]
            lines += [f"  removed partial output: {p}" for p in self.removed]
            lines += [f"  kept finished output: {p}" for p in self.kept]
            return "\n".join(lines)
        if ev.ev == "done":
            self.exit_code = int(d.get("exit_code", 0))
            if self.exit_code == 130 and not self.cancelled:
                self.cancel_unconfirmed = True
            return f"finished with exit code {self.exit_code}"
        return None

    def stage_text_or_cancelling(self) -> str:
        if self.cancelling and not self.finished:
            return "Cancelling… waiting for the backend to stop and remove partial output"
        return self.stage_text()

    def finish_without_done(self, exit_code: int, start_error: str = "", stderr_tail: list[str] | None = None) -> None:
        """The process ended without a ``done`` event (crash, missing backend, killed)."""
        if self.exit_code is not None:
            return
        self.exit_code = exit_code
        if exit_code == 130 or (self.cancelling and not self.cancelled):
            self.cancel_unconfirmed = not self.cancelled
        elif self.error is None and exit_code != 0:
            tail = "\n".join((stderr_tail or [])[-8:])
            self.error = {
                "code": "backend_failed",
                "message": start_error or f"The backend stopped unexpectedly (exit code {exit_code}).",
                "hint": "Run Doctor (Help > Run doctor) and check the log for the backend output.",
                "details": {"stderr": tail},
                "exit_code": exit_code,
            }
