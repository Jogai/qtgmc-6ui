"""GUI against the real CLI (``python -m qtgmc_6ui``) on the reference clip, inside the sandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pytestqt")
pytestmark = [pytest.mark.gui, pytest.mark.needs_sandbox]

TIMEOUT = 120_000


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch: pytest.MonkeyPatch):
    from qtgmc_6ui.gui.main_window import MainWindow

    monkeypatch.delenv("QTGMC6UI_GUI_BACKEND", raising=False)
    monkeypatch.setenv("QTGMC6UI_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", str(tmp_path / "cache"))
    win = MainWindow(run_startup_checks=False)
    qtbot.addWidget(win)
    win.show()
    yield win
    win.shutdown()


def test_real_analysis_and_source_preview(qtbot, window, reference_clip) -> None:
    (item,) = window.add_paths([str(reference_clip)])
    qtbot.waitUntil(lambda: item.analysis is not None or item.error is not None, timeout=TIMEOUT)
    assert item.analysis, item.error
    assert item.detected_order() == "bff"
    assert item.analysis.probe.video.nb_frames == 274
    window.queue_panel.select(item)
    assert "Interlaced BFF" in window.analysis_panel.callout.title.text()
    window.tabs.setCurrentIndex(3)
    pv = window.preview
    qtbot.waitUntil(lambda: pv.pane_a.pixmap is not None or "failed" in pv.pane_a.caption.text(), timeout=TIMEOUT)
    assert pv.pane_a.pixmap is not None, pv.pane_a.caption.text()
    assert (pv.pane_a.pixmap.width(), pv.pane_a.pixmap.height()) == (720, 576)
    assert pv._server is not None and pv._server.is_ready  # scrubbing uses preview-serve


def test_real_profile_editor_matrix(qtbot, window) -> None:
    window.catalog.load_profiles()
    qtbot.waitUntil(lambda: bool(window.catalog.profiles), timeout=TIMEOUT)
    assert {"mp4-h264-aac", "mkv-hevc10-opus", "mkv-h264-flac"} <= {p.id for p in window.catalog.profiles}
    dlg = window.view_section.open_editor(None)
    qtbot.waitUntil(lambda: bool(window.catalog.encoders), timeout=TIMEOUT)
    dlg.container_combo.select_data("mov")
    qtbot.waitUntil(lambda: "mov" in window.catalog.probes and not window.catalog.probes_pending("mov"), timeout=TIMEOUT)
    assert dlg.ext_edit.text() == "mov"
    combo = dlg.aenc_combo
    row = combo.findData("flac")
    assert row >= 0 and not combo.is_enabled_row(row), "mov must reject FLAC"
    assert combo.reason(row)  # the backend's reason text
    assert combo.is_enabled_row(combo.findData("aac"))
    dlg.name_edit.setText("GUI test MOV")
    dlg.id_edit.setText("gui-test-mov")
    with qtbot.waitSignal(dlg.saved, timeout=TIMEOUT):
        dlg.save()
    qtbot.waitUntil(lambda: window.catalog.profile("gui-test-mov") is not None, timeout=TIMEOUT)
    prof = window.catalog.profile("gui-test-mov")
    assert prof.extension == "mov" and prof.path and Path(prof.path).is_file()
    done = []
    window.catalog.delete_profile("gui-test-mov", lambda ok, msg: done.append(ok))
    qtbot.waitUntil(lambda: bool(done), timeout=TIMEOUT)
    assert done == [True] and not Path(prof.path).exists()


@pytest.mark.needs_qtgmc
def test_real_cancel_removes_partial(qtbot, window, reference_clip, tmp_path) -> None:
    (item,) = window.add_paths([str(reference_clip)])
    qtbot.waitUntil(lambda: item.analysis is not None or item.error is not None, timeout=TIMEOUT)
    window.settings_panel.out_dir.setText(str(tmp_path))
    window.settings_panel.preset_combo.setCurrentIndex(window.settings_panel.preset_combo.findData("draft"))
    window.start_queue()
    qtbot.waitUntil(lambda: any(tmp_path.glob("*.mkv*")) or item.status.value != "running", timeout=TIMEOUT)
    with qtbot.waitSignal(window.controller.batchFinished, timeout=TIMEOUT):
        window.controller.cancel()
    assert item.status.value == "cancelled" and item.error is None, item.error
    assert not list(tmp_path.glob("*.mkv*"))


@pytest.mark.needs_qtgmc
@pytest.mark.slow
def test_real_no_master_job(qtbot, window, reference_clip, tmp_path) -> None:
    from PySide6.QtCore import Qt

    window.catalog.load_profiles()
    qtbot.waitUntil(lambda: window.view_section.list.count() > 0, timeout=TIMEOUT)
    lst = window.view_section.list
    for r in range(lst.count()):
        if lst.item(r).data(Qt.ItemDataRole.UserRole) == "mp4-h264-aac":
            lst.item(r).setCheckState(Qt.CheckState.Checked)
    out = tmp_path / "out"
    out.mkdir()
    sp = window.settings_panel
    sp.keep_master.setChecked(False)
    sp.out_dir.setText(str(out))
    sp.preset_combo.setCurrentIndex(sp.preset_combo.findData("draft"))
    (item,) = window.add_paths([str(reference_clip)])
    qtbot.waitUntil(lambda: item.analysis is not None or item.error is not None, timeout=TIMEOUT)
    with qtbot.waitSignal(window.controller.batchFinished, timeout=600_000):
        window.start_queue()
    assert item.status.value == "done", item.error
    names = sorted(p.name for p in out.iterdir())
    assert names == [f"{reference_clip.stem}.deint.mp4-h264-aac.mp4"], names  # no master, no hidden temp
    assert [o["kind"] for o in item.outputs] == ["view"]


def test_real_audio_modes(qtbot, window) -> None:
    window.load_audio_modes()
    qtbot.waitUntil(lambda: window._audio_call is not None and window._audio_call.proc.state().name == "NotRunning",
                    timeout=TIMEOUT)
    qtbot.waitUntil(lambda: window.settings_panel.audio_combo.count() > 0, timeout=10_000)
    combo = window.settings_panel.audio_combo
    assert [combo.itemData(i) for i in range(combo.count())] == ["copy", "pcm", "flac", "alac", "aac", "opus"]
    assert combo.currentIndex() == 0
    labels = [combo.itemText(i).lower() for i in range(combo.count())]
    assert not any(w in t for t in labels for w in ("best", "worst", "default"))
