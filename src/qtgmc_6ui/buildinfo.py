"""build-info.json reader (WP-B).  The writer lives in packaging/write-build-info.py (WP-G)."""

from __future__ import annotations

from pathlib import Path

from .models import BuildInfo


def load(path: Path | None = None) -> BuildInfo:
    """Read paths.build_info_path() (or ``path``); raise ComponentError(BUILD_INFO_MISSING)."""
    raise NotImplementedError


def load_or_dev(path: Path | None = None) -> BuildInfo:
    """Like load(), but when the file is absent and QTGMC6UI_PLUGIN_DIR is set, synthesise a
    BuildInfo from the running interpreter and the plugin dir contents (development mode)."""
    raise NotImplementedError
