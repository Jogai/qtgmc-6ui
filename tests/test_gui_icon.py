"""Application/window icon: bundled resources, inheritance by windows and dialogs, wheel packaging."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("pytestqt")
pytestmark = pytest.mark.gui

GUI_ROOT = Path(__file__).resolve().parents[1]
GRUVBOX_BG = ((0x1D, 0x20, 0x21), (0x32, 0x30, 0x2F))  # gradient ends of the icon background


def _is_gruvbox_bg(r: int, g: int, b: int) -> bool:
    lo, hi = GRUVBOX_BG
    return all(min(a, c) - 3 <= v <= max(a, c) + 3 for v, a, c in zip((r, g, b), lo, hi, strict=True))


def _assert_icon_renders(icon, size: int = 32) -> None:
    pm = icon.pixmap(size, size)
    assert not pm.isNull()
    img = pm.toImage()
    centre_ish = img.pixelColor(size // 2, int(size * 0.9))  # bottom margin: background, below the glyph
    assert centre_ish.alpha() == 255
    assert _is_gruvbox_bg(centre_ish.red(), centre_ish.green(), centre_ish.blue()), centre_ish.name()
    assert img.pixelColor(0, 0).alpha() == 0  # transparent corner of the rounded square


def test_bundled_icon_without_svg_plugin(qtbot, monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui.gui import icon

    monkeypatch.setattr(icon, "svg_supported", lambda: False)  # as on Arch without qt6-svg
    ic = icon.bundled_icon()
    assert not ic.isNull()
    for size in (16, 32, 64):
        _assert_icon_renders(ic, size)


def test_app_icon_set_before_windows(qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QApplication

    from qtgmc_6ui.gui.icon import app_icon
    from qtgmc_6ui.gui.main_window import MainWindow

    monkeypatch.setenv("QTGMC6UI_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", str(tmp_path / "cache"))
    app = QApplication.instance()
    QApplication.setDesktopFileName("qtgmc-6ui")
    app.setWindowIcon(app_icon())
    assert not app.windowIcon().isNull()
    _assert_icon_renders(app.windowIcon())
    assert QApplication.desktopFileName() == "qtgmc-6ui"
    win = MainWindow(run_startup_checks=False)
    qtbot.addWidget(win)
    assert not win.windowIcon().isNull()
    _assert_icon_renders(win.windowIcon())
    dlg = win.view_section.open_editor(None)
    _assert_icon_renders(dlg.windowIcon())
    dlg.reject()
    win.shutdown()


def test_desktop_file_matches_app_id() -> None:
    desktop = (GUI_ROOT / "packaging" / "qtgmc-6ui.desktop").read_text(encoding="utf-8")
    assert "Icon=qtgmc-6ui" in desktop.splitlines()


@pytest.mark.slow
def test_wheel_contains_icon_resources(tmp_path: Path) -> None:
    pytest.importorskip("build")
    out = tmp_path / "dist"
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "-o", str(out), str(GUI_ROOT)],
                   check=True, capture_output=True)
    (whl,) = out.glob("*.whl")
    names = zipfile.ZipFile(whl).namelist()
    assert "qtgmc_6ui/gui/resources/qtgmc-6ui.svg" in names
    assert all(f"qtgmc_6ui/gui/resources/qtgmc-6ui-{s}.png" in names for s in (16, 32, 256))
    site = tmp_path / "site"
    zipfile.ZipFile(whl).extractall(site)
    code = ("from PySide6.QtWidgets import QApplication; a = QApplication([]); "
            "import qtgmc_6ui.gui.icon as i; assert i.__file__.startswith(" + repr(str(site)) + "), i.__file__; "
            "ic = i.app_icon(); assert i.has_opaque_pixels(ic); print('ok')")
    env = dict(os.environ, PYTHONPATH=str(site), QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, shlex.join([r.stdout, r.stderr])
