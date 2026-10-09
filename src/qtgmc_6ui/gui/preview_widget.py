"""Frame preview through ``qtgmc-6ui preview-frame`` (PNG); no player, so nothing is auto-deinterlaced."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import paths
from ..models import MasterSettings, RateMode
from .backend import CliCall, CliResult
from .preview_server import PreviewServer
from .queue_model import JobItem

VIEW_SOURCE, VIEW_FIELDS, VIEW_OUTPUT, VIEW_SIDE, VIEW_TOGGLE = "source", "fields", "output", "side", "toggle"
VIEW_LABELS = (
    (VIEW_SOURCE, "Source (raw frame, not deinterlaced)"),
    (VIEW_FIELDS, "Show fields (separated, first field on top)"),
    (VIEW_OUTPUT, "Output (QTGMC)"),
    (VIEW_SIDE, "Side by side: source | output"),
    (VIEW_TOGGLE, "Toggle source / output (Space)"),
)
ZOOMS = (("Fit", 0.0), ("50%", 0.5), ("100%", 1.0), ("200%", 2.0), ("300%", 3.0))
CAPTIONS = {
    "source": "Source frame {n} — raw, interlaced (combing is expected)",
    "fields": "Fields of frame {n} — first field on top, line-doubled",
    "output": "QTGMC output for frame {n}",
}


PREVIEW_CACHE_LIMIT = 500 * 1024 * 1024


def preview_cache() -> Path:
    d = paths.preview_cache_dir() / "gui"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune_preview_cache(limit: int = PREVIEW_CACHE_LIMIT) -> None:
    """Keep the rendered-PNG cache under ``limit`` bytes, dropping the least recently used."""
    try:
        files = [(f.stat(), f) for f in preview_cache().glob("*.png")]
    except OSError:
        return
    total = sum(st.st_size for st, _ in files)
    for st, f in sorted(files, key=lambda x: x[0].st_atime):
        if total <= limit:
            break
        f.unlink(missing_ok=True)
        total -= st.st_size


@dataclass(frozen=True)
class FrameKey:
    input: str
    frame: int
    mode: str  # source | fields | output
    field_order: str
    preset: str
    lossless: bool
    rate: str

    def request(self, out_png: str) -> dict:
        """preview-serve request body (same parameters as ``args``)."""
        r: dict = {"frame": self.frame, "mode": self.mode, "output": out_png}
        if self.field_order:
            r["field_order"] = self.field_order
        if self.mode == "output":
            r.update(rate=self.rate, lossless=self.lossless)
            if self.preset:
                r["preset"] = self.preset
        return r

    def args(self, out_png: str) -> list[str]:
        a = ["preview-frame", self.input, str(self.frame), "--mode", self.mode, "-o", out_png, "--json"]
        if self.field_order:
            a += ["--field-order", self.field_order]
        if self.mode == "output":
            if self.preset:
                a += ["--preset", self.preset]
            if not self.lossless:
                a.append("--no-lossless")
            if self.rate == RateMode.SINGLE.value:
                a += ["--rate", "single"]
        return a

    def cache_path(self) -> str:
        try:
            st = os.stat(self.input)
            stamp = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            stamp = ""
        h = hashlib.sha1(f"{self!r}{stamp}".encode()).hexdigest()[:20]
        return str(preview_cache() / f"{h}.png")


class ImagePane(QWidget):
    """Caption + image label; scaling is nearest-neighbour so combing stays visible."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.caption = QLabel()
        self.caption.setObjectName("paneCaption")
        self.caption.setStyleSheet("font-weight: bold;")
        self.full_caption = ""
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.pixmap: QPixmap | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.caption)
        lay.addWidget(self.image, 1)

    def set_pixmap(self, pm: QPixmap | None, caption: str) -> None:
        self.pixmap = pm
        self.full_caption = caption
        self.caption.setToolTip(caption)
        self.caption.setText(caption)
        if pm is None:
            self.image.clear()

    def apply_zoom(self, zoom: float, avail_w: int, avail_h: int) -> None:
        pm = self.pixmap
        if pm is None or pm.isNull():
            return
        if zoom <= 0:
            factor = min(avail_w / pm.width(), max(avail_h - self.caption.sizeHint().height(), 50) / pm.height())
            factor = max(factor, 0.05)
        else:
            factor = zoom
        size = pm.size() * factor
        scaled = pm if factor == 1.0 else pm.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
        self.image.setPixmap(scaled)
        self.image.setFixedSize(scaled.size())
        width = max(scaled.width(), 200)
        self.caption.setFixedWidth(width)
        self.caption.setText(self.caption.fontMetrics().elidedText(self.full_caption, Qt.TextElideMode.ElideRight, width))


