"""Viewing-copy profile editor: only valid container/codec combinations are selectable."""

from __future__ import annotations

import copy
import html
import re
import shlex

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..models import (
    AudioSettings,
    Profile,
    RateControl,
    ScaleMode,
    ScaleSettings,
    SoxSettings,
    VideoBackend,
    VideoSettings,
    ViewRateMode,
)
from .catalog import CLI_BACKENDS, Catalog

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RC_LABELS = {
    RateControl.CRF: "Constant quality (CRF)",
    RateControl.QP: "Constant quantizer (QP)",
    RateControl.CQ: "Constrained quality (CQ)",
    RateControl.BITRATE: "Average bitrate",
    RateControl.LOSSLESS: "Lossless",
}
INTRINSICALLY_LOSSLESS = {"ffv1", "utvideo", "huffyuv", "ffvhuff"}
CQ_ENCODERS = {"libvpx-vp9", "libvpx", "libaom-av1"}
LOSSLESS_CAPABLE = {"libx264", "libx265", "libvpx-vp9", "libaom-av1", "libsvtav1", "x264", "x265"}
SCALE_ALGOS = ("lanczos", "spline", "bicubic", "bilinear", "neighbor")
SAMPLE_RATES = (0, 44100, 48000, 96000)
CHANNELS = ((0, "keep"), (1, "mono"), (2, "stereo"), (6, "5.1"))


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "profile"


def rate_controls(encoder: str, option_names: set[str]) -> list[RateControl]:
    """Rate-control modes offered for an encoder (from its AVOptions where known)."""
    if encoder in INTRINSICALLY_LOSSLESS:
        return [RateControl.LOSSLESS]
    if encoder == "kvazaar":
        return [RateControl.QP, RateControl.BITRATE]
    out = []
    if encoder in CQ_ENCODERS:
        out.append(RateControl.CQ)
    elif "crf" in option_names or encoder in ("x264", "x265"):
        out.append(RateControl.CRF)
    if "qp" in option_names or encoder in ("x264", "x265"):
        out.append(RateControl.QP)
    out.append(RateControl.BITRATE)
    if encoder in LOSSLESS_CAPABLE:
        out.append(RateControl.LOSSLESS)
    return out


