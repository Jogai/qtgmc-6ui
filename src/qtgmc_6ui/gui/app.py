"""GUI entry point (WP-E): ``qtgmc-6ui`` / ``qtgmc-6ui gui``."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from .. import __version__, osdep, paths


def _force_utf8_children() -> None:
    """Child processes inherit this environment: keep non-ASCII paths working under LANG=C."""
    osdep.force_utf8_environ(os.environ)


def main(argv: Sequence[str] | None = None) -> int:
    """Create the QApplication (display name qtgmc-6UI), show the main window; the window runs
    ``qtgmc-6ui doctor --json --no-load`` in the background and shows a banner on problems."""
    _force_utf8_children()
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        print(f"error: PySide6 (Qt6) is not available: {e}\nhint: install the 'pyside6' package", file=sys.stderr)
        return 4

    from .icon import app_icon
    from .main_window import MainWindow

    # argv[0] gives the X11 WM_CLASS instance name: keep it "qtgmc-6ui" also for `python -m`
    args = [paths.APP_ID, *(argv or [])]
    QApplication.setDesktopFileName(paths.APP_ID)  # Wayland app_id -> qtgmc-6ui.desktop -> Icon=
    app = QApplication.instance() or QApplication(args)
    app.setApplicationName(paths.APP_ID)
    app.setApplicationDisplayName(paths.DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(paths.APP_ID)
    app.setWindowIcon(app_icon())  # before any window, so every window and dialog inherits it
    win = MainWindow()
    win.show()
    return int(app.exec())
