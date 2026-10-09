"""Run controls and progress (frames/total, fps, ETA, stage), error display and the log pane."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .log_pane import LogPane
from .queue_model import JobItem
from .run_model import RunState, format_duration

COMPONENT_EXIT = 4


class RunPanel(QWidget):
    startRequested = Signal()
    cancelRequested = Signal()
    doctorRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.start_button = QPushButton("Start queue")
        self.start_button.setObjectName("startButton")
        self.start_button.setDefault(True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setToolTip("Stop the running job; its partial output file is deleted")
        self.start_button.clicked.connect(self.startRequested)
        self.cancel_button.clicked.connect(self._cancel)

        self.progress = QProgressBar()
        self.progress.setObjectName("progressBar")
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("Idle")

        self.stage_label = QLabel("Idle")
        self.stage_label.setObjectName("stageLabel")
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("statsLabel")
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.error_frame = QFrame()
        self.error_frame.setObjectName("runError")
        self.error_frame.setStyleSheet("#runError { background: #fdecea; border: 1px solid #c62828; border-radius: 4px; }"
                                       "#runError QLabel { color: #1a1a1a; }")
        self.error_label = QLabel(wordWrap=True, textInteractionFlags=Qt.TextInteractionFlag.TextSelectableByMouse)
        self.doctor_button = QPushButton("Run doctor…")
        self.doctor_button.clicked.connect(self.doctorRequested)
        dismiss = QPushButton("Dismiss")
        dismiss.clicked.connect(self.error_frame.hide)
        el = QHBoxLayout(self.error_frame)
        el.setContentsMargins(10, 6, 10, 6)
        el.addWidget(self.error_label, 1)
        el.addWidget(self.doctor_button, 0, Qt.AlignmentFlag.AlignTop)
        el.addWidget(dismiss, 0, Qt.AlignmentFlag.AlignTop)
        self.error_frame.hide()

        self.log = LogPane()

        top = QHBoxLayout()
        top.addWidget(self.start_button)
        top.addWidget(self.cancel_button)
        top.addWidget(self.progress, 1)
        info = QHBoxLayout()
        info.addWidget(self.stage_label, 1)
        info.addWidget(self.stats_label)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addLayout(info)
        lay.addWidget(self.error_frame)
        lay.addWidget(self.log, 1)

    def _cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.stage_label.setText("Cancelling… (stopping the encoder and removing partial output)")
        self.cancelRequested.emit()

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        if running:
            self.error_frame.hide()

    def update_state(self, item: JobItem, state: RunState, index: int, total: int) -> None:
        frac = item.progress if item.progress is not None else state.overall_fraction
        if frac is None:
            self.progress.setRange(0, 0)  # busy indicator until the first frame count
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(frac * 1000))
            self.progress.setFormat(f"{frac * 100:.1f}%")
        job = f"Job {index} of {total}: {item.name}" if total else item.name
        self.stage_label.setText(f"{job} — {state.stage_text_or_cancelling() or 'starting…'}")
        if state.cancelling and not state.finished:
            self.cancel_button.setEnabled(False)
            self.progress.setFormat("Cancelling…")
        self.stage_label.setToolTip(item.path)
        parts = []
        if state.frames_text():
            parts.append(state.frames_text())
        if state.fps:
            parts.append(f"{state.fps:.1f} fps")
        if state.eta_s is not None:
            parts.append(f"ETA {format_duration(state.eta_s)}")
        if state.elapsed_s:
            parts.append(f"elapsed {format_duration(state.elapsed_s)}")
        self.stats_label.setText("  ·  ".join(parts))

    def show_finished(self, item: JobItem, state: RunState) -> None:
        self.progress.setRange(0, 1000)
        if state.cancelled:
            self.progress.setValue(0)
            self.progress.setFormat("Cancelled")
            removed = len(state.removed)
            self.stage_label.setText(
                f"Cancelled {item.name}" + (f" — the backend removed {removed} partial file(s)" if removed else "")
            )
        elif state.cancel_unconfirmed:
            self.progress.setValue(0)
            self.progress.setFormat("Stopped")
            self.stage_label.setText(f"Stopped {item.name}; cleanup was not confirmed")
            self.show_error(state.error or {}, item.name)
        elif state.exit_code == 0:
            self.progress.setValue(1000)
            self.progress.setFormat("Done")
            self.stage_label.setText(f"Finished {item.name}")
        else:
            self.progress.setFormat("Failed")
            self.show_error(state.error or {}, item.name)

    def show_error(self, err: dict, name: str = "") -> None:
        msg = err.get("message") or "The job failed."
        hint = err.get("hint") or ""
        code = err.get("code") or ""
        stderr = (err.get("details") or {}).get("stderr") if isinstance(err.get("details"), dict) else ""
        html = f"<b>{_esc(name + ': ' if name else '')}{_esc(msg)}</b>"
        if hint:
            html += f"<br>What to do: {_esc(hint)}"
        if stderr:
            html += f"<br><tt>{_esc(str(stderr)[-600:])}</tt>"
        if code:
            html += f"<br><small>error code: {_esc(code)} (full output in the log below)</small>"
        self.error_label.setText(html)
        self.doctor_button.setVisible(err.get("exit_code") == COMPONENT_EXIT or code == "backend_failed")
        self.error_frame.show()

    def show_batch_done(self, cancelled: bool, summary: str) -> None:
        self.set_running(False)
        if not cancelled:
            self.stage_label.setText(summary)
        self.stats_label.setText("")


def _esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