class ComboWithReasons(QComboBox):
    """Combo whose disabled entries stay visible, greyed, with the reason as tooltip."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self.setModel(self._model)

    def clear_items(self) -> None:
        self._model.clear()

    def add(self, text: str, data: object, enabled: bool = True, reason: str = "") -> None:
        it = QStandardItem(text if enabled else f"{text}  — not available")
        it.setData(data, Qt.ItemDataRole.UserRole)
        if reason:
            it.setToolTip(reason)
        if not enabled:
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
        self._model.appendRow(it)

    def select_data(self, data: object) -> bool:
        for row in range(self._model.rowCount()):
            it = self._model.item(row)
            if it.data(Qt.ItemDataRole.UserRole) == data:
                self.setCurrentIndex(row)
                return bool(it.flags() & Qt.ItemFlag.ItemIsEnabled)
        return False

    def is_enabled_row(self, row: int) -> bool:
        it = self._model.item(row)
        return bool(it and it.flags() & Qt.ItemFlag.ItemIsEnabled)

    def reason(self, row: int) -> str:
        it = self._model.item(row)
        return it.toolTip() if it else ""

    def disabled_count(self) -> int:
        return sum(1 for r in range(self._model.rowCount()) if not self.is_enabled_row(r))


def editable_combo(choices: list[str], value: str, empty_label: str = "(default)") -> QComboBox:
    cb = QComboBox()
    cb.setEditable(True)
    cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    set_choices(cb, choices, value, empty_label)
    return cb


def set_choices(cb: QComboBox, choices: list[str], value: str, empty_label: str = "(default)") -> None:
    cb.blockSignals(True)
    cb.clear()
    cb.addItem("", "")
    if cb.lineEdit():
        cb.lineEdit().setPlaceholderText(empty_label)
    for c in choices:
        if c:
            cb.addItem(c, c)
    cb.setCurrentText(value)
    cb.blockSignals(False)


class ProfileEditor(QDialog):
    saved = Signal(str)  # profile id

    def __init__(self, catalog: Catalog, profile: Profile | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.original = profile
        self.is_new = profile is None or profile.builtin
        base = copy.deepcopy(profile) if profile else Profile(id="", name="", container="matroska", extension="")
        if profile and profile.builtin:
            base.id, base.name, base.builtin, base.path = f"{profile.id}-custom", f"{profile.name} (custom)", False, None
        self.profile = base
        self.setWindowTitle("New viewing-copy profile" if profile is None else f"Profile: {base.name}")
        self.setObjectName("profileEditor")
        self._loading = True
        self._rc_pref = base.video.rate_control.value
        self._build()
        self._wire()
        if self.catalog.encoders:
            self._populate()
        else:
            self.status.setText("Loading the encoder list from FFmpeg…")
            self.catalog.encodersLoaded.connect(self._populate)
            self.catalog.load_encoders()
        avail = self.screen().availableSize()
        self.resize(min(1060, int(avail.width() * 0.95)), min(820, int(avail.height() * 0.92)))

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        p = self.profile
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setSpacing(10)

        gen = QGroupBox("Profile")
        gf = QFormLayout(gen)
        gf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name_edit = QLineEdit(p.name)
        self.name_edit.setObjectName("profileName")
        self.id_edit = QLineEdit(p.id)
        self.id_edit.setObjectName("profileId")
        self.id_edit.setToolTip("File name of the profile: lowercase letters, digits and '-'.")
        self.desc_edit = QLineEdit(p.description)
        gf.addRow("Name:", self.name_edit)
        gf.addRow("ID / file name:", self.id_edit)
        gf.addRow("Description:", self.desc_edit)
        lay.addWidget(gen)

        cont = QGroupBox("Container")
        cg = QGridLayout(cont)
        self.container_combo = ComboWithReasons()
        self.container_combo.setObjectName("containerCombo")
        self.all_containers = QCheckBox("Show all FFmpeg containers")
        self.ext_edit = QLineEdit(p.extension)
        self.ext_edit.setMaximumWidth(self.fontMetrics().horizontalAdvance("matroska") + 24)
        cg.addWidget(QLabel("Format:"), 0, 0)
        cg.addWidget(self.container_combo, 0, 1)
        cg.addWidget(QLabel("Extension:"), 0, 2)
        cg.addWidget(self.ext_edit, 0, 3)
        cg.addWidget(self.all_containers, 1, 1, 1, 3)
        cg.setColumnStretch(1, 1)
        lay.addWidget(cont)

        cols = QHBoxLayout()
        cols.setSpacing(10)
        # video
        vid = QGroupBox("Video")
        vf = QFormLayout(vid)
        vf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.venc_combo = ComboWithReasons()
        self.venc_combo.setObjectName("videoEncoderCombo")
        self.all_vencoders = QCheckBox("Show all encoders")
        self.venc_note = QLabel(wordWrap=True)
        self.venc_note.setStyleSheet("color: palette(placeholder-text);")
        ve = QVBoxLayout()
        ve.addWidget(self.venc_combo)
        ve.addWidget(self.all_vencoders)
        ve.addWidget(self.venc_note)
        vf.addRow("Encoder:", ve)
        self.rc_combo = QComboBox()
        self.rc_combo.setObjectName("rateControlCombo")
        vf.addRow("Rate control:", self.rc_combo)
        self.quality_spin = QDoubleSpinBox(minimum=0, maximum=63, decimals=1, singleStep=1, value=p.video.quality)
        self.bitrate_spin = QSpinBox(minimum=100, maximum=500_000, singleStep=500, value=p.video.bitrate_kbps or 4000)
        self.bitrate_spin.setSuffix(" kbit/s")
        self.quality_label = QLabel("Quality:")
        self.bitrate_label = QLabel("Bitrate:")
        vf.addRow(self.quality_label, self.quality_spin)
        vf.addRow(self.bitrate_label, self.bitrate_spin)
        self.preset_combo = editable_combo([], p.video.preset)
        self.tune_combo = editable_combo([], p.video.tune, "(none)")
        self.vprofile_combo = editable_combo([], p.video.profile)
        self.level_edit = QLineEdit(p.video.level)
        self.level_edit.setPlaceholderText("(automatic)")
        self.pixfmt_combo = editable_combo([], p.video.pix_fmt, "keep master format")
        vf.addRow("Preset:", self.preset_combo)
        vf.addRow("Tune:", self.tune_combo)
        vf.addRow("Profile:", self.vprofile_combo)
        vf.addRow("Level:", self.level_edit)
        vf.addRow("Pixel format:", self.pixfmt_combo)
        self.vrate_combo = QComboBox()
        self.vrate_combo.addItem("Keep master rate (e.g. 50p)", ViewRateMode.KEEP.value)
        self.vrate_combo.addItem("Single rate (drop every second frame, e.g. 25p)", ViewRateMode.SINGLE.value)
        vf.addRow("Frame rate:", self.vrate_combo)
        self.scale_combo = QComboBox()
        self.scale_combo.setObjectName("scaleCombo")
        self.scale_combo.addItem("Keep anamorphic (SAR carried over)", ScaleMode.KEEP.value)
        self.scale_combo.addItem("Square pixels (720×576 SAR 16:15 → 768×576)", ScaleMode.SQUARE.value)
        self.scale_combo.addItem("Custom size", ScaleMode.CUSTOM.value)
        self.scale_w = QSpinBox(minimum=0, maximum=7680, singleStep=2, value=p.video.scale.width)
        self.scale_h = QSpinBox(minimum=0, maximum=4320, singleStep=2, value=p.video.scale.height)
        self.scale_w.setSpecialValueText("auto")
        self.scale_h.setSpecialValueText("auto")
        self.scale_algo = QComboBox()
        self.scale_algo.addItems(list(SCALE_ALGOS))
        sh = QHBoxLayout()
        sh.addWidget(self.scale_w)
        sh.addWidget(QLabel("×"))
        sh.addWidget(self.scale_h)
        sh.addWidget(self.scale_algo, 1)
        self.scale_custom = QWidget()
        self.scale_custom.setLayout(sh)
        sh.setContentsMargins(0, 0, 0, 0)
        sv = QVBoxLayout()
        sv.addWidget(self.scale_combo)
        sv.addWidget(self.scale_custom)
        vf.addRow("Scaling:", sv)
        self.vextra_edit = QLineEdit(shlex.join(p.video.extra_args))
        self.vextra_edit.setPlaceholderText("advanced: extra encoder arguments")
        vf.addRow("Extra args:", self.vextra_edit)
        cols.addWidget(vid, 3)

        # audio
        aud = QGroupBox("Audio")
        af = QFormLayout(aud)
        af.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.aenc_combo = ComboWithReasons()
        self.aenc_combo.setObjectName("audioEncoderCombo")
        self.all_aencoders = QCheckBox("Show all encoders")
        self.aenc_note = QLabel(wordWrap=True)
        self.aenc_note.setStyleSheet("color: palette(placeholder-text);")
        ae = QVBoxLayout()
        ae.addWidget(self.aenc_combo)
        ae.addWidget(self.all_aencoders)
        ae.addWidget(self.aenc_note)
        af.addRow("Encoder:", ae)
        self.abitrate_spin = QSpinBox(minimum=0, maximum=1536, singleStep=16, value=p.audio.bitrate_kbps)
        self.abitrate_spin.setSuffix(" kbit/s")
        self.abitrate_spin.setSpecialValueText("encoder default")
        af.addRow("Bitrate:", self.abitrate_spin)
        self.srate_combo = QComboBox()
        for sr in SAMPLE_RATES:
            self.srate_combo.addItem("keep" if sr == 0 else f"{sr} Hz", sr)
        af.addRow("Sample rate:", self.srate_combo)
        self.channels_combo = QComboBox()
        for ch, label in CHANNELS:
            self.channels_combo.addItem(label, ch)
        af.addRow("Channels:", self.channels_combo)
        self.aextra_edit = QLineEdit(shlex.join(p.audio.extra_args))
        self.aextra_edit.setPlaceholderText("advanced: extra encoder arguments")
        af.addRow("Extra args:", self.aextra_edit)
        self.sox_box = QGroupBox("SoX processing (before encoding)")
        self.sox_box.setCheckable(True)
        self.sox_box.setChecked(p.audio.sox.enabled)
        sxl = QVBoxLayout(self.sox_box)
        self.sox_edit = QLineEdit(shlex.join(p.audio.sox.effects))
        self.sox_edit.setPlaceholderText("e.g. rate -v 48000 dither -s")
        sxl.addWidget(self.sox_edit)
        af.addRow(self.sox_box)
        self.sox_missing = QLabel("SoX audio processing appears here when sox is installed (optional dependency).")
        self.sox_missing.setWordWrap(True)
        self.sox_missing.setStyleSheet("color: palette(placeholder-text);")
        af.addRow(self.sox_missing)
        cols.addWidget(aud, 2)
        lay.addLayout(cols)
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.status = QLabel(wordWrap=True, textFormat=Qt.TextFormat.RichText)
        self.status.setObjectName("editorStatus")
        self.busy = QWidget()
        bl = QHBoxLayout(self.busy)
        bl.setContentsMargins(0, 0, 0, 0)
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setMaximumWidth(self.fontMetrics().horizontalAdvance("x") * 16)
        self.busy_bar.setMaximumHeight(self.fontMetrics().height())
        self.busy_label = QLabel()
        bl.addWidget(self.busy_bar)
        bl.addWidget(self.busy_label, 1)
        self.busy.hide()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll, 1)
        outer.addWidget(self.busy)
        outer.addWidget(self.status)
        outer.addWidget(self.buttons)
        body.setMinimumWidth(body.sizeHint().width())
        scroll.setMinimumWidth(min(body.sizeHint().width() + 24, 1100))

    def _wire(self) -> None:
        self.name_edit.textEdited.connect(self._on_name)
        self.container_combo.currentIndexChanged.connect(self._on_container)
        self.all_containers.toggled.connect(lambda _: self._fill_containers())
        self.venc_combo.currentIndexChanged.connect(self._on_vencoder)
        self.all_vencoders.toggled.connect(lambda _: self._fill_encoders())
        self.aenc_combo.currentIndexChanged.connect(self._on_aencoder)
        self.all_aencoders.toggled.connect(lambda _: self._fill_encoders())
        self.rc_combo.currentIndexChanged.connect(lambda _: self._update_rc())
        self.rc_combo.activated.connect(lambda _: setattr(self, "_rc_pref", self.rc_combo.currentData()))
        self.scale_combo.currentIndexChanged.connect(lambda _: self.scale_custom.setVisible(self.scale_combo.currentData() == "custom"))
        self.catalog.probesLoaded.connect(self._on_probes)
        self.catalog.encoderDetailsLoaded.connect(self._on_details)

    # ------------------------------------------------------------------ population
    def _populate(self) -> None:
        try:
            self.catalog.encodersLoaded.disconnect(self._populate)
        except (RuntimeError, TypeError):
            pass
        p = self.profile
        self._loading = True
        if self.catalog.encoders_error:
            self.status.setText(f"<span style='color:#c62828'>{self.catalog.encoders_error}</span>")
        else:
            self.status.setText("")
        if p.container not in self.catalog.containers(False):
            self.all_containers.setChecked(True)
        self._fill_containers()
        self.vrate_combo.setCurrentIndex(max(self.vrate_combo.findData(p.video.rate.value), 0))
        self.scale_combo.setCurrentIndex(max(self.scale_combo.findData(p.video.scale.mode.value), 0))
        self.scale_custom.setVisible(p.video.scale.mode == ScaleMode.CUSTOM)
        self.scale_algo.setCurrentText(p.video.scale.algorithm)
        self.srate_combo.setCurrentIndex(max(self.srate_combo.findData(p.audio.sample_rate), 0))
        self.channels_combo.setCurrentIndex(max(self.channels_combo.findData(p.audio.channels), 0))
        sox = self.catalog.tool_available("sox")
        self.sox_box.setVisible(sox)
        self.sox_missing.setVisible(not sox)
        self._loading = False
        self._on_container()

    def current_container(self) -> str:
        return self.container_combo.currentData() or self.profile.container

    def _fill_containers(self) -> None:
        cur = self.container_combo.currentData() or self.profile.container
        self.container_combo.blockSignals(True)
        self.container_combo.clear_items()
        names = self.catalog.containers(self.all_containers.isChecked())
        if cur and cur not in names:
            names.append(cur)
        for n in names:
            m = self.catalog.muxers.get(n)
            label = f"{n} — {m.description}" if m and m.description else n
            self.container_combo.add(label, n)
        self.container_combo.select_data(cur)
        self.container_combo.blockSignals(False)

    def _on_container(self) -> None:
        if self._loading:
            return
        c = self.current_container()
        self._sync_extension()
        self.catalog.load_probes(c)
        self._fill_encoders()

    def _sync_extension(self) -> None:
        """File extension from the backend's muxer data (``-h muxer=``), never guessed here."""
        m = self.catalog.muxers.get(self.current_container())
        ext = self.ext_edit.text().strip().lstrip(".")
        if m and m.extensions and ext not in m.extensions:
            self.ext_edit.setText(m.extensions[0])

    def _on_probes(self, container: str) -> None:
        if container == self.current_container():
            self._sync_extension()
            self._fill_encoders()

    def _video_entries(self) -> list[tuple[str, str, str, VideoBackend]]:
        """(label, key, verdict-encoder, backend); key is the encoder name or the CLI tool name."""
        out = []
        for n in self.catalog.encoder_names("video", self.all_vencoders.isChecked()):
            e = self.catalog.encoders[n]
            out.append((_label(n, e.description), n, n, VideoBackend.FFMPEG))
        for tool, (ff, codec) in CLI_BACKENDS.items():
            if self.catalog.tool_available(tool):
                out.append((f"{tool} — standalone {codec} encoder program", tool, ff, VideoBackend(tool)))
        return out

    def _fill_encoders(self) -> None:
        c = self.current_container()
        p = self.profile
        loading = self.catalog.probes_pending(c)
        self.busy.setVisible(loading)
        self.busy_label.setText(f"Testing which codecs {c} accepts… (you can keep editing)")
        vkey = self.venc_combo.currentData() or (p.video.encoder if p.video.backend == VideoBackend.FFMPEG else p.video.backend.value)
        akey = self.aenc_combo.currentData() or p.audio.encoder
        self._loading = True
        self.venc_combo.clear_items()
        entries = self._video_entries()
        if vkey not in [k for _, k, _, _ in entries]:
            entries.append((vkey, vkey, vkey, VideoBackend.FFMPEG))
        for label, key, ff, _ in entries:
            verdict, why = self.catalog.verdict(c, ff)
            self.venc_combo.add(label, key, verdict != "fail", why or self._unknown_tip(verdict, loading))
        self.venc_combo.select_data(vkey)
        missing_tools = [t for t in CLI_BACKENDS if not self.catalog.tool_available(t)]
        notes = []
        if n := self.venc_combo.disabled_count():
            notes.append(f"{n} encoder(s) greyed out: {c} cannot hold them (hover for the reason).")
        if missing_tools:
            notes.append("Standalone " + "/".join(missing_tools) + " appear here when installed.")
        self.venc_note.setText(" ".join(notes))
        self.venc_note.setVisible(bool(notes))

        self.aenc_combo.clear_items()
        anames = self.catalog.encoder_names("audio", self.all_aencoders.isChecked())
        self.aenc_combo.add("copy — keep the master's FLAC audio", "copy", *self._audio_verdict(c, "flac"))
        if akey not in anames and akey != "copy":
            anames.append(akey)
        for n in anames:
            e = self.catalog.encoders.get(n)
            verdict, why = self.catalog.verdict(c, n)
            self.aenc_combo.add(_label(n, e.description if e else ""), n, verdict != "fail", why or self._unknown_tip(verdict, loading))
        self.aenc_combo.select_data(akey)
        an = self.aenc_combo.disabled_count()
        self.aenc_note.setText(f"{an} encoder(s) greyed out: {c} cannot hold them (hover for the reason)." if an else "")
        self.aenc_note.setVisible(bool(an))
        self._loading = False
        self._on_vencoder()
        self._on_aencoder()

    @staticmethod
    def _unknown_tip(verdict: str, loading: bool) -> str:
        if verdict != "unknown":
            return ""
        return "Compatibility is being tested…" if loading else "Compatibility with this container was not tested."

    def _audio_verdict(self, container: str, encoder: str) -> tuple[bool, str]:
        verdict, why = self.catalog.verdict(container, encoder)
        return verdict != "fail", why

    def _vencoder_ffmpeg_name(self) -> str:
        key = self.venc_combo.currentData() or ""
        return CLI_BACKENDS[key][0] if key in CLI_BACKENDS else key

    def _on_vencoder(self) -> None:
        if self._loading:
            return
        ff = self._vencoder_ffmpeg_name()
        if ff:
            self.catalog.load_details(ff)
        self._apply_details()
        self._check_selection()

    def _on_aencoder(self) -> None:
        if self._loading:
            return
        enc = self.aenc_combo.currentData()
        lossless = enc in ("copy", "flac", "alac") or str(enc or "").startswith("pcm_")
        self.abitrate_spin.setEnabled(not lossless)
        self._check_selection()

    def _on_details(self, name: str) -> None:
        if name == self._vencoder_ffmpeg_name():
            self._apply_details()

    def _apply_details(self) -> None:
        key = self.venc_combo.currentData() or ""
        ff = self._vencoder_ffmpeg_name()
        info = self.catalog.details.get(ff)
        opts = {o.name: o for o in info.options} if info else {}
        v = self.profile.video
        cur_rc = self._rc_pref
        self.rc_combo.blockSignals(True)
        self.rc_combo.clear()
        for rc in rate_controls(key, set(opts)):
            self.rc_combo.addItem(RC_LABELS[rc], rc.value)
        idx = self.rc_combo.findData(cur_rc)
        self.rc_combo.setCurrentIndex(max(idx, 0))
        self.rc_combo.blockSignals(False)

        def choices(name: str) -> list[str]:
            o = opts.get(name)
            return list(o.choices) if o else []

        has = set(opts) if info else {"preset", "tune", "profile"}
        is_cli = key in CLI_BACKENDS
        for combo, name, val in (
            (self.preset_combo, "preset", self.preset_combo.currentText() or v.preset),
            (self.tune_combo, "tune", self.tune_combo.currentText() or v.tune),
            (self.vprofile_combo, "profile", self.vprofile_combo.currentText() or v.profile),
        ):
            set_choices(combo, choices(name), val, "(none)" if name == "tune" else "(encoder default)")
            combo.setEnabled(is_cli or name in has)
            combo.setToolTip("" if combo.isEnabled() else f"{ff} has no '{name}' option")
        pix = list(info.pix_fmts) if info else []
        set_choices(self.pixfmt_combo, pix, self.pixfmt_combo.currentText() or v.pix_fmt, "keep master format")
        self.level_edit.setEnabled(is_cli or "level" in has or not info)
        self._update_rc()

    def _update_rc(self) -> None:
        rc = self.rc_combo.currentData()
        quality = rc in ("crf", "qp", "cq")
        self.quality_spin.setVisible(quality)
        self.quality_label.setVisible(quality)
        self.bitrate_spin.setVisible(rc in ("bitrate", "cq"))
        self.bitrate_label.setVisible(rc in ("bitrate", "cq"))
        labels = {"crf": "CRF (lower = better):", "qp": "QP (lower = better):", "cq": "CQ level:"}
        self.quality_label.setText(labels.get(rc, "Quality:"))
        self.bitrate_label.setText("Max bitrate:" if rc == "cq" else "Bitrate:")

    def _check_selection(self) -> list[str]:
        problems = []
        for combo, what in ((self.venc_combo, "video"), (self.aenc_combo, "audio")):
            row = combo.currentIndex()
            if row >= 0 and not combo.is_enabled_row(row):
                problems.append(f"The {what} encoder is not valid for this container: {combo.reason(row)}")
        if problems:
            self.status.setText("<span style='color:#c62828'>" + "<br>• ".join(_esc(p) for p in problems) + "</span>")
        elif self.status.text().startswith("<span style='color:#c62828'>The"):
            self.status.setText("")
        return problems

    # ------------------------------------------------------------------ result
    def _on_name(self, text: str) -> None:
        if self.is_new and not self.id_edit.isModified():
            self.id_edit.setText(slugify(text))

    def build_profile(self) -> Profile:
        key = self.venc_combo.currentData() or self.profile.video.encoder
        backend = VideoBackend(key) if key in CLI_BACKENDS else VideoBackend.FFMPEG
        rc = RateControl(self.rc_combo.currentData() or "crf")
        video = VideoSettings(
            backend=backend, encoder=key, rate_control=rc, quality=self.quality_spin.value(),
            bitrate_kbps=self.bitrate_spin.value() if rc in (RateControl.BITRATE, RateControl.CQ) else 0,
            preset=self.preset_combo.currentText().strip(), tune=self.tune_combo.currentText().strip(),
            profile=self.vprofile_combo.currentText().strip(), level=self.level_edit.text().strip(),
            pix_fmt=self.pixfmt_combo.currentText().strip(), extra_args=shlex.split(self.vextra_edit.text()),
            scale=ScaleSettings(ScaleMode(self.scale_combo.currentData()), self.scale_w.value(), self.scale_h.value(),
                                self.scale_algo.currentText()),
            rate=ViewRateMode(self.vrate_combo.currentData()),
        )
        aenc = self.aenc_combo.currentData() or "aac"
        audio = AudioSettings(
            encoder=aenc, bitrate_kbps=self.abitrate_spin.value() if self.abitrate_spin.isEnabled() else 0,
            quality=self.profile.audio.quality, sample_rate=self.srate_combo.currentData() or 0,
            channels=self.channels_combo.currentData() or 0, extra_args=shlex.split(self.aextra_edit.text()),
            sox=SoxSettings(self.sox_box.isVisible() and self.sox_box.isChecked(), shlex.split(self.sox_edit.text())),
        )
        return Profile(
            id=self.id_edit.text().strip(), name=self.name_edit.text().strip(), description=self.desc_edit.text().strip(),
            container=self.current_container(), extension=self.ext_edit.text().strip().lstrip("."),
            video=video, audio=audio,
        )

    def local_problems(self) -> list[str]:
        problems = []
        try:
            prof = self.build_profile()
        except ValueError as e:  # shlex: unbalanced quotes
            return [f"Extra arguments: {e}"]
        if not prof.name:
            problems.append("Give the profile a name.")
        if not ID_RE.match(prof.id):
            problems.append("The ID may only contain lowercase letters, digits and '-'.")
        builtin_ids = {p.id for p in self.catalog.profiles if p.builtin}
        if prof.id in builtin_ids:
            problems.append(f"'{prof.id}' is a built-in profile ID; choose another.")
        taken = {p.id for p in self.catalog.profiles if not p.builtin}
        if prof.id in taken and (self.is_new or (self.original and prof.id != self.original.id)):
            problems.append(f"A profile with ID '{prof.id}' already exists.")
        if not prof.extension:
            problems.append("Set a file extension.")
        problems += self._check_selection()
        return problems

    def save(self) -> None:
        problems = self.local_problems()
        if problems:
            self.status.setText("<span style='color:#c62828'>" + "<br>• ".join(_esc(p) for p in problems) + "</span>")
            return
        prof = self.build_profile()
        editing = not self.is_new and self.original is not None
        self.buttons.setEnabled(False)
        self.status.setText("Saving…")
        self.catalog.save_profile(
            prof, lambda ok, probs, path: self._after_save(prof, ok, probs),
            overwrite=editing and prof.id == self.original.id,
            replaces=self.original.id if editing else None,
        )

    def _after_save(self, prof: Profile, ok: bool, problems: list[str]) -> None:
        self.buttons.setEnabled(True)
        if not ok:
            self.status.setText(
                "<span style='color:#c62828'>The profile was not saved:<br>• "
                + "<br>• ".join(_esc(p) for p in problems) + "</span>"
            )
            return
        self.saved.emit(prof.id)
        self.accept()


def _label(name: str, description: str) -> str:
    return f"{name} — {description}" if description and description != name else name


def _esc(s: object) -> str:
    return html.escape(str(s), quote=False)
