"""GUI entry point (WP-E)."""

from __future__ import annotations

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Create the QApplication (display name qtgmc-6UI), run doctor.startup_check() in a
    subprocess (``qtgmc-6ui doctor --json --no-load``), show the main window."""
    raise NotImplementedError
