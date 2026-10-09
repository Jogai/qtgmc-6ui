"""Archival master settings (rate, preset, fidelity, denoise) and output folder/naming."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..models import AnalysisResult, ChromaMode, MasterSettings, Preset, RateMode
from . import ui_state

PRESET_LABELS = {
    Preset.DRAFT: "draft (fastest)",
    Preset.FASTER: "faster",
    Preset.FAST: "fast",
    Preset.MEDIUM: "medium",
    Preset.SLOW: "slow",
    Preset.SLOWER: "slower",
    Preset.VERY_SLOW: "very slow",
    Preset.PLACEBO: "placebo (slowest)",
}

DENOISE_WARNING = (
    "Denoising alters the image. With lossless mode the <b>denoised</b> fields are woven back, so the "
    "original pixels are no longer preserved in the master. Needs the optional dfttest2 plugin."
)
LOSSLESS_OFF_WARNING = "Without lossless mode, synthesised lines replace source lines: the master is no longer field-exact."
SOURCE_MATCH_OFF_NOTE = "Lossless still holds without source match; source match only improves the synthesised lines."


class WrapLabel(QLabel):
    """Word-wrapped label that reserves its wrapped height (QFormLayout rows otherwise clip it)."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        h = self.heightForWidth(self.width())
        if h > 0 and h != self.minimumHeight():
            self.setMinimumHeight(h)


def warning_label() -> QLabel:
    lab = WrapLabel()
    lab.setTextFormat(Qt.TextFormat.RichText)
    lab.setStyleSheet("color: #b26a00;")
    return lab


