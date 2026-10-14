"""Startup component check: a banner in the main window and a dialog rendering the DoctorReport."""

from __future__ import annotations

import html
import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import DoctorReport, Status
from .backend import CliCall, CliResult
from .run_model import error_text

STATUS_MARK = {Status.OK: "OK", Status.WARN: "Warning", Status.FAIL: "FAIL", Status.SKIP: "skipped"}
STATUS_COLOR = {Status.OK: "#2e7d32", Status.WARN: "#b26a00", Status.FAIL: "#c62828", Status.SKIP: "#757575"}

BANNER_STYLE = {
    "error": ("#fdecea", "#c62828"),
    "warning": ("#fff4e5", "#b26a00"),
    "info": ("#e8f1fb", "#1565c0"),
}


def parse_report(result: CliResult) -> DoctorReport | None:
    if isinstance(result.data, dict) and "checks" in result.data:
        try:
            return DoctorReport.from_dict(result.data)
        except (TypeError, ValueError):
            return None
    return None


def problems(report: DoctorReport) -> list[str]:
    out = []
    for c in report.checks:
        if c.status == Status.FAIL:
            out.append(f"{c.title}: {c.detail}" + (f" — {c.hint}" if c.hint else ""))
    return out


class Banner(QFrame):
    """Dismissable message strip; ``action`` is an optional button (e.g. "Details…")."""

    actionClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("banner")
        self.label = QLabel(wordWrap=True, textInteractionFlags=Qt.TextInteractionFlag.TextSelectableByMouse)
        self.action = QPushButton("Details…")
        self.action.clicked.connect(self.actionClicked)
        self.close_button = QPushButton("Dismiss")
        self.close_button.clicked.connect(self.hide)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.action, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.hide()

    def show_message(self, level: str, html: str, action_text: str | None = "Details…") -> None:
        bg, fg = BANNER_STYLE.get(level, BANNER_STYLE["info"])
        self.setStyleSheet(
            f"#banner {{ background: {bg}; border: 1px solid {fg}; border-radius: 4px; }}"
            f"#banner QLabel {{ color: #1a1a1a; }}"
        )
        self.label.setText(html)
        self.action.setVisible(bool(action_text))
        if action_text:
            self.action.setText(action_text)
        self.show()


class DoctorDialog(QDialog):
    """Renders a DoctorReport (or the raw failure when doctor itself could not run)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Doctor: component check")
        self.resize(820, 520)
        self.summary = QLabel(wordWrap=True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Check", "Status", "Detail / what to do"])
        self.tree.setRootIsDecorated(False)
        self.tree.setWordWrap(True)
        self.tree.setAlternatingRowColors(True)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._raw = ""

        self.full_button = QPushButton("Run full check (loads plugins)")
        self.copy_button = QPushButton("Copy report")
        self.copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._raw))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.full_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.full_button.clicked.connect(self.run_full)

        lay = QVBoxLayout(self)
        lay.addWidget(self.summary)
        lay.addWidget(self.tree, 1)
        lay.addWidget(buttons)
        self._call: CliCall | None = None

    def set_result(self, result: CliResult) -> None:
        report = parse_report(result)
        self.tree.clear()
        if report is None:
            self._raw = (result.stdout + "\n" + result.stderr).strip()
            self.summary.setText(
                "<b>Doctor could not run.</b> The backend failed before producing a report:<br>"
                + error_text(result.error).replace("\n", "<br>")
            )
            QTreeWidgetItem(self.tree, ["backend output", "FAIL", self._raw[-4000:] or "(no output)"])
            return
        self._raw = json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
        colour = "#2e7d32" if report.ok else "#c62828"
        self.summary.setText(f"<b style='color:{colour}'>{'All good' if report.ok else 'Problems found'}</b>: {report.summary}")
        for c in report.checks:
            detail = c.detail + (f"\n→ {c.hint}" if c.hint else "")
            it = QTreeWidgetItem(self.tree, [c.title, STATUS_MARK.get(c.status, str(c.status)), detail])
            it.setForeground(1, QBrush(QColor(STATUS_COLOR.get(c.status, "#000"))))
            it.setToolTip(2, detail)

    def run_full(self) -> None:
        self.full_button.setEnabled(False)
        self.summary.setText("Running full doctor check…")
        self._call = CliCall(["doctor", "--json"], self)
        self._call.finished.connect(self._on_full)
        self._call.start()

    def _on_full(self, result: CliResult) -> None:
        self.full_button.setEnabled(True)
        self.set_result(result)


def banner_for(result: CliResult) -> tuple[str, str] | None:
    """(level, html) for the startup banner, or None when everything is fine."""
    report = parse_report(result)
    if report is None:
        msg = error_text(result.error) or "unknown failure"
        return (
            "error",
            "<b>qtgmc-6UI cannot check its components.</b> The backend failed to start: "
            + msg.replace("\n", " — ")
            + "<br>Processing will probably fail. Click <i>Details…</i> for the output.",
        )
    if report.ok:
        warns = [c for c in report.checks if c.status == Status.WARN]
        if warns:
            return "info", "Component check passed with notes: " + "; ".join(f"{c.title}: {c.detail}" for c in warns)
        return None
    lines = problems(report) or [report.summary]
    return (
        "error",
        "<b>Component problem — processing will fail until this is fixed:</b><br>"
        + "<br>".join(_escape(x) for x in lines),
    )


def _escape(s: Any) -> str:
    return html.escape(str(s), quote=False)
