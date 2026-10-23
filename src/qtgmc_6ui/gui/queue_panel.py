"""Job queue view: add files/folders, reorder, remove, per-job status."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QStyle,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import ui_state
from .queue_model import VIDEO_EXTENSIONS, JobItem, JobStatus, QueueModel, expand_paths

VIDEO_FILTER = "Video files (" + " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS)) + ");;All files (*)"


class QueuePanel(QWidget):
    addRequested = Signal(list)  # list[str] of files
    removeRequested = Signal(list)  # list[JobItem]
    reanalyzeRequested = Signal(object)  # JobItem
    currentChanged = Signal(object)  # JobItem | None

    def __init__(self, model: QueueModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self.table = QTableView()
        self.table.setObjectName("queueTable")
        self.table.setModel(model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QueueModel.COL_FILE, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(QueueModel.COL_CONTENT, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(QueueModel.COL_STATUS, QHeaderView.ResizeMode.Interactive)
        fm = self.fontMetrics()
        hdr.resizeSection(QueueModel.COL_CONTENT, fm.horizontalAdvance("Interlaced BFF  ") + 8)
        hdr.resizeSection(QueueModel.COL_STATUS, fm.horizontalAdvance("Needs your decision  ") + 8)
        self.table.selectionModel().currentRowChanged.connect(lambda cur, _prev: self.currentChanged.emit(model.item_at(cur.row())))

        st = self.style()

        def tool(text: str, icon: QStyle.StandardPixmap, tip: str) -> QToolButton:
            b = QToolButton(text=text, toolTip=tip)
            b.setIcon(st.standardIcon(icon))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            return b

        self.add_files_button = tool("Add files…", QStyle.StandardPixmap.SP_FileIcon, "Add video files to the queue")
        self.add_folder_button = tool("Add folder…", QStyle.StandardPixmap.SP_DirIcon, "Add every video file in a folder (recursive)")
        self.remove_button = tool("Remove", QStyle.StandardPixmap.SP_DialogDiscardButton, "Remove the selected jobs (Del)")
        self.up_button = tool("", QStyle.StandardPixmap.SP_ArrowUp, "Move up (Ctrl+Up)")
        self.down_button = tool("", QStyle.StandardPixmap.SP_ArrowDown, "Move down (Ctrl+Down)")
        self.reanalyze_button = tool("", QStyle.StandardPixmap.SP_BrowserReload, "Analyze the selected file again")
        self.add_files_button.clicked.connect(self.choose_files)
        self.add_folder_button.clicked.connect(self.choose_folder)
        self.remove_button.clicked.connect(self.remove_selected)
        self.up_button.clicked.connect(lambda: self.move_current(-1))
        self.down_button.clicked.connect(lambda: self.move_current(+1))
        self.reanalyze_button.clicked.connect(self._reanalyze)
        QShortcut(QKeySequence.StandardKey.Delete, self.table, self.remove_selected)
        QShortcut(QKeySequence("Ctrl+Up"), self.table, lambda: self.move_current(-1))
        QShortcut(QKeySequence("Ctrl+Down"), self.table, lambda: self.move_current(+1))

        self.empty_hint = QLabel("Add files or folders, or drop them here.")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setStyleSheet("color: palette(placeholder-text); padding: 12px;")

        bar = QHBoxLayout()
        bar.setSpacing(4)
        bar.addWidget(self.add_files_button)
        bar.addWidget(self.add_folder_button)
        bar.addStretch(1)
        for b in (self.reanalyze_button, self.up_button, self.down_button, self.remove_button):
            bar.addWidget(b)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        title = QLabel("<b>Queue</b>")
        lay.addWidget(title)
        lay.addLayout(bar)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.empty_hint)
        model.rowsInserted.connect(self._update_buttons)
        model.rowsRemoved.connect(self._update_buttons)
        model.dataChanged.connect(self._update_buttons)
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)
        self._update_buttons()

    # ------------------------------------------------------------------ selection
    def selected_items(self) -> list[JobItem]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [it for r in rows if (it := self.model.item_at(r))]

    def current_item(self) -> JobItem | None:
        return self.model.item_at(self.table.currentIndex().row())

    def select(self, item: JobItem) -> None:
        row = self.model.row_of(item)
        if row >= 0:
            self.table.selectionModel().setCurrentIndex(
                self.model.index(row, 0),
                QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
            )

    def _update_buttons(self, *args) -> None:  # noqa: ARG002
        sel = self.selected_items()
        busy = {JobStatus.RUNNING, JobStatus.QUEUED}
        self.remove_button.setEnabled(bool(sel) and not any(i.status in busy for i in sel))
        cur = self.current_item()
        row = self.model.row_of(cur) if cur else -1
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < self.model.rowCount() - 1)
        self.reanalyze_button.setEnabled(cur is not None and cur.status not in busy | {JobStatus.ANALYZING, JobStatus.WAITING})
        self.empty_hint.setVisible(self.model.rowCount() == 0)

    # ------------------------------------------------------------------ actions
    def choose_files(self) -> None:
        start = ui_state.get_str("last_input_dir")
        files, _ = QFileDialog.getOpenFileNames(self, "Add video files", start, VIDEO_FILTER)
        if files:
            ui_state.put("last_input_dir", str(Path(files[0]).parent))
            self.addRequested.emit(files)

    def choose_folder(self) -> None:
        start = ui_state.get_str("last_input_dir")
        folder = QFileDialog.getExistingDirectory(self, "Add a folder of videos", start)
        if folder:
            ui_state.put("last_input_dir", folder)
            files = expand_paths([folder])
            if files:
                self.addRequested.emit(files)

    def remove_selected(self) -> None:
        items = self.selected_items()
        if items:
            self.removeRequested.emit(items)

    def move_current(self, delta: int) -> None:
        item = self.current_item()
        if not item:
            return
        self.model.move(self.model.row_of(item), delta)
        self.select(item)
        self._update_buttons()

    def _reanalyze(self) -> None:
        if item := self.current_item():
            self.reanalyzeRequested.emit(item)
