"""Queue controller: analyses added files one at a time and runs the batch, both via the CLI."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .. import paths
from ..events import Event
from ..models import AnalysisResult, JobSpec
from .backend import EventProcess, ProcessOutcome
from .queue_model import JobItem, JobStatus, QueueModel
from .run_model import STAGE_LABELS, RunState


def jobs_dir() -> Path:
    d = paths.cache_dir() / "gui-jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


class QueueController(QObject):
    log = Signal(str)  # one or more lines for the log pane
    analysisDone = Signal(object)  # JobItem
    runUpdated = Signal(object, object)  # JobItem, RunState
    jobFinished = Signal(object, object)  # JobItem, RunState
    batchStarted = Signal(int)  # number of jobs
    batchFinished = Signal(bool)  # True when cancelled
    idle = Signal()  # nothing runs any more (after stop_all)

    def __init__(self, model: QueueModel, spec_builder: Callable[[JobItem], JobSpec], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.spec_builder = spec_builder
        self._analysis: tuple[JobItem, EventProcess, RunState] | None = None
        self._stopping = False
        self._run: tuple[JobItem, EventProcess, RunState] | None = None
        self._batch_cancelled = False
        self.batch_total = 0
        self.batch_done = 0

    # ------------------------------------------------------------------ analysis
    def add(self, paths_: list[str]) -> list[JobItem]:
        new = self.model.add_paths(paths_)
        self._next_analysis()
        return new

    def reanalyze(self, item: JobItem) -> None:
        if item.status in (JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.ANALYZING):
            return
        item.status, item.analysis, item.error = JobStatus.WAITING, None, None
        self.model.notify(item)
        self._next_analysis()

    def remove(self, items: list[JobItem]) -> None:
        rows = []
        for item in items:
            if item.status in (JobStatus.RUNNING, JobStatus.QUEUED):
                continue
            if self._analysis and self._analysis[0] is item:
                self._analysis[1].cancel()  # stdin cancel: the CLI stops its ffmpeg children
            rows.append(self.model.row_of(item))
        self.model.remove_rows([r for r in rows if r >= 0])

    @property
    def analyzing(self) -> bool:
        return self._analysis is not None

    def _next_analysis(self) -> None:
        if self._analysis or self._stopping:
            if not self._analysis:
                self._check_idle()
            return
        item = self.model.next_waiting()
        if not item:
            return
        item.status, item.analysis_progress = JobStatus.ANALYZING, ""
        self.model.notify(item)
        proc = EventProcess(["analyze", item.path, "--events"], self)
        state = RunState()
        self._analysis = (item, proc, state)
        proc.event.connect(lambda ev: self._on_analysis_event(item, state, ev))
        proc.text.connect(lambda stream, line: self.log.emit(f"[analyze {stream}] {line}"))
        proc.finished.connect(lambda outcome: self._on_analysis_finished(item, state, outcome))
        self.log.emit(f"Analyzing {item.path}")
        proc.start()

    def _on_analysis_event(self, item: JobItem, state: RunState, ev: Event) -> None:
        line = state.apply(ev)
        if line and ev.ev in ("log", "warning", "error"):
            self.log.emit(line)
        if ev.ev in ("stage", "progress"):
            pct = f" {state.fraction * 100:.0f}%" if state.fraction is not None else ""
            item.analysis_progress = STAGE_LABELS.get(state.stage, state.stage).lower() + pct
            self.model.notify(item)

    def _on_analysis_finished(self, item: JobItem, state: RunState, outcome: ProcessOutcome) -> None:
        state.finish_without_done(outcome.exit_code, outcome.start_error, outcome.stderr_tail)
        proc = self._analysis[1] if self._analysis else None
        self._analysis = None
        if proc:
            proc.deleteLater()
        if self.model.row_of(item) < 0:  # removed while analyzing
            self._next_analysis()
            return
        analysis = None
        if state.result and state.exit_code == 0:
            try:
                analysis = AnalysisResult.from_dict(state.result)
            except (TypeError, ValueError, KeyError) as e:
                state.error = {"code": "bad_result", "message": f"Unreadable analysis result: {e}",
                               "hint": "The backend and GUI versions may not match; run Doctor."}
        if analysis:
            item.analysis, item.error = analysis, None
            item.status = JobStatus.READY
            item.refresh_status()
            if self._run is not None and item.status == JobStatus.READY and not self._batch_cancelled:
                item.status = JobStatus.QUEUED  # joins the running batch
                self.batch_total += 1
            self.log.emit(f"Analysis of {item.name}: {item.content_text()} — {analysis.recommendation.message}")
        else:
            item.status = JobStatus.ANALYSIS_FAILED
            item.error = state.error or {"code": "analysis_failed", "message": "Analysis produced no result."}
            self.log.emit(f"Analysis of {item.name} failed: {item.error.get('message')}")
        self.model.notify(item)
        self.analysisDone.emit(item)
        self._next_analysis()

    # ------------------------------------------------------------------ batch
    @property
    def running(self) -> bool:
        return self._run is not None

    @property
    def busy(self) -> bool:
        return self._run is not None or self._analysis is not None

    def queued(self) -> list[JobItem]:
        return [i for i in self.model.items if i.status == JobStatus.QUEUED]

    def start(self, items: list[JobItem] | None = None) -> int:
        if self.running:
            return 0
        batch = [i for i in (items if items is not None else self.model.runnable()) if i.status == JobStatus.READY]
        for i in batch:
            i.status, i.progress, i.error = JobStatus.QUEUED, None, None
            self.model.notify(i)
        self._batch_cancelled = False
        self.batch_total, self.batch_done = len(batch), 0
        if batch:
            self.batchStarted.emit(len(batch))
            self._next_job()
        return len(batch)

    def cancel(self) -> None:
        """Stop the batch: queued jobs go back to Ready, the running one is cancelled by the CLI."""
        self._batch_cancelled = True
        for i in self.queued():
            i.status = JobStatus.READY
            i.refresh_status()
            self.model.notify(i)
        if self._run:
            item, proc, state = self._run
            if not state.cancelling:
                state.cancelling = item.cancelling = True
                self.model.notify(item)
                self.log.emit("Cancel requested: waiting for the backend to stop and remove partial output…")
                proc.cancel()
                self.runUpdated.emit(item, state)

    @property
    def batch_index(self) -> int:
        return self.batch_done

    def _next_job(self) -> None:
        nxt = self.queued()  # model order, so reordering during a batch is honoured
        if self._batch_cancelled or self._stopping or not nxt:
            self.batchFinished.emit(self._batch_cancelled)
            self._check_idle()
            return
        item = nxt[0]
        self.batch_done += 1
        try:
            spec = self.spec_builder(item)
        except ValueError as e:
            item.status, item.error = JobStatus.FAILED, {"code": "usage", "message": str(e)}
            self.model.notify(item)
            self._next_job()
            return
        job_file = jobs_dir() / f"job-{item.id}.json"
        job_file.write_text(json.dumps(spec.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        item.status, item.progress, item.outputs, item.error = JobStatus.RUNNING, 0.0, [], None
        self.model.notify(item)
        state = RunState(keep_master=spec.keep_master)
        item.keep_master = spec.keep_master
        proc = EventProcess(["run-job", str(job_file), "--events"], self)
        self._run = (item, proc, state)
        proc.event.connect(lambda ev: self._on_run_event(item, state, ev))
        proc.text.connect(lambda stream, line: self.log.emit(line if stream == "stderr" else f"[stdout] {line}"))
        proc.escalated.connect(lambda how: self._on_escalated(item, state, how))
        proc.finished.connect(lambda outcome: self._on_run_finished(item, state, outcome, job_file))
        self.log.emit(f"\n=== Job {self.batch_index}/{self.batch_total}: {item.path}")
        proc.start()
        self.runUpdated.emit(item, state)

    def _on_escalated(self, item: JobItem, state: RunState, how: str) -> None:
        if how == "terminate":
            self.log.emit("The backend did not stop within 20 s of the cancel request; sending SIGTERM.")
        else:
            self.log.emit("The backend still did not stop; killing it. Partial output may remain on disk.")
        self.runUpdated.emit(item, state)

    def _on_run_event(self, item: JobItem, state: RunState, ev: Event) -> None:
        line = state.apply(ev)
        if line:
            self.log.emit(line)
        if ev.ev in ("stage", "progress") and state.overall_fraction is not None:
            item.progress = max(item.progress or 0.0, state.overall_fraction)  # never jumps backwards
            self.model.notify(item)
        self.runUpdated.emit(item, state)

    def _on_run_finished(self, item: JobItem, state: RunState, outcome: ProcessOutcome, job_file: Path) -> None:
        state.finish_without_done(outcome.exit_code, outcome.start_error, outcome.stderr_tail)
        if self._run:
            self._run[1].deleteLater()
        self._run = None
        job_file.unlink(missing_ok=True)
        if state.cancelled or state.cancel_unconfirmed:
            item.status = JobStatus.CANCELLED
            if state.cancel_unconfirmed:
                left = [p for p in state.planned_outputs if Path(p).exists()]
                item.error = {
                    "code": "cancel_unconfirmed",
                    "message": "The backend stopped without confirming that partial output was removed.",
                    "hint": ("These files may be incomplete; delete them: " + ", ".join(left)) if left
                    else "No planned output file exists, so nothing was left behind.",
                    "details": {"leftover": left},
                }
                state.error = item.error
        elif state.exit_code == 0:
            item.status = JobStatus.DONE
            item.outputs = list((state.result or {}).get("outputs") or [])
        else:
            item.status = JobStatus.FAILED
            item.error = state.error
        item.progress, item.cancelling = None, False
        self.model.notify(item)
        self.runUpdated.emit(item, state)
        self.jobFinished.emit(item, state)
        if state.cancelled or state.cancel_unconfirmed:
            self.cancel()
        self._next_job()

    # ------------------------------------------------------------------ shutdown
    def stop_all(self) -> None:
        """Window closing: cancel everything through the CLI; ``idle`` fires once all processes ended."""
        self._stopping = True
        if self._analysis:
            self._analysis[1].cancel()
        if self._run:
            self.cancel()
        self._check_idle()

    def _check_idle(self) -> None:
        if self._stopping and not self.busy:
            self.idle.emit()

    def kill_all(self) -> None:
        """Last resort at interpreter exit (tests, crashes): no waiting for cleanup."""
        for entry in (self._run, self._analysis):
            if entry:
                entry[1].proc.kill()
                entry[1].proc.waitForFinished(2000)