class SettingsPanel(QScrollArea):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._analysis: AnalysisResult | None = None
        self._views_selected = 0
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setSpacing(10)

        # --- deinterlacing
        box = QGroupBox("Archival master (QTGMC → FFV1 + FLAC, lossless)")
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        order_note = QLabel("Detected per file; override it in the <i>Analysis</i> tab.")
        form.addRow("Field order:", order_note)

        self.rate_double = QRadioButton("Double rate — one frame per field (25i → 50p, 29.97i → 59.94p)")
        self.rate_single = QRadioButton("Single rate — one frame per frame (25i → 25p)")
        self.rate_group = QButtonGroup(self)
        self.rate_group.addButton(self.rate_double)
        self.rate_group.addButton(self.rate_single)
        rate_box = QVBoxLayout()
        rate_box.addWidget(self.rate_double)
        rate_box.addWidget(self.rate_single)
        form.addRow("Rate:", rate_box)

        self.preset_combo = QComboBox()
        self.preset_combo.setObjectName("presetCombo")
        self.preset_combo.addItem("Auto (by resolution)", None)
        for p in Preset:
            self.preset_combo.addItem(PRESET_LABELS[p], p.value)
        self.preset_hint = WrapLabel()
        self.preset_hint.setStyleSheet("color: palette(placeholder-text);")
        pv = QVBoxLayout()
        pv.addWidget(self.preset_combo)
        pv.addWidget(self.preset_hint)
        form.addRow("Preset:", pv)

        self.source_match = QCheckBox("Source match (refines synthesised lines against the source)")
        self.lossless = QCheckBox("Lossless (original field lines copied back unchanged)")
        self.fidelity_note = warning_label()
        fv = QVBoxLayout()
        fv.addWidget(self.source_match)
        fv.addWidget(self.lossless)
        fv.addWidget(self.fidelity_note)
        form.addRow("Fidelity:", fv)

        self.denoise = QCheckBox("Denoise (alters the image)")
        self.denoise.setObjectName("denoiseCheck")
        self.denoise_sigma = QDoubleSpinBox(minimum=0.1, maximum=16.0, singleStep=0.5, decimals=1, value=2.0)
        self.denoise_sigma.setPrefix("sigma ")
        self.denoise_warning = warning_label()
        self.denoise_warning.setText("<b>Warning:</b> " + DENOISE_WARNING)
        dh = QHBoxLayout()
        dh.addWidget(self.denoise)
        dh.addWidget(self.denoise_sigma)
        dh.addStretch(1)
        dv = QVBoxLayout()
        dv.addLayout(dh)
        dv.addWidget(self.denoise_warning)
        form.addRow("Denoise:", dv)
        lay.addWidget(box)

        # --- advanced
        adv = QGroupBox("Advanced")
        aform = QFormLayout(adv)
        self.chroma = QComboBox()
        self.chroma.addItem("Keep source subsampling (4:1:1 stays 4:1:1, bit-exact)", ChromaMode.KEEP.value)
        self.chroma.addItem("Keep 4:2:2 intermediate (4:1:1 sources only)", ChromaMode.YUV422.value)
        aform.addRow("Chroma:", self.chroma)
        self.threads = QSpinBox(minimum=0, maximum=256, specialValueText="all cores")
        self.cache_mb = QSpinBox(minimum=0, maximum=262144, singleStep=512, specialValueText="default by resolution")
        self.cache_mb.setSuffix(" MB")
        aform.addRow("Threads:", self.threads)
        aform.addRow("Frame cache:", self.cache_mb)
        lay.addWidget(adv)

        # --- output
        out = QGroupBox("Output")
        oform = QFormLayout(out)
        oform.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.out_dir = QLineEdit()
        self.out_dir.setObjectName("outDirEdit")
        self.out_dir.setPlaceholderText("Next to each input file")
        self.out_dir.setClearButtonEnabled(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        oh = QHBoxLayout()
        oh.addWidget(self.out_dir, 1)
        oh.addWidget(browse)
        oform.addRow("Folder:", oh)
        self.name_template = QLineEdit("{stem}.deint")
        self.name_template.setObjectName("nameTemplateEdit")
        self.name_preview = WrapLabel()
        self.name_preview.setStyleSheet("color: palette(placeholder-text);")
        nv = QVBoxLayout()
        nv.addWidget(self.name_template)
        nv.addWidget(self.name_preview)
        oform.addRow("Name:", nv)
        self.keep_master = QCheckBox("Keep the archival master (FFV1 + FLAC)")
        self.keep_master.setObjectName("keepMasterCheck")
        self.keep_master.setChecked(True)  # on at every start: the master is the archival result
        self.keep_master_hint = WrapLabel()
        self.keep_master_hint.setTextFormat(Qt.TextFormat.RichText)
        km = QVBoxLayout()
        km.addWidget(self.keep_master)
        km.addWidget(self.keep_master_hint)
        oform.addRow("Master:", km)
        self.overwrite = QCheckBox("Overwrite existing output files")
        oform.addRow("", self.overwrite)
        note = WrapLabel("The input file is never modified. Viewing copies are encoded from the master.")
        oform.addRow("", note)
        lay.addWidget(out)
        lay.addStretch(1)
        self.setWidget(root)

        self._load()
        for sig in (
            self.rate_group.buttonToggled, self.preset_combo.currentIndexChanged, self.source_match.toggled,
            self.lossless.toggled, self.denoise.toggled, self.denoise_sigma.valueChanged, self.chroma.currentIndexChanged,
            self.threads.valueChanged, self.cache_mb.valueChanged, self.out_dir.textChanged,
            self.name_template.textChanged, self.overwrite.toggled, self.keep_master.toggled,
        ):
            sig.connect(self._on_change)
        self._update_notes()

    # ------------------------------------------------------------------ values
    def master_settings(self) -> MasterSettings:
        preset = self.preset_combo.currentData()
        return MasterSettings(
            rate=RateMode.SINGLE if self.rate_single.isChecked() else RateMode.DOUBLE,
            preset=Preset(preset) if preset else None,
            source_match=self.source_match.isChecked(),
            lossless=self.lossless.isChecked(),
            denoise_sigma=self.denoise_sigma.value() if self.denoise.isChecked() else 0.0,
            chroma=ChromaMode(self.chroma.currentData()),
            threads=self.threads.value(),
            cache_mb=self.cache_mb.value(),
        )

    def keeps_master(self) -> bool:
        return self.keep_master.isChecked()

    def set_views_selected(self, n: int) -> None:
        self._views_selected = n
        self._update_notes()

    def output_dir(self) -> str:
        return self.out_dir.text().strip()

    def name_template_text(self) -> str:
        return self.name_template.text().strip() or "{stem}.deint"

    def validate(self) -> str | None:
        tpl = self.name_template_text()
        if "{stem}" not in tpl:
            return "The output name must contain {stem} (the input file name without extension)."
        if "/" in tpl:
            return "The output name must not contain '/'; choose the folder separately."
        try:
            tpl.format(stem="x")
        except (KeyError, IndexError, ValueError):
            return "The output name may only use the {stem} placeholder."
        d = self.output_dir()
        if d and not Path(d).is_dir():
            return f"The output folder does not exist: {d}"
        return None

    def set_analysis(self, analysis: AnalysisResult | None) -> None:
        self._analysis = analysis
        self._update_notes()

    # ------------------------------------------------------------------ internals
    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.output_dir() or ui_state.get_str("last_input_dir"))
        if d:
            self.out_dir.setText(d)

    def _on_change(self, *args) -> None:  # noqa: ARG002
        self._update_notes()
        self._save()
        self.changed.emit()

    def _update_notes(self) -> None:
        if self.keep_master.isChecked():
            self.keep_master_hint.setStyleSheet("color: palette(placeholder-text);")
            self.keep_master_hint.setText(
                "The master is lossless and typically about 4× larger than the viewing copies. "
                "Without it, only the viewing copies are kept (the master is built temporarily and deleted)."
            )
        elif self._views_selected:
            self.keep_master_hint.setStyleSheet("color: #b26a00;")
            self.keep_master_hint.setText(
                f"<b>No archival master:</b> only the {self._views_selected} selected viewing "
                "cop" + ("y is" if self._views_selected == 1 else "ies are") + " kept. The deinterlaced "
                "lossless master is built temporarily and deleted afterwards."
            )
        else:
            self.keep_master_hint.setStyleSheet("color: #c62828;")
            self.keep_master_hint.setText(
                "<b>Select at least one viewing copy</b> (Viewing copies tab): without the master there "
                "would be nothing to keep. Start is disabled until then."
            )
        self.keep_master_hint.setVisible(True)
        self.denoise_sigma.setEnabled(self.denoise.isChecked())
        self.denoise_warning.setVisible(self.denoise.isChecked())
        notes = []
        if not self.lossless.isChecked():
            notes.append("<b>Warning:</b> " + LOSSLESS_OFF_WARNING)
        if not self.source_match.isChecked():
            notes.append(SOURCE_MATCH_OFF_NOTE)
        self.fidelity_note.setText("<br>".join(notes))
        self.fidelity_note.setVisible(bool(notes))
        a = self._analysis
        hint = "Slower presets only improve the synthesised lines when lossless is on; they cost a lot of time."
        if a:
            v = a.probe.video
            hint = f"Auto for the selected file ({v.width}×{v.height}): {a.preset_default}. " + hint
            if v.height > 576:
                hint += " HD is slow and memory-hungry: start with the default and go higher only if needed."
        self.preset_hint.setText(hint)
        stem = Path(a.input).stem if a else "clip"
        try:
            name = self.name_template_text().format(stem=stem)
            master = f"Master → {name}.mkv; v" if self.keep_master.isChecked() else "No master; v"
            self.name_preview.setText(f"{master}iewing copies → {name}.<profile>.<ext>")
        except (KeyError, IndexError, ValueError):
            self.name_preview.setText("Invalid template: only {stem} is allowed.")

    def _load(self) -> None:
        g = ui_state.get_str
        (self.rate_single if g("master/rate", "double") == "single" else self.rate_double).setChecked(True)
        idx = self.preset_combo.findData(g("master/preset") or None)
        self.preset_combo.setCurrentIndex(max(idx, 0))
        self.source_match.setChecked(ui_state.get_bool("master/source_match", True))
        self.lossless.setChecked(ui_state.get_bool("master/lossless", True))
        self.denoise.setChecked(False)  # off on every start, by design
        self.chroma.setCurrentIndex(max(self.chroma.findData(g("master/chroma", "keep")), 0))
        self.out_dir.setText(g("output/dir"))
        self.name_template.setText(g("output/name", "{stem}.deint") or "{stem}.deint")
        self.overwrite.setChecked(False)

    def _save(self) -> None:
        ms = self.master_settings()
        ui_state.put("master/rate", ms.rate.value)
        ui_state.put("master/preset", ms.preset.value if ms.preset else "")
        ui_state.put("master/source_match", ms.source_match)
        ui_state.put("master/lossless", ms.lossless)
        ui_state.put("master/chroma", ms.chroma.value)
        ui_state.put("output/dir", self.output_dir())
        ui_state.put("output/name", self.name_template_text())
