"""GUI entry point (WP-E): ``qtgmc-6ui`` / ``qtgmc-6ui gui``."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from .. import __version__, paths


def _force_utf8_children() -> None:
    """Child processes inherit this environment: keep non-ASCII paths working under LANG=C."""
    os.environ.setdefault("PYTHONUTF8", "1")
    loc = os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE") or os.environ.get("LANG") or ""
    if "utf" not in loc.lower().replace("-", ""):
        os.environ["LC_CTYPE"] = "C.UTF-8"


def main(argv: Sequence[str] | None = None) -> int:
    """Create the QApplication (display name qtgmc-6UI), show the main window; the window runs
    ``qtgmc-6ui doctor --json --no-load`` in the background and shows a banner on problems."""
    _force_utf8_children()
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        print(f"error: PySide6 (Qt6) is not available: {e}\nhint: install the 'pyside6' package", file=sys.stderr)
        return 4

    from .main_window import MainWindow

    args = [sys.argv[0] if sys.argv else paths.APP_ID, *(argv or [])]
    app = QApplication.instance() or QApplication(args)
    app.setApplicationName(paths.APP_ID)
    app.setApplicationDisplayName(paths.DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(paths.APP_ID)
    app.setDesktopFileName(paths.APP_ID)
    win = MainWindow()
    win.show()
    return int(app.exec())
