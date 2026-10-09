"""Offscreen GUI smoke tests against tests/fake_backend.py (brief §11: launches, loads a file,
starts and cancels a job, opens the profile editor; plus preview, override marking, IVTC notice,
doctor rendering).  Run: QT_QPA_PLATFORM=offscreen pytest -m gui"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

pytest.importorskip("pytestqt")
pytestmark = pytest.mark.gui

FAKE = Path(__file__).with_name("fake_backend.py")
TIMEOUT = 15000


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    monkeypatch.setenv("QTGMC6UI_GUI_BACKEND", shlex.join([sys.executable, str(FAKE)]))
    monkeypatch.setenv("QTGMC6UI_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FAKE_QTGMC_ARGV_LOG", str(tmp_path / "argv.log"))
    clips = tmp_path / "clips ü ビデオ"
    clips.mkdir()
    for name in ("ref_bff été.avi", "film_tele.avi", "static_undet.avi", "pal_tff.avi"):
        (clips / name).write_bytes(b"x")
    return clips


@pytest.fixture
def window(qtbot, env: Path):
    from qtgmc_6ui.gui.main_window import MainWindow

    win = MainWindow(run_startup_checks=False)
    qtbot.addWidget(win)
    win.resize(1280, 720)
    win.show()
    yield win
    win.shutdown()


def add_and_wait(qtbot, win, paths: list[Path]):
    items = win.add_paths([str(p) for p in paths])
    qtbot.waitUntil(lambda: all(i.analysis or i.error for i in items), timeout=TIMEOUT)
    return items


def test_launch_and_doctor_banner(qtbot, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui.gui.main_window import MainWindow

    monkeypatch.setenv("FAKE_QTGMC_DOCTOR", "fail")
    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(lambda: win.doctor_result is not None, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: bool(win.catalog.profiles), timeout=TIMEOUT)
    assert win.banner.isVisible()
    assert "reinstall/rebuild qtgmc-6ui" in win.banner.label.text()
    dlg = win.show_doctor()
    assert dlg.tree.topLevelItemCount() == 3
    assert dlg.tree.topLevelItem(1).text(1) == "FAIL"
    assert "Problems found" in dlg.summary.text()
    win.shutdown()


def test_missing_backend_is_reported(qtbot, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui.gui.main_window import MainWindow

    monkeypatch.setenv("QTGMC6UI_GUI_BACKEND", "/nonexistent/qtgmc-6ui-backend")
    win = MainWindow()
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: win.doctor_result is not None, timeout=TIMEOUT)
    assert win.banner.isVisibleTo(win)
    assert "cannot check its components" in win.banner.label.text()
    win.shutdown()


def test_load_file_fills_analysis_panel(qtbot, window, env: Path) -> None:
    (item,) = add_and_wait(qtbot, window, [env / "ref_bff été.avi"])
    window.queue_panel.select(item)
    panel = window.analysis_panel
    assert item.analysis.probe.video.width == 720
    assert item.status.value == "ready"
    assert "Interlaced BFF" in panel.callout.title.text()
    assert panel.order_combo.currentText() == "Detected: BFF"
    assert not panel.override_badge.isVisibleTo(panel)
    assert panel.source_form.rowCount() >= 6
    assert "été" in item.path  # non-ASCII path survived the CLI round trip


def test_telecined_explanation_and_force(qtbot, window, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    (item,) = add_and_wait(qtbot, window, [env / "film_tele.avi"])
    window.queue_panel.select(item)
    panel = window.analysis_panel
    assert item.status.value == "needs_decision"
    assert "inverse telecine (IVTC)" in panel.callout.body.text()
    assert panel.force_check.isVisibleTo(panel)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Yes)
    panel.force_check.setChecked(True)
    assert item.force
    assert "Override active" in panel.force_note.text()
    panel.order_combo.setCurrentIndex(1)  # TFF
    assert item.status.value == "ready"


def test_override_is_visibly_marked(qtbot, window, env: Path) -> None:
    (item,) = add_and_wait(qtbot, window, [env / "static_undet.avi"])
    window.queue_panel.select(item)
    panel = window.analysis_panel
    assert item.status.value == "needs_decision"
    panel.order_combo.setCurrentIndex(2)  # BFF
    assert item.field_order_override == "bff"
    assert panel.override_badge.isVisibleTo(panel) and panel.override_badge.text().strip() == "OVERRIDE"
    assert window.model.data(window.model.index(0, 1)) == "BFF (override)"
    assert item.status.value == "ready"


def test_queue_reorder_remove(qtbot, window, env: Path) -> None:
    items = add_and_wait(qtbot, window, [env / "pal_tff.avi", env / "ref_bff été.avi", env / "film_tele.avi"])
    qp = window.queue_panel
    qp.select(items[2])
    qp.move_current(-1)
    assert [i.name for i in window.model.items] == ["pal_tff.avi", "film_tele.avi", "ref_bff été.avi"]
    qp.select(items[0])
    qp.remove_selected()
    assert [i.name for i in window.model.items] == ["film_tele.avi", "ref_bff été.avi"]


def test_start_and_cancel_job(qtbot, window, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_QTGMC_DELAY", "0.05")
    monkeypatch.setenv("FAKE_QTGMC_CANCEL_DELAY", "1.0")  # a slow in-flight frame
    (item,) = add_and_wait(qtbot, window, [env / "ref_bff été.avi"])
    window.start_queue()
    assert window.controller.running and window.run_panel.cancel_button.isEnabled()
    qtbot.waitUntil(lambda: item.progress is not None and item.progress > 0.4, timeout=TIMEOUT)
    assert "frames" in window.run_panel.stats_label.text()
    partial = env / "ref_bff été.deint.mkv"
    assert partial.exists()
    window.run_panel.cancel_button.click()
    # not "Cancelled" until the backend confirms the cleanup
    assert item.status.value == "running"
    assert "Cancelling" in window.run_panel.stage_label.text()
    assert window.model.data(window.model.index(0, 2)) == "Cancelling…"
    assert not window.run_panel.cancel_button.isEnabled()
    with qtbot.waitSignal(window.controller.batchFinished, timeout=TIMEOUT):
        pass
    assert item.status.value == "cancelled" and item.error is None
    assert not partial.exists()  # the CLI removed the partial output
    assert "removed partial output" in window.run_panel.log.text()
    assert window.run_panel.start_button.isEnabled()


def test_cancel_unconfirmed_is_reported(qtbot, window, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui.gui import backend

    monkeypatch.setattr(backend, "CANCEL_TERMINATE_MS", 300)
    monkeypatch.setattr(backend, "CANCEL_KILL_MS", 600)
    monkeypatch.setenv("FAKE_QTGMC_DELAY", "0.05")
    monkeypatch.setenv("FAKE_QTGMC_CANCEL_DELAY", "30")  # backend never finishes its cleanup
    (item,) = add_and_wait(qtbot, window, [env / "ref_bff été.avi"])
    window.start_queue()
    qtbot.waitUntil(lambda: item.progress is not None and item.progress > 0.4, timeout=TIMEOUT)
    with qtbot.waitSignal(window.controller.batchFinished, timeout=TIMEOUT):
        window.run_panel.cancel_button.click()
    assert item.status.value == "cancelled"
    assert item.error and item.error["code"] == "cancel_unconfirmed"
    assert str(env / "ref_bff été.deint.mkv") in item.error["hint"]  # honest: the partial is still there
    assert window.run_panel.error_frame.isVisibleTo(window.run_panel)
    assert "killing it" in window.run_panel.log.text()


def test_close_waits_for_cleanup(qtbot, window, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setenv("FAKE_QTGMC_DELAY", "0.05")
    monkeypatch.setenv("FAKE_QTGMC_CANCEL_DELAY", "0.5")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    (item,) = add_and_wait(qtbot, window, [env / "ref_bff été.avi"])
    window.start_queue()
    qtbot.waitUntil(lambda: (env / "ref_bff été.deint.mkv").exists(), timeout=TIMEOUT)
    window.close()
    assert window.isVisible()  # still cleaning up
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=TIMEOUT)
    assert item.status.value == "cancelled"
    assert not (env / "ref_bff été.deint.mkv").exists()


def test_duplicate_master_names_refused(qtbot, window, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    other = env / "sub"
    other.mkdir()
    (other / "pal_tff.avi").write_bytes(b"x")
    add_and_wait(qtbot, window, [env / "pal_tff.avi", other / "pal_tff.avi"])
    window.settings_panel.out_dir.setText(str(env))
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    window.start_queue()
    assert not window.controller.running
    assert shown and "pal_tff.deint.*" in shown[0]


def test_run_job_success_writes_jobspec(qtbot, window, env: Path) -> None:
    import json

    (item,) = add_and_wait(qtbot, window, [env / "pal_tff.avi"])
    window.settings_panel.rate_single.setChecked(True)
    with qtbot.waitSignal(window.controller.batchFinished, timeout=TIMEOUT):
        window.start_queue()
    assert item.status.value == "done"
    assert (env / "pal_tff.deint.mkv").exists()
    argv = [json.loads(x) for x in (env.parent / "argv.log").read_text(encoding="utf-8").splitlines()]
    assert any(a[0] == "run-job" and "--events" in a for a in argv)


def test_job_error_is_actionable(qtbot, window, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_QTGMC_FAIL", "plugin_load_failed")
    (item,) = add_and_wait(qtbot, window, [env / "pal_tff.avi"])
    with qtbot.waitSignal(window.controller.batchFinished, timeout=TIMEOUT):
        window.start_queue()
    assert item.status.value == "failed"
    rp = window.run_panel
    assert rp.error_frame.isVisibleTo(rp)
    assert "What to do" in rp.error_label.text()
    assert rp.doctor_button.isVisibleTo(rp)  # component error -> offer doctor


def test_profile_editor_greys_out_invalid(qtbot, window) -> None:
    window.catalog.load_profiles()
    qtbot.waitUntil(lambda: bool(window.catalog.profiles), timeout=TIMEOUT)
    assert window.view_section.list.count() == 3
    dlg = window.view_section.open_editor(None)
    qtbot.waitUntil(lambda: bool(window.catalog.encoders), timeout=TIMEOUT)
    assert dlg.ext_edit.text() == "mkv"  # from the backend's muxer data
    dlg.container_combo.select_data("mov")
    qtbot.waitUntil(lambda: "mov" in window.catalog.probes and not window.catalog.probes_pending("mov"), timeout=TIMEOUT)
    assert dlg.ext_edit.text() == "mov"
    combo = dlg.aenc_combo
    rows = {combo.itemData(r): r for r in range(combo.count())}
    assert not combo.is_enabled_row(rows["flac"])
    assert combo.reason(rows["flac"]) == "mov does not accept flac: Could not write header"  # backend text
    assert combo.is_enabled_row(rows["aac"])
    # standalone CLI encoders are only offered when the tool is installed
    assert dlg.venc_combo.findData("x264") >= 0
    dlg.reject()


def test_profile_editor_tests_unprobed_pairs_async(qtbot, window, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_QTGMC_UNPROBED", "mov")
    dlg = window.view_section.open_editor(None)
    qtbot.waitUntil(lambda: bool(window.catalog.encoders), timeout=TIMEOUT)
    dlg.container_combo.select_data("mov")
    qtbot.waitUntil(lambda: dlg.busy.isVisibleTo(dlg), timeout=TIMEOUT)  # spinner, dialog stays usable
    assert dlg.name_edit.isEnabled()
    qtbot.waitUntil(lambda: not window.catalog.probes_pending("mov"), timeout=TIMEOUT)
    assert not dlg.busy.isVisibleTo(dlg)
    assert not dlg.aenc_combo.is_enabled_row(dlg.aenc_combo.findData("flac"))
    dlg.reject()


def test_profile_editor_hides_missing_tools(qtbot, window, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_QTGMC_TOOLS", "")
    dlg = window.view_section.open_editor(None)
    qtbot.waitUntil(lambda: bool(window.catalog.encoders), timeout=TIMEOUT)
    assert dlg.venc_combo.findData("x264") < 0
    assert not dlg.sox_box.isVisibleTo(dlg) and dlg.sox_missing.isVisibleTo(dlg)
    dlg.reject()


def test_profile_save_and_delete(qtbot, window, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    window.catalog.load_profiles()
    qtbot.waitUntil(lambda: bool(window.catalog.profiles) and bool(window.catalog.profiles_dir), timeout=TIMEOUT)
    dlg = window.view_section.open_editor(None)
    qtbot.waitUntil(lambda: bool(window.catalog.encoders), timeout=TIMEOUT)
    dlg.name_edit.setText("Small MKV")
    dlg.id_edit.setText("small-mkv")
    with qtbot.waitSignal(dlg.saved, timeout=TIMEOUT):
        dlg.save()
    qtbot.waitUntil(lambda: window.catalog.profile("small-mkv") is not None, timeout=TIMEOUT)
    assert "small-mkv" in window.view_section.selected_ids()
    prof = window.catalog.profile("small-mkv")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    for r in range(window.view_section.list.count()):
        if window.view_section.list.item(r).data(Qt.ItemDataRole.UserRole) == "small-mkv":
            window.view_section.list.setCurrentRow(r)
    window.view_section.delete_current()
    qtbot.waitUntil(lambda: window.catalog.profile("small-mkv") is None, timeout=TIMEOUT)
    assert not Path(prof.path).exists()
    argv = (Path(window.catalog.profiles_dir).parent.parent / "argv.log").read_text(encoding="utf-8")
    assert '"profiles", "save", "-"' in argv and '"profiles", "delete", "small-mkv"' in argv


def test_preview_shows_png(qtbot, window, env: Path) -> None:
    (item,) = add_and_wait(qtbot, window, [env / "ref_bff été.avi"])
    window.queue_panel.select(item)
    window.tabs.setCurrentIndex(3)
    pv = window.preview
    qtbot.waitUntil(lambda: pv.pane_a.pixmap is not None, timeout=TIMEOUT)
    assert pv.pane_a.pixmap.width() == 720
    assert "raw" in pv.pane_a.full_caption
    pv.set_view("fields")
    qtbot.waitUntil(lambda: pv.pane_a.pixmap is not None and pv.pane_a.pixmap.height() == 1152, timeout=TIMEOUT)
    pv.set_view("side")
    qtbot.waitUntil(lambda: pv.pane_b.pixmap is not None, timeout=TIMEOUT)
    assert pv.pane_b.isVisibleTo(pv)
    assert pv.zoom_combo.currentText() == "Fit"
    assert pv.pane_b.pixmap.height() == 576  # side by side: the output frame of the first field
    pv.toggle()
    assert pv.view() == "toggle"
    for _ in range(5):  # scrubbing goes through one long-lived preview-serve process
        pv.spin.setValue(pv.spin.value() + 1)
    qtbot.waitUntil(lambda: pv._inflight is None and not pv.busy.isVisibleTo(pv), timeout=TIMEOUT)
    argv = (env.parent / "argv.log").read_text(encoding="utf-8")
    assert argv.count('"preview-serve"') == 1 and '"preview-frame"' not in argv


def test_preview_falls_back_without_serve(qtbot, window, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_QTGMC_SERVE", "broken")
    (item,) = add_and_wait(qtbot, window, [env / "ref_bff été.avi"])
    window.queue_panel.select(item)
    window.tabs.setCurrentIndex(3)
    pv = window.preview
    qtbot.waitUntil(lambda: pv.pane_a.pixmap is not None, timeout=TIMEOUT)
    assert '"preview-frame"' in (env.parent / "argv.log").read_text(encoding="utf-8")


def _tick_profile(window, pid: str) -> None:
    from PySide6.QtCore import Qt

    lst = window.view_section.list
    for r in range(lst.count()):
        if lst.item(r).data(Qt.ItemDataRole.UserRole) == pid:
            lst.item(r).setCheckState(Qt.CheckState.Checked)


def test_no_master_requires_a_viewing_copy(qtbot, window, env: Path) -> None:
    window.catalog.load_profiles()
    qtbot.waitUntil(lambda: window.view_section.list.count() == 3, timeout=TIMEOUT)
    sp, rp = window.settings_panel, window.run_panel
    assert sp.keep_master.isChecked()  # default on
    assert "4×" in sp.keep_master_hint.text()
    sp.keep_master.setChecked(False)
    assert not rp.start_button.isEnabled()
    assert "at least one viewing copy" in sp.keep_master_hint.text()
    assert "Cannot start" in rp.stage_label.text()
    _tick_profile(window, "mp4-h264-aac")
    assert rp.start_button.isEnabled()
    assert "No archival master" in sp.keep_master_hint.text()


def test_no_master_job_produces_views_only(qtbot, window, env: Path) -> None:
    import json

    window.catalog.load_profiles()
    qtbot.waitUntil(lambda: window.view_section.list.count() == 3, timeout=TIMEOUT)
    _tick_profile(window, "mp4-h264-aac")
    window.settings_panel.keep_master.setChecked(False)
    (item,) = add_and_wait(qtbot, window, [env / "pal_tff.avi"])
    seen_stages = []
    window.controller.runUpdated.connect(lambda it, st: seen_stages.append(st.stage_text()))
    with qtbot.waitSignal(window.controller.batchFinished, timeout=TIMEOUT):
        window.start_queue()
    assert item.status.value == "done"
    assert window.model.data(window.model.index(0, 2)) == "Done (viewing copies only)"
    assert any("Temporary master" in s for s in seen_stages)
    assert "no archival master kept" in window.run_panel.stage_label.text()
    assert (env / "pal_tff.deint.mp4-h264-aac.mp4").exists()
    assert not (env / "pal_tff.deint.mkv").exists()
    assert not list(env.glob(".*nomaster*"))  # temporary master removed
    assert [o["kind"] for o in item.outputs] == ["view"]
    jobs = [json.loads(x) for x in (env.parent / "argv.log").read_text(encoding="utf-8").splitlines()]
    assert any(a[0] == "run-job" for a in jobs)
