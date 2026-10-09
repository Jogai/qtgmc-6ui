"""Per-file analysis: ffprobe summary, idet counts, detected type, field-order override, IVTC notice."""

from __future__ import annotations

from fractions import Fraction

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..models import AnalysisResult, ContentClass, RecommendedAction
from .queue_model import CONTENT_TEXT, JobItem, JobStatus
from .run_model import format_duration

IVTC_TEXT = (
    "This material is <b>telecined</b> (progressive film with 3:2 pulldown), not truly interlaced. "
    "It needs <b>inverse telecine (IVTC)</b>, which qtgmc-6UI does not do: use a dedicated tool "
    "(for example VapourSynth VIVTC / fieldmatch + decimate, or FFmpeg's <tt>fieldmatch,decimate</tt>). "
    "Deinterlacing it with QTGMC gives inferior results, so this file is not processed by default."
)
MIXED_TEXT = (
    "This file is <b>mixed</b>: parts are telecined film and parts are truly interlaced. The telecined "
    "parts need <b>inverse telecine (IVTC)</b> with another tool; QTGMC would treat them as video and "
    "lose quality. This file is not processed by default."
)
PROGRESSIVE_TEXT = (
    "No interlacing was found: this looks <b>progressive</b>. Deinterlacing a progressive source only "
    "costs time and can soften the image, so it is skipped by default."
)
UNDETERMINED_TEXT = (
    "The field order could not be determined reliably (static or very clean content, or conflicting "
    "results). Check it in the Preview tab with <i>Show fields</i>, then choose TFF or BFF below."
)

CALLOUT = {
    "ok": ("#e8f5e9", "#2e7d32"),
    "warn": ("#fff4e5", "#b26a00"),
    "block": ("#fdecea", "#c62828"),
}


def fmt_fraction(value: Fraction | None, sep: str = "/") -> str:
    if value is None:
        return "–"
    if value.denominator == 1 and sep == "/":
        return str(value.numerator)
    return f"{value.numerator}{sep}{value.denominator}"


def fmt_fps(value: Fraction | None) -> str:
    if value is None:
        return "–"
    return f"{float(value):.3f}".rstrip("0").rstrip(".") + f" ({fmt_fraction(value)})"


