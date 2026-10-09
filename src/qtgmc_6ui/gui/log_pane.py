"""Full backend log (events, ffmpeg/vspipe/encoder output) with copy and save."""

from __future__ import annotations

from PySide6.QtGui import QFontDatabase, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

MAX_LINES = 200_000


class LogPane(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = QPlainTextEdit(readOnly=True)
        self.view.setObjectName("logView")
        self.view.setMaximumBlockCount(MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setPlaceholderText("Backend output appears here.")

        self.copy_button = QPushButton("Copy log")
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        self.save_button = QPushButton("Save…")
        self.save_button.clicked.connect(self.save_as)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.view.clear)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("<b>Log</b>"))
        bar.addStretch(1)
        for b in (self.copy_button, self.save_button, self.clear_button):
            bar.addWidget(b)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)

    def append(self, text: str) -> None:
        bar = self.view.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 2
        self.view.appendPlainText(text)
        if at_end:
            self.view.moveCursor(QTextCursor.MoveOperation.End)
            bar.setValue(bar.maximum())

    def text(self) -> str:
        return self.view.toPlainText()

    def copy_to_clipboard(self) -> None:
        QGuiApplication.clipboard().setText(self.text())

    def save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "qtgmc-6ui.log", "Log files (*.log *.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.text())
        except OSError as e:
            QMessageBox.warning(self, "Save log", f"Could not save the log:\n{e}")
