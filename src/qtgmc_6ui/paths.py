"""Install and user directories.  Everything configurable here is overridable
with an environment variable so the package can run from the working tree
(sandbox tests, development) without being installed.

QTGMC6UI_PLUGIN_DIR   dir with bundled native plugins   (default /usr/lib/qtgmc-6ui/plugins)
QTGMC6UI_BUILD_INFO   build-info.json path              (default /usr/lib/qtgmc-6ui/build-info.json)
QTGMC6UI_CONFIG_DIR   user config dir                   (default $XDG_CONFIG_HOME/qtgmc-6ui)
QTGMC6UI_CACHE_DIR    user cache dir                    (default $XDG_CACHE_HOME/qtgmc-6ui)
"""

from __future__ import annotations

import os
from pathlib import Path

APP_ID = "qtgmc-6ui"
DISPLAY_NAME = "qtgmc-6UI"

INSTALL_ROOT = Path("/usr/lib/qtgmc-6ui")


def plugin_dir() -> Path:
    return Path(os.environ.get("QTGMC6UI_PLUGIN_DIR", INSTALL_ROOT / "plugins"))


def build_info_path() -> Path:
    return Path(os.environ.get("QTGMC6UI_BUILD_INFO", INSTALL_ROOT / "build-info.json"))


def config_dir() -> Path:
    if p := os.environ.get("QTGMC6UI_CONFIG_DIR"):
        return Path(p)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_ID


def cache_dir() -> Path:
    if p := os.environ.get("QTGMC6UI_CACHE_DIR"):
        return Path(p)
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / APP_ID


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def analysis_cache_dir() -> Path:
    return cache_dir() / "analysis"


def bs_index_dir() -> Path:
    """bestsource index cache (cachepath=...), keyed by the source filter."""
    return cache_dir() / "bsindex"


def mux_matrix_cache_dir() -> Path:
    return cache_dir() / "muxmatrix"


def preview_cache_dir() -> Path:
    return cache_dir() / "preview"


def system_vs_plugin_dir() -> Path | None:
    """Arch's VapourSynth autoload dir (<site-packages>/vapoursynth/plugins); None if VS is absent."""
    try:
        import vapoursynth  # noqa: F401  (import only; never touches vs.core)
    except Exception:
        return None
    return Path(vapoursynth.__file__).resolve().parent / "plugins"


def ensure_dirs() -> None:
    for d in (profiles_dir(), analysis_cache_dir(), bs_index_dir(), mux_matrix_cache_dir(), preview_cache_dir()):
        d.mkdir(parents=True, exist_ok=True)