class Callout(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("callout")
        self.title = QLabel()
        self.title.setObjectName("calloutTitle")
        self.body = QLabel(wordWrap=True, textFormat=Qt.TextFormat.RichText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(self.title)
        lay.addWidget(self.body)

    def set(self, kind: str, title: str, body: str) -> None:
        bg, fg = CALLOUT[kind]
        self.setStyleSheet(
            f"#callout {{ background: {bg}; border-left: 4px solid {fg}; border-radius: 3px; }}"
            f"#callout QLabel {{ color: #1a1a1a; }} #calloutTitle {{ color: {fg}; font-weight: bold; }}"
        )
        self.title.setText(title)
        self.body.setText(body)
        self.body.setVisible(bool(body))


class AnalysisPanel(QScrollArea):
    decisionChanged = Signal(object)  # JobItem
    reanalyzeRequested = Signal(object)
    previewRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.item: JobItem | None = None
        self._updating = False

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setSpacing(10)
        self.heading = QLabel(textFormat=Qt.TextFormat.RichText, wordWrap=True)
        self.heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.heading)
        self.callout = Callout()
        lay.addWidget(self.callout)

        # decision row
        self.decision_box = QGroupBox("Decision")
        dlay = QGridLayout(self.decision_box)
        dlay.setColumnStretch(2, 1)
        self.order_combo = QComboBox()
        self.order_combo.setObjectName("fieldOrderCombo")
        self.order_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.order_combo.addItem("Detected", None)
        self.order_combo.addItem("Override: TFF (top field first)", "tff")
        self.order_combo.addItem("Override: BFF (bottom field first)", "bff")
        self.order_combo.currentIndexChanged.connect(self._on_order)
        self.override_badge = QLabel(" OVERRIDE ")
        self.override_badge.setObjectName("overrideBadge")
        self.override_badge.setStyleSheet(
            "#overrideBadge { background: #b26a00; color: white; font-weight: bold; border-radius: 3px; padding: 2px 6px; }"
        )
        self.override_note = QLabel(wordWrap=True)
        self.force_check = QCheckBox("Process anyway (not recommended)")
        self.force_check.setObjectName("forceCheck")
        self.force_check.toggled.connect(self._on_force)
        self.force_note = QLabel(wordWrap=True)
        self.force_note.setStyleSheet("color: #c62828;")
        self.preview_button = QPushButton("Check in preview…")
        self.preview_button.clicked.connect(lambda: self.item and self.previewRequested.emit(self.item))
        dlay.addWidget(QLabel("Field order:"), 0, 0)
        dlay.addWidget(self.order_combo, 0, 1)
        dlay.addWidget(self.override_badge, 0, 2, Qt.AlignmentFlag.AlignLeft)
        dlay.addWidget(self.preview_button, 0, 3)
        dlay.addWidget(self.override_note, 1, 0, 1, 4)
        dlay.addWidget(self.force_check, 2, 0, 1, 4)
        dlay.addWidget(self.force_note, 3, 0, 1, 4)
        lay.addWidget(self.decision_box)

        cols = QHBoxLayout()
        cols.setSpacing(10)
        self.source_box = QGroupBox("Source (ffprobe)")
        self.source_form = QFormLayout(self.source_box)
        self.source_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        cols.addWidget(self.source_box, 1)
        self.idet_box = QGroupBox("Interlace detection (idet)")
        ilay = QVBoxLayout(self.idet_box)
        self.idet_grid = QGridLayout()
        self.idet_grid.setHorizontalSpacing(16)
        ilay.addLayout(self.idet_grid)
        self.idet_notes = QLabel(wordWrap=True, textFormat=Qt.TextFormat.RichText)
        self.idet_notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        ilay.addWidget(self.idet_notes)
        ilay.addStretch(1)
        cols.addWidget(self.idet_box, 1)
        lay.addLayout(cols)

        self.error_box = QGroupBox("Analysis failed")
        elay = QVBoxLayout(self.error_box)
        self.error_label = QLabel(wordWrap=True)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.retry_button = QPushButton("Analyze again")
        self.retry_button.clicked.connect(lambda: self.item and self.reanalyzeRequested.emit(self.item))
        elay.addWidget(self.error_label)
        elay.addWidget(self.retry_button, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.error_box)
        lay.addStretch(1)
        self.setWidget(root)
        self.set_item(None)

    # ------------------------------------------------------------------ display
    def set_item(self, item: JobItem | None) -> None:
        self.item = item
        self._updating = True
        try:
            self._render()
        finally:
            self._updating = False

    def refresh(self) -> None:
        self.set_item(self.item)

    def _render(self) -> None:
        item = self.item
        a = item.analysis if item else None
        has = a is not None
        for w in (self.decision_box, self.source_box, self.idet_box):
            w.setVisible(has)
        self.error_box.setVisible(bool(item and item.status == JobStatus.ANALYSIS_FAILED))
        if not item:
            self.heading.setText("<h3>No file selected</h3>Add files to the queue; each one is analysed automatically.")
            self.callout.hide()
            return
        self.heading.setText(f"<h3 style='margin-bottom:2px'>{_esc(item.name)}</h3><span>{_esc(item.path)}</span>")
        if item.status == JobStatus.ANALYSIS_FAILED:
            err = item.error or {}
            self.error_label.setText(f"{err.get('message', 'Unknown error')}\n{err.get('hint') or ''}".strip())
            self.callout.hide()
            return
        if not a:
            self.callout.show()
            self.callout.set("warn", "Analysis pending", item.status_text())
            return
        self.callout.show()
        self._render_callout(item, a)
        self._render_decision(item, a)
        self._render_source(a)
        self._render_idet(a)

    def _render_callout(self, item: JobItem, a: AnalysisResult) -> None:
        c = a.classification
        conf = f"{c.confidence * 100:.0f}% confidence"
        title = f"Detected: {CONTENT_TEXT.get(c.content, c.content)} ({conf})"
        act = a.recommendation.action
        msg = _esc(a.recommendation.message)
        if act == RecommendedAction.BLOCK_TELECINED:
            self.callout.set("block", title, IVTC_TEXT)
        elif act == RecommendedAction.BLOCK_MIXED:
            self.callout.set("block", title, MIXED_TEXT)
        elif act == RecommendedAction.SKIP_PROGRESSIVE:
            self.callout.set("warn", title, PROGRESSIVE_TEXT + (f"<br><br><i>Analysis:</i> {msg}" if msg else ""))
        elif act == RecommendedAction.CONFIRM_FIELD_ORDER:
            self.callout.set("warn", title, UNDETERMINED_TEXT + (f"<br><br><i>Analysis:</i> {msg}" if msg else ""))
        else:
            self.callout.set("ok", title, msg or "Ready to deinterlace.")
        warn = [*c.warnings]
        if warn:
            self.callout.body.setText(self.callout.body.text() + "<br>" + "<br>".join("<b>Warning:</b> " + _esc(w) for w in warn))
            self.callout.body.show()

    def _render_decision(self, item: JobItem, a: AnalysisResult) -> None:
        detected = item.detected_order()
        self.order_combo.setItemText(0, f"Detected: {detected.upper()}" if detected else "Detected: unknown — choose one")
        idx = {None: 0, "tff": 1, "bff": 2}[item.field_order_override]
        self.order_combo.setCurrentIndex(idx)
        overridden = item.field_order_override is not None
        self.override_badge.setVisible(overridden)
        self.order_combo.setStyleSheet("QComboBox { border: 2px solid #b26a00; border-radius: 3px; }" if overridden else "")
        if overridden and detected and detected != item.field_order_override:
            self.override_note.setText(
                f"You override the detected field order ({detected.upper()}) with "
                f"{item.field_order_override.upper()}. A wrong field order makes motion judder; confirm in the preview."
            )
        elif overridden:
            self.override_note.setText(f"Field order set manually to {item.field_order_override.upper()}.")
        else:
            self.override_note.setText("")
        self.override_note.setVisible(bool(self.override_note.text()))

        act = a.recommendation.action
        blocked = act in (RecommendedAction.BLOCK_TELECINED, RecommendedAction.BLOCK_MIXED, RecommendedAction.SKIP_PROGRESSIVE)
        self.force_check.setVisible(blocked and a.recommendation.force_allowed)
        self.force_check.setChecked(item.force)
        if act in (RecommendedAction.BLOCK_TELECINED, RecommendedAction.BLOCK_MIXED):
            self.force_check.setText("Deinterlace anyway, without IVTC (not recommended)")
        else:
            self.force_check.setText("Deinterlace anyway (not recommended)")
        note = item.decision_needed() or ""
        if item.force:
            note = "Override active: this file will be deinterlaced against the recommendation." + (
                f" {note}" if note else ""
            )
        self.force_note.setText(note)
        self.force_note.setVisible(bool(note))

    def _render_source(self, a: AnalysisResult) -> None:
        form = self.source_form
        while form.rowCount():
            form.removeRow(0)
        p, v = a.probe, a.probe.video

        def row(label: str, value: str) -> None:
            lab = QLabel(value)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lab.setWordWrap(True)
            form.addRow(label + ":", lab)

        row("Container", p.container or "–")
        row("Video", f"{v.codec} {v.width}×{v.height}, {v.pix_fmt} ({v.bit_depth}-bit)")
        row("Aspect", f"SAR {fmt_fraction(v.sar, ':')}, DAR {fmt_fraction(v.dar, ':')}")
        row("Frame rate", fmt_fps(v.fps))
        if v.nb_frames:
            frames = f"{v.nb_frames} frames" + ("" if v.nb_frames_exact else ", estimated")
        else:
            frames = "frame count unknown"
        row("Duration", f"{format_duration(v.duration_s or p.duration_s)} ({frames})")
        row("Container field order", f"{v.container_field_order} (informational; not used)")
        col = a.colorimetry
        row("Colour", f"{col.matrix} / {col.primaries} / {col.transfer}, range {col.range} — from {col.source}")
        if not p.audio:
            row("Audio", "none")
        for i, au in enumerate(p.audio, 1):
            lang = f" [{au.language}]" if au.language else ""
            row(f"Audio {i}", f"{au.codec} {au.sample_rate} Hz, {au.channel_layout or au.channels}{lang}")
        out_fps = fmt_fps(a.fps_out_double) if a.fps_out_double else "–"
        row("Output (double rate)", f"{a.frames_out_double or '–'} frames at {out_fps}")
        row("Default preset", str(a.preset_default))

    def _render_idet(self, a: AnalysisResult) -> None:
        g = self.idet_grid
        while g.count():
            w = g.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        heads = ["", "Single", "Multi"] + (["After fieldmatch"] if a.fieldmatch_run else [])
        for col, h in enumerate(heads):
            g.addWidget(QLabel(f"<b>{h}</b>"), 0, col, Qt.AlignmentFlag.AlignRight if col else Qt.AlignmentFlag.AlignLeft)
        after = None
        if a.fieldmatch_run:
            after = _sum_after(a)
        for r, (name, attr) in enumerate(
            (("TFF", "tff"), ("BFF", "bff"), ("Progressive", "progressive"), ("Undetermined", "undetermined")), 1
        ):
            g.addWidget(QLabel(name), r, 0)
            vals = [getattr(a.totals_single, attr), getattr(a.totals_multi, attr)]
            if after is not None:
                vals.append(after.get(attr, 0))
            for c, val in enumerate(vals, 1):
                lab = QLabel(str(val))
                lab.setAlignment(Qt.AlignmentFlag.AlignRight)
                g.addWidget(lab, r, c)
        rep = a.totals_repeated
        ratio = (rep.top + rep.bottom) / rep.total if rep.total else 0.0
        sampled = "all frames" if a.sampled_all_frames else f"{len(a.windows)} windows"
        notes = [
            f"Repeated fields: top {rep.top}, bottom {rep.bottom}, neither {rep.neither} "
            f"(<b>{ratio * 100:.0f}%</b>; ~40% is the 3:2 pulldown signature)",
            f"Sampled: {sampled}; analysis took {a.took_s:.1f} s",
        ]
        notes += [_esc(r) for r in a.classification.reasons]
        self.idet_notes.setText("<br>".join(notes))

    # ------------------------------------------------------------------ edits
    def _on_order(self, idx: int) -> None:
        if self._updating or not self.item:
            return
        self.item.field_order_override = self.order_combo.itemData(idx)
        self._changed()

    def _on_force(self, on: bool) -> None:
        if self._updating or not self.item or not self.item.analysis:
            return
        if on:
            content = self.item.analysis.classification.content
            if content in (ContentClass.TELECINED, ContentClass.MIXED):
                text = (
                    "This file needs inverse telecine (IVTC) with another tool. Deinterlacing it with QTGMC "
                    "produces a worse result than IVTC.\n\nDeinterlace it anyway?"
                )
            else:
                text = "This file looks progressive. Deinterlacing it is not recommended.\n\nDeinterlace it anyway?"
            answer = QMessageBox.warning(
                self, "Override the analysis?", text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._updating = True
                self.force_check.setChecked(False)
                self._updating = False
                return
        self.item.force = on
        self._changed()

    def _changed(self) -> None:
        assert self.item is not None
        self.item.refresh_status()
        self.decisionChanged.emit(self.item)
        self.refresh()


def _sum_after(a: AnalysisResult) -> dict[str, int]:
    out = {"tff": 0, "bff": 0, "progressive": 0, "undetermined": 0}
    for w in a.windows:
        if w.after_fieldmatch:
            for k in out:
                out[k] += getattr(w.after_fieldmatch, k)
    return out


def _esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