class PreviewWidget(QWidget):
    renderFailed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item: JobItem | None = None
        self.master = MasterSettings()
        self._server: PreviewServer | None = None
        self._use_serve = True
        self._server_failures = 0
        self._rid = 0
        self._inflight: tuple[object, FrameKey] | None = None  # (request id or CliCall, key)
        self._failed: set[FrameKey] = set()
        self._wanted: dict[str, FrameKey | None] = {"a": None, "b": None}
        self._toggle_b = False

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setObjectName("frameSlider")
        self.spin = QSpinBox()
        self.spin.setObjectName("frameSpin")
        self.spin.setKeyboardTracking(False)
        st = self.style()
        self.prev_button = QToolButton(toolTip="Previous frame (,)")
        self.prev_button.setIcon(st.standardIcon(QStyle.StandardPixmap.SP_MediaSeekBackward))
        self.next_button = QToolButton(toolTip="Next frame (.)")
        self.next_button.setIcon(st.standardIcon(QStyle.StandardPixmap.SP_MediaSeekForward))
        self.view_combo = QComboBox()
        self.view_combo.setObjectName("viewCombo")
        for key, label in VIEW_LABELS:
            self.view_combo.addItem(label, key)
        self.zoom_combo = QComboBox()
        for label, z in ZOOMS:
            self.zoom_combo.addItem(label, z)
        self.zoom_combo.setCurrentIndex(2)
        self.toggle_button = QPushButton("Show output")
        self.toggle_button.setObjectName("toggleButton")
        self.toggle_button.clicked.connect(self.toggle)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Frame:"))
        row1.addWidget(self.prev_button)
        row1.addWidget(self.slider, 1)
        row1.addWidget(self.next_button)
        row1.addWidget(self.spin)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("View:"))
        row2.addWidget(self.view_combo, 1)
        row2.addWidget(self.toggle_button)
        row2.addSpacing(12)
        row2.addWidget(QLabel("Zoom:"))
        row2.addWidget(self.zoom_combo)

        self.busy = QWidget()
        bl = QHBoxLayout(self.busy)
        bl.setContentsMargins(0, 0, 0, 0)
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setMaximumWidth(self.fontMetrics().horizontalAdvance("x") * 14)
        bar.setMaximumHeight(self.fontMetrics().height())
        self.busy_label = QLabel()
        bl.addWidget(bar)
        bl.addWidget(self.busy_label)
        bl.addStretch(1)
        self.busy.hide()
        self.info = QLabel(wordWrap=True)
        self.info.setObjectName("previewInfo")
        self.info.setStyleSheet("color: palette(placeholder-text);")
        self.pane_a = ImagePane()
        self.pane_b = ImagePane()
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.pane_a, 0, Qt.AlignmentFlag.AlignTop)
        hl.addWidget(self.pane_b, 0, Qt.AlignmentFlag.AlignTop)
        hl.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("previewScroll")
        self.scroll.setWidget(holder)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.StyledPanel)
        self.placeholder = QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)

        lay = QVBoxLayout(self)
        lay.addLayout(row1)
        lay.addLayout(row2)
        lay.addWidget(self.info)
        lay.addWidget(self.busy)
        lay.addWidget(self.scroll, 1)
        lay.addWidget(self.placeholder, 1)

        self._debounce = QTimer(self, singleShot=True, interval=180)
        self._debounce.timeout.connect(self.render)
        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        self.prev_button.clicked.connect(lambda: self.spin.setValue(self.spin.value() - 1))
        self.next_button.clicked.connect(lambda: self.spin.setValue(self.spin.value() + 1))
        self.view_combo.currentIndexChanged.connect(lambda _: self._view_changed())
        self.zoom_combo.currentIndexChanged.connect(lambda _: self._apply_zoom())
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.toggle)
        QShortcut(QKeySequence(","), self, lambda: self.spin.setValue(self.spin.value() - 1))
        QShortcut(QKeySequence("."), self, lambda: self.spin.setValue(self.spin.value() + 1))
        self.set_item(None)

    # ------------------------------------------------------------------ inputs
    def set_item(self, item: JobItem | None) -> None:
        changed = item is not self.item
        self.item = item
        usable = bool(item and item.analysis)
        for w in (self.slider, self.spin, self.prev_button, self.next_button, self.view_combo, self.zoom_combo, self.toggle_button):
            w.setEnabled(usable)
        self.scroll.setVisible(usable)
        self.placeholder.setVisible(not usable)
        if not usable:
            self.placeholder.setText(
                "Select an analysed file in the queue to preview it." if not item else "Waiting for the analysis of this file…"
            )
            self.pane_a.set_pixmap(None, "")
            self.pane_b.set_pixmap(None, "")
            self._stop_server()
            return
        frames = item.analysis.probe.video.nb_frames or 1
        if changed:
            for w in (self.slider, self.spin):
                w.blockSignals(True)
                w.setRange(0, max(frames - 1, 0))
                w.setValue(min(frames // 2, frames - 1))
                w.blockSignals(False)
        self.render()

    def set_master(self, master: MasterSettings) -> None:
        self.master = master

    def current_frame(self) -> int:
        return self.spin.value()

    def view(self) -> str:
        return self.view_combo.currentData()

    def set_view(self, view: str) -> None:
        self.view_combo.setCurrentIndex(max(self.view_combo.findData(view), 0))

    # ------------------------------------------------------------------ rendering
    def _on_slider(self, v: int) -> None:
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)
        self._debounce.start()

    def _on_spin(self, v: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(v)
        self.slider.blockSignals(False)
        self._debounce.start()

    def _view_changed(self) -> None:
        self._toggle_b = False
        if self.view() == VIEW_SIDE:
            self.zoom_combo.setCurrentIndex(0)  # Fit: both panes visible
        self.render()

    def toggle(self) -> None:
        if self.view() != VIEW_TOGGLE:
            self.set_view(VIEW_TOGGLE)
        self._toggle_b = not self._toggle_b
        self.render()

    def _key(self, mode: str) -> FrameKey | None:
        if not self.item or not self.item.analysis:
            return None
        order = self.item.effective_field_order() or ""
        m = self.master
        return FrameKey(self.item.path, self.current_frame(), mode, order, m.preset.value if m.preset else "",
                        m.lossless, m.rate.value)

    def slots(self) -> list[tuple[str, str]]:
        """(slot, mode) pairs to show for the current view."""
        v = self.view()
        if v == VIEW_SIDE:
            return [("a", "source"), ("b", "output")]
        if v == VIEW_TOGGLE:
            return [("a", "output" if self._toggle_b else "source")]
        return [("a", v)]

    def render(self) -> None:
        if not self.item or not self.item.analysis:
            return
        slots = self.slots()
        self.pane_b.setVisible(len(slots) > 1)
        is_toggle = self.view() == VIEW_TOGGLE
        self.toggle_button.setText("Show source" if is_toggle and self._toggle_b else "Show output")
        notes = ["Rendered from the file itself: no player, no automatic deinterlacing."]
        if not self.item.effective_field_order() and any(m != "source" for _, m in slots):
            notes.insert(0, "Field order unknown: choose TFF or BFF in the Analysis tab (TFF assumed here).")
        if any(m == "output" for _, m in slots):
            notes.append("Output frames are computed with QTGMC around the frame and can take a few seconds.")
        self.info.setText(" ".join(notes))
        for slot, mode in slots:
            key = self._key(mode)
            self._wanted[slot] = key
            if key is None:
                continue
            pm = QPixmap(key.cache_path())
            if not pm.isNull():
                self._show(slot, key, pm)
            else:
                self._pane(slot).caption.setText(CAPTIONS[mode].format(n=key.frame) + " — rendering…")
        self._pump()

    def _pane(self, slot: str) -> ImagePane:
        return self.pane_a if slot == "a" else self.pane_b

    # ------------------------------------------------------------------ backend
    def _missing(self) -> list[tuple[str, FrameKey]]:
        out = []
        for slot, key in self._wanted.items():
            if key is not None and key not in self._failed and QPixmap(key.cache_path()).isNull():
                if slot == "a" or self.pane_b.isVisible():
                    out.append((slot, key))
        return out

    def _pump(self) -> None:
        """Send the next wanted frame; only one request is in flight, stale positions are skipped."""
        if self._inflight is not None or not self.isVisible():
            self._update_busy()
            return
        todo = self._missing()
        if not todo:
            self._update_busy()
            return
        slot, key = todo[0]
        if self._use_serve:
            srv = self._ensure_server()
            if srv is None or not srv.is_ready:
                self._update_busy()
                return  # the server's ``ready`` calls _pump again
            self._rid += 1
            self._inflight = (self._rid, key)
            srv.request(self._rid, key.request(key.cache_path()))
        else:
            call = CliCall(key.args(key.cache_path()), self)
            self._inflight = (call, key)
            call.finished.connect(lambda r: self._on_frame_call(call, key, r))
            call.start()
        self._update_busy()

    def _update_busy(self) -> None:
        busy = self._inflight is not None or (self._server is not None and not self._server.is_ready and bool(self._missing()))
        self.busy.setVisible(busy)
        if busy:
            mode = self._inflight[1].mode if self._inflight else ""
            self.busy_label.setText("Rendering QTGMC output…" if mode == "output" else "Rendering…")

    def _ensure_server(self) -> PreviewServer | None:
        path = self.item.path if self.item else None
        if self._server is not None and self._server.path != path:
            self._stop_server()
        if self._server is None and path:
            self._server = PreviewServer(path, self)
            self._server.ready.connect(lambda _info: self._pump())
            self._server.frameDone.connect(self._on_served)
            self._server.frameFailed.connect(self._on_serve_failed)
            self._server.died.connect(self._on_server_died)
            self._server.start()
        return self._server

    def _stop_server(self) -> None:
        if self._server is not None:
            srv, self._server = self._server, None
            srv.stop()
            srv.proc.finished.connect(lambda _o: srv.deleteLater())
            if self._inflight is not None and not isinstance(self._inflight[0], CliCall):
                self._inflight = None

    def _on_served(self, rid, data: dict) -> None:
        if self._inflight is None or self._inflight[0] != rid:
            return
        key = self._inflight[1]
        self._inflight = None
        self._deliver(key, True, data.get("note") or "", {})
        self._pump()

    def _on_serve_failed(self, rid, err: dict) -> None:
        if self._inflight is None or self._inflight[0] != rid:
            return
        key = self._inflight[1]
        self._inflight = None
        self._deliver(key, False, "", err)
        self._pump()

    def _on_server_died(self, msg: str) -> None:
        self._server_failures += 1
        self._server.deleteLater()
        self._server = None
        if self._inflight is not None and not isinstance(self._inflight[0], CliCall):
            self._inflight = None
        if self._server_failures >= 2:
            self._use_serve = False  # older backend or broken renderer: one process per frame
            self.renderFailed.emit(f"preview renderer stopped ({msg}); using single-frame rendering")
        self._pump()

    def _on_frame_call(self, call: CliCall, key: FrameKey, result: CliResult) -> None:
        call.deleteLater()
        if self._inflight is not None and self._inflight[0] is call:
            self._inflight = None
        note = (result.data or {}).get("note", "") if isinstance(result.data, dict) else ""
        self._deliver(key, result.ok, note, result.error or {})
        self._pump()

    def _deliver(self, key: FrameKey, ok: bool, note: str, err: dict) -> None:
        for slot, wanted in self._wanted.items():
            if wanted != key:
                continue
            if ok:
                pm = QPixmap(key.cache_path())
                if not pm.isNull():
                    self._show(slot, key, pm, note)
            else:
                self._failed.add(key)
                msg = err.get("message", "preview failed")
                hint = err.get("hint") or ""
                self._pane(slot).set_pixmap(None, f"Preview failed: {msg}" + (f" — {hint}" if hint else ""))
                self.renderFailed.emit(str(msg))

    # ------------------------------------------------------------------ display
    def _show(self, slot: str, key: FrameKey, pm: QPixmap, note: str = "") -> None:
        caption = CAPTIONS[key.mode].format(n=key.frame)
        if key.mode == "output" and key.rate == "double":
            if self.view() == VIEW_SIDE and pm.height() % 2 == 0:
                pm = pm.copy(0, 0, pm.width(), pm.height() // 2)  # the frame of the first field
                caption += " (first field; the toggle view shows both)"
            else:
                caption += " (two output frames, stacked)"
        if note:
            caption += f" — {note}"
        self._pane(slot).set_pixmap(pm, caption)
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        z = self.zoom_combo.currentData()
        vp = self.scroll.viewport().size()
        n = 2 if self.pane_b.isVisible() else 1
        for pane in (self.pane_a, self.pane_b)[:n]:
            pane.apply_zoom(z, max(vp.width() // n - 12, 50), vp.height() - 8)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.zoom_combo.currentData() == 0.0:
            self._apply_zoom()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.render()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._stop_server()  # the renderer lives only while the Preview tab is visible

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.kill()
            self._server = None
        if self._inflight is not None and isinstance(self._inflight[0], CliCall):
            self._inflight[0].kill()
        self._inflight = None
