"""Job queue: JobItem (one input file) and the QAbstractTableModel shown in the queue view."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont

from ..models import AnalysisResult, ContentClass, RecommendedAction

VIDEO_EXTENSIONS = frozenset(
    ".avi .dv .mkv .mov .mp4 .m4v .mpg .mpeg .m2v .vob .ts .m2ts .mts .m2t .mxf .wmv .flv .webm .nut .y4m .3gp .ogv".split()
)


class JobStatus(StrEnum):
    WAITING = "waiting"  # waiting for analysis
    ANALYZING = "analyzing"
    READY = "ready"
    NEEDS_DECISION = "needs_decision"  # blocked by classification or field order unknown
    QUEUED = "queued"  # part of the running batch, not started yet
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ANALYSIS_FAILED = "analysis_failed"


STATUS_TEXT = {
    JobStatus.WAITING: "Waiting for analysis",
    JobStatus.ANALYZING: "Analyzing…",
    JobStatus.READY: "Ready",
    JobStatus.NEEDS_DECISION: "Needs your decision",
    JobStatus.QUEUED: "Queued",
    JobStatus.RUNNING: "Running",
    JobStatus.DONE: "Done",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
    JobStatus.ANALYSIS_FAILED: "Analysis failed",
}

STATUS_COLOR = {
    JobStatus.READY: "#2e7d32",
    JobStatus.DONE: "#2e7d32",
    JobStatus.NEEDS_DECISION: "#b26a00",
    JobStatus.FAILED: "#c62828",
    JobStatus.ANALYSIS_FAILED: "#c62828",
    JobStatus.CANCELLED: "#757575",
    JobStatus.RUNNING: "#1565c0",
    JobStatus.ANALYZING: "#1565c0",
}

CONTENT_TEXT = {
    ContentClass.INTERLACED_TFF: "Interlaced TFF",
    ContentClass.INTERLACED_BFF: "Interlaced BFF",
    ContentClass.TELECINED: "Telecined",
    ContentClass.MIXED: "Mixed (partly telecined)",
    ContentClass.PROGRESSIVE: "Progressive",
    ContentClass.UNDETERMINED: "Undetermined",
}

_ids = itertools.count(1)


@dataclass
class JobItem:
    path: str
    id: int = field(default_factory=lambda: next(_ids))
    status: JobStatus = JobStatus.WAITING
    analysis: AnalysisResult | None = None
    analysis_progress: str = ""
    field_order_override: str | None = None  # "tff" | "bff"; None = detected
    force: bool = False  # user accepted processing despite telecine/progressive detection
    error: dict[str, Any] | None = None
    progress: float | None = None  # 0..1 while running
    outputs: list[dict[str, Any]] = field(default_factory=list)
    cancelling: bool = False
    keep_master: bool = True  # of the last run

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def action(self) -> RecommendedAction | None:
        return self.analysis.recommendation.action if self.analysis else None

    def detected_order(self) -> str | None:
        if not self.analysis or not self.analysis.classification.field_order:
            return None
        return str(self.analysis.classification.field_order)

    def effective_field_order(self) -> str | None:
        return self.field_order_override or self.detected_order()

    def content_text(self) -> str:
        if not self.analysis:
            return ""
        if self.field_order_override:
            return f"{self.field_order_override.upper()} (override)"
        return CONTENT_TEXT.get(self.analysis.classification.content, str(self.analysis.classification.content))

    def decision_needed(self) -> str | None:
        """Why this job cannot run as configured, or None when it can."""
        if not self.analysis:
            return None
        act = self.action
        if act in (RecommendedAction.BLOCK_TELECINED, RecommendedAction.BLOCK_MIXED) and not self.force:
            return "Telecined content: needs inverse telecine (IVTC) with another tool."
        if act == RecommendedAction.SKIP_PROGRESSIVE and not self.force:
            return "Progressive content: deinterlacing is not recommended."
        if act == RecommendedAction.CONFIRM_FIELD_ORDER and not self.field_order_override:
            return "Field order could not be determined: choose TFF or BFF."
        if self.force and not self.effective_field_order():
            return "Choose a field order to process this file anyway."
        return None

    def refresh_status(self) -> None:
        """Recompute READY / NEEDS_DECISION after analysis or a user decision."""
        if self.status in (JobStatus.READY, JobStatus.NEEDS_DECISION, JobStatus.CANCELLED, JobStatus.FAILED) and self.analysis:
            self.status = JobStatus.NEEDS_DECISION if self.decision_needed() else JobStatus.READY

    def status_text(self) -> str:
        text = STATUS_TEXT[self.status]
        if self.status == JobStatus.ANALYZING and self.analysis_progress:
            text = f"Analyzing: {self.analysis_progress}"
        elif self.status == JobStatus.DONE and not self.keep_master:
            text = "Done (viewing copies only)"
        elif self.status == JobStatus.RUNNING and self.cancelling:
            text = "Cancelling…"
        elif self.status == JobStatus.RUNNING and self.progress is not None:
            text = f"Running {self.progress * 100:.0f}%"
        return text


class QueueModel(QAbstractTableModel):
    COL_FILE, COL_CONTENT, COL_STATUS = range(3)
    HEADERS = ("File", "Detected", "Status")
    ItemRole = Qt.ItemDataRole.UserRole + 1

    itemChanged = Signal(object)  # JobItem

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: list[JobItem] = []

    # --- Qt model API
    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        item = self.items[index.row()]
        col = index.column()
        if role == self.ItemRole:
            return item
        if role == Qt.ItemDataRole.DisplayRole:
            if col == self.COL_FILE:
                return item.name
            if col == self.COL_CONTENT:
                return item.content_text()
            return item.status_text()
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == self.COL_FILE:
                return item.path
            if col == self.COL_STATUS:
                return item.decision_needed() or (item.error or {}).get("message") or item.status_text()
            if item.field_order_override and item.analysis:
                detected = CONTENT_TEXT.get(item.analysis.classification.content, "")
                return f"Detected: {detected}; field order overridden to {item.field_order_override.upper()}"
            return item.content_text()
        if role == Qt.ItemDataRole.ForegroundRole and col == self.COL_CONTENT and item.field_order_override:
            return QBrush(QColor("#b26a00"))
        if role == Qt.ItemDataRole.ForegroundRole and col == self.COL_STATUS and item.status in STATUS_COLOR:
            return QBrush(QColor(STATUS_COLOR[item.status]))
        if role == Qt.ItemDataRole.FontRole and col == self.COL_CONTENT and item.field_order_override:
            f = QFont()
            f.setItalic(True)
            return f
        return None

    # --- queue operations
    def add_paths(self, paths: list[str]) -> list[JobItem]:
        known = {i.path for i in self.items}
        new = [JobItem(p) for p in dict.fromkeys(paths) if p not in known]
        if new:
            start = len(self.items)
            self.beginInsertRows(QModelIndex(), start, start + len(new) - 1)
            self.items.extend(new)
            self.endInsertRows()
        return new

    def remove_rows(self, rows: list[int]) -> list[JobItem]:
        removed = []
        for row in sorted(set(rows), reverse=True):
            if 0 <= row < len(self.items):
                self.beginRemoveRows(QModelIndex(), row, row)
                removed.append(self.items.pop(row))
                self.endRemoveRows()
        return removed

    def move(self, row: int, delta: int) -> int:
        """Move one row by ``delta`` (-1 up / +1 down); returns the new row."""
        dest = row + delta
        if not (0 <= row < len(self.items)) or not (0 <= dest < len(self.items)) or delta == 0:
            return row
        # beginMoveRows wants the destination index "before which" the row is inserted
        qt_dest = dest + 1 if delta > 0 else dest
        self.beginMoveRows(QModelIndex(), row, row, QModelIndex(), qt_dest)
        self.items.insert(dest, self.items.pop(row))
        self.endMoveRows()
        return dest

    def row_of(self, item: JobItem) -> int:
        for i, it in enumerate(self.items):
            if it is item:
                return i
        return -1

    def item_at(self, row: int) -> JobItem | None:
        return self.items[row] if 0 <= row < len(self.items) else None

    def notify(self, item: JobItem) -> None:
        row = self.row_of(item)
        if row >= 0:
            self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))
            self.itemChanged.emit(item)

    def next_waiting(self) -> JobItem | None:
        return next((i for i in self.items if i.status == JobStatus.WAITING), None)

    def runnable(self) -> list[JobItem]:
        return [i for i in self.items if i.status == JobStatus.READY]


def expand_paths(paths: list[str], recursive: bool = True) -> list[str]:
    """Files are kept as given; folders contribute their video files (sorted)."""
    out: list[str] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            it = path.rglob("*") if recursive else path.glob("*")
            out.extend(str(f) for f in sorted(it) if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS)
        elif path.is_file():
            out.append(str(path))
    return out
