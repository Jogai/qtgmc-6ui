"""VapourSynth environment (WP-B): autoload disabled, explicit plugin loading, ABI checks.

Rules (verified in the sandbox, see plans/architecture.md §1.2/§1.4):
  * ``vapoursynth`` is imported lazily and ``vs.core`` is never touched before
    ``open_core()`` registered an EnvironmentPolicy whose environment was created
    with ``vs.DISABLE_AUTO_LOADING`` (core.flags == 2, plugins == std/resize/text).
  * Repo plugins (bestsource) are loaded from paths.system_vs_plugin_dir();
    bundled ones from paths.plugin_dir() in the order of build-info.json.
  * ``LoadPlugin`` errors are raised (not swallowed like autoload) and turned
    into ComponentError(PLUGIN_LOAD_FAILED / PLUGIN_CONFLICT).
  * ``destroy_environment`` is called on close() to avoid the R80 RuntimeWarning.
  * Only one VSCore per process; the GUI never creates one (it uses the CLI).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import BuildInfo, PluginInfo

REQUIRED_NAMESPACES: tuple[str, ...] = ("bs", "akarin", "resize2", "mvu", "znedi3", "zsmooth")
OPTIONAL_NAMESPACES: tuple[str, ...] = ("dfttest2_cpu", "vivtc", "vszip", "ffms2")

DEFAULT_CACHE_MB_SD = 2048
DEFAULT_CACHE_MB_HD = 6144


@dataclass
class VSCore:
    """Handle around the single VapourSynth core of this process."""

    core: Any  # vapoursynth.Core
    loaded: dict[str, str] = field(default_factory=dict)  # namespace -> plugin path
    build_info: BuildInfo | None = None

    def has(self, namespace: str) -> bool:
        return namespace in self.loaded

    def close(self) -> None:
        raise NotImplementedError


def open_core(
    *,
    threads: int = 0,
    cache_mb: int = 0,
    plugin_dir: Path | None = None,
    build_info: BuildInfo | None = None,
    load_optional: bool = True,
    check_abi: bool = True,
) -> VSCore:
    """Register the no-autoload policy, create the core, run ``check_abi``, load plugins.

    Raises ComponentError with a code from errors.ErrorCode (never a bare traceback).
    """
    raise NotImplementedError


def check_abi(build_info: BuildInfo) -> list[str]:
    """Compare running python minor / VS core / API versions with build-info; return problems
    as user-facing lines ("Arch updated python to 3.15; reinstall/rebuild qtgmc-6ui")."""
    raise NotImplementedError


def load_plugin(core: Any, path: Path, expect: PluginInfo | None = None) -> str:
    """LoadPlugin one file; return its namespace.  Already-loaded same identifier -> PLUGIN_CONFLICT
    unless it comes from the same path (then no-op)."""
    raise NotImplementedError


def plugin_table(core: Any) -> list[dict[str, str]]:
    """[{namespace, identifier, name, path}] of everything loaded (for doctor)."""
    raise NotImplementedError


def runtime_versions() -> dict[str, str]:
    """{'python': '3.14.7', 'vapoursynth': 'R80', 'api': 'R4.3', 'vsjetpack': '2.2.6', 'jetpytools': ...}
    without creating a core."""
    raise NotImplementedError
