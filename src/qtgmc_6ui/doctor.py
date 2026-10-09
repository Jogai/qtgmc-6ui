"""``qtgmc-6ui doctor`` (WP-B): the first thing a user runs when something breaks.

Checks (id -> title), each Status ok/warn/fail/skip with a hint:
  build_info        build-info.json present and readable
  python_abi        running python minor == build-info python minor
  vapoursynth_abi   running core/api == build-info
  vapoursynth_core  core creates with autoload disabled
  plugin:<name>     each bundled/repo plugin loads (namespace, identifier, version)
  pypkg:<name>      vsjetpack / jetpytools importable and version == build-info
  tool:ffmpeg       ffmpeg + ffprobe present (fail), x264/x265/kvazaar/sox (skip when absent)
  qtgmc_smoke       QTempGaussMC on a 64x64 BlankClip, 4 frames (proves the whole stack)
  locale            LC_CTYPE/PYTHONUTF8 give a UTF-8 filesystem encoding
Output is suitable for pasting into a bug report (``--json`` for the DoctorReport).
"""

from __future__ import annotations

from .models import DoctorReport


def run(*, load_plugins: bool = True, smoke: bool = True) -> DoctorReport:
    raise NotImplementedError


def render_text(report: DoctorReport) -> str:
    raise NotImplementedError


def startup_check() -> list[str]:
    """Cheap subset for app startup (no core creation): build-info, python/VS ABI, plugin dir.
    Returns problem lines; empty means fine."""
    raise NotImplementedError
