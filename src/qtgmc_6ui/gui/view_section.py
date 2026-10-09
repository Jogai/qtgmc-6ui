"""Viewing copies: pick profiles for the queue; create, edit, duplicate and delete profiles."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import Profile, ScaleMode, VideoBackend
from . import ui_state
from .catalog import Catalog
from .profile_editor import ProfileEditor

SELECTED_KEY = "views/selected"


def describe(p: Profile) -> str:
    v = p.video
    venc = v.encoder if v.backend == VideoBackend.FFMPEG else f"{v.backend.value} (CLI)"
    q = {"crf": f"CRF {v.quality:g}", "qp": f"QP {v.quality:g}", "cq": f"CQ {v.quality:g}",
         "bitrate": f"{v.bitrate_kbps} kbit/s", "lossless": "lossless"}.get(v.rate_control.value, "")
    scale = {ScaleMode.KEEP: "anamorphic", ScaleMode.SQUARE: "square pixels", ScaleMode.CUSTOM: f"{v.scale.width}×{v.scale.height}"}[v.scale.mode]
    a = p.audio
    aud = a.encoder + (f" {a.bitrate_kbps}k" if a.bitrate_kbps else "")
    rate = ", single rate" if v.rate.value == "single" else ""
    return f".{p.extension} · {venc} {q}{' ' + v.pix_fmt if v.pix_fmt else ''} · {aud} · {scale}{rate}"


class ViewSection(QWidget):
    selectionChanged = Signal()

    def __init__(self, catalog: Catalog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.editor: ProfileEditor | None = None
        intro = QLabel(
            "Viewing copies are encoded <b>from the archival master</b> after it is finished. "
            "Tick the profiles to create for every job in the queue."
        )
        intro.setWordWrap(True)
        self.list = QListWidget()
        self.list.setObjectName("profileList")
        self.list.setAlternatingRowColors(True)
        self.list.setSpacing(2)
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.list.itemDoubleClicked.connect(lambda _: self.edit_current())
        self.status = QLabel(wordWrap=True)
        self.status.setStyleSheet("color: palette(placeholder-text);")

        self.new_button = QPushButton("New…")
        self.new_button.setObjectName("newProfileButton")
        self.edit_button = QPushButton("Edit…")
        self.edit_button.setObjectName("editProfileButton")
        self.dup_button = QPushButton("Duplicate…")
        self.delete_button = QPushButton("Delete")
        self.refresh_button = QPushButton("Reload")
        self.new_button.clicked.connect(lambda: self.open_editor(None))
        self.edit_button.clicked.connect(self.edit_current)
        self.dup_button.clicked.connect(self.duplicate_current)
        self.delete_button.clicked.connect(self.delete_current)
        self.refresh_button.clicked.connect(self.catalog.load_profiles)

        buttons = QHBoxLayout()
        for b in (self.new_button, self.edit_button, self.dup_button, self.delete_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(self.refresh_button)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addLayout(buttons)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.status)
        self._selected = set(ui_state.get_list(SELECTED_KEY))
        catalog.profilesChanged.connect(self._fill)
        self._update_buttons()

    # ------------------------------------------------------------------ list
    def _fill(self) -> None:
        cur = self.current_profile()
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.catalog.profiles:
            tag = "built-in" if p.builtin else "custom"
            it = QListWidgetItem(f"{p.name}   [{tag}]\n{describe(p)}")
            it.setData(Qt.ItemDataRole.UserRole, p.id)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if p.id in self._selected else Qt.CheckState.Unchecked)
            it.setToolTip((p.description + "\n" if p.description else "") + (p.path or "built-in profile"))
            self.list.addItem(it)
            if cur and cur.id == p.id:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        known = {p.id for p in self.catalog.profiles}
        if self.catalog.profiles_error:
            self.status.setText(f"Warning: {self.catalog.profiles_error}")
        else:
            missing = sorted(self._selected - known)
            self.status.setText(f"Previously selected profile(s) not found: {', '.join(missing)}" if missing else "")
        self._selected &= known or self._selected
        self._update_buttons()
        self.selectionChanged.emit()

    def selected_ids(self) -> list[str]:
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def set_selected(self, ids: list[str]) -> None:
        self._selected = set(ids)
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(Qt.CheckState.Checked if it.data(Qt.ItemDataRole.UserRole) in self._selected else Qt.CheckState.Unchecked)

    def _on_item_changed(self, _it: QListWidgetItem) -> None:
        self._selected = set(self.selected_ids())
        ui_state.put(SELECTED_KEY, sorted(self._selected))
        self.selectionChanged.emit()

    def current_profile(self) -> Profile | None:
        it = self.list.currentItem()
        return self.catalog.profile(it.data(Qt.ItemDataRole.UserRole)) if it else None

    def _update_buttons(self) -> None:
        p = self.current_profile()
        self.edit_button.setEnabled(p is not None)
        self.edit_button.setText("Customize…" if p and p.builtin else "Edit…")
        self.edit_button.setToolTip("Built-in profiles are read-only: this creates an editable copy." if p and p.builtin else "")
        self.dup_button.setEnabled(p is not None)
        self.delete_button.setEnabled(bool(p and not p.builtin))

    # ------------------------------------------------------------------ actions
    def open_editor(self, profile: Profile | None) -> ProfileEditor:
        self.editor = ProfileEditor(self.catalog, profile, self)
        self.editor.saved.connect(self._after_save)
        self.editor.setModal(True)
        self.editor.show()
        return self.editor

    def edit_current(self) -> None:
        if p := self.current_profile():
            self.open_editor(p)

    def duplicate_current(self) -> None:
        if p := self.current_profile():
            dup = Profile.from_dict(p.to_dict())
            dup.builtin = True  # editor treats it as a template -> new id/name
            self.open_editor(dup)

    def delete_current(self) -> None:
        p = self.current_profile()
        if not p or p.builtin:
            return
        if QMessageBox.question(self, "Delete profile", f"Delete the profile “{p.name}”?\n{p.path}") != QMessageBox.StandardButton.Yes:
            return
        self.delete_button.setEnabled(False)
        self.catalog.delete_profile(p.id, lambda ok, msg: self._after_delete(p, ok, msg))

    def _after_delete(self, p: Profile, ok: bool, msg: str) -> None:
        if not ok:
            QMessageBox.warning(self, "Delete profile", msg)
        else:
            self._selected.discard(p.id)
            ui_state.put(SELECTED_KEY, sorted(self._selected))
        self.catalog.load_profiles()

    def _after_save(self, pid: str) -> None:
        self._selected.add(pid)
        ui_state.put(SELECTED_KEY, sorted(self._selected))
        self.catalog.load_profiles()
