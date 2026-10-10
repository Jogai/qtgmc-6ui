"""Install and user directories.  Everything configurable here is overridable
with an environment variable so the package can run from the working tree
(sandbox tests, development) without being installed.

QTGMC6UI_PLUGIN_DIR   dir with bundled native plugins   (default <install root>/plugins)
QTGMC6UI_BUILD_INFO   build-info.json path              (default <install root>/build-info.json)
QTGMC6UI_CONFIG_DIR   user config dir                   (default <OS config base>/qtgmc-6ui)
QTGMC6UI_CACHE_DIR    user cache dir                    (default <OS cache base>/qtgmc-6ui)

The OS defaults (install root, config/cache bases) come from osdep.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import osdep

APP_ID = osdep.APP_ID
DISPLAY_NAME = "qtgmc-6UI"

def __getattr__(name: str) -> object:  # INSTALL_ROOT, computed per call (HOST may be swapped)
    if name == "INSTALL_ROOT":
        return osdep.install_root()
    raise AttributeError(name)


def plugin_dir() -> Path:
    return Path(os.environ.get("QTGMC6UI_PLUGIN_DIR", osdep.install_root() / "plugins"))


def build_info_path() -> Path:
    return Path(os.environ.get("QTGMC6UI_BUILD_INFO", osdep.install_root() / "build-info.json"))


def config_dir() -> Path:
    if p := os.environ.get("QTGMC6UI_CONFIG_DIR"):
        return Path(p)
    return osdep.config_base() / APP_ID


def cache_dir() -> Path:
    if p := os.environ.get("QTGMC6UI_CACHE_DIR"):
        return Path(p)
    return osdep.cache_base() / APP_ID


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
    """The system VapourSynth plugin dir (repo plugins such as bestsource); None if VS is absent."""
    return osdep.system_vs_plugin_dir()


def ensure_dirs() -> None:
    for d in (profiles_dir(), analysis_cache_dir(), bs_index_dir(), mux_matrix_cache_dir(), preview_cache_dir()):
        d.mkdir(parents=True, exist_ok=True)
