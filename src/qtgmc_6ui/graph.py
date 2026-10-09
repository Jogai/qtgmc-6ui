"""Filter graph builder (WP-B): source -> (chroma wrap) -> QTGMC -> output clip, plus
an equivalent .vpy text for debugging (``qtgmc-6ui script``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import AnalysisResult, FieldOrder, MasterSettings, Preset
from .vsenv import VSCore


@dataclass
class Graph:
    clip: Any  # output VideoNode
    source: Any  # bestsource clip (for preview and tests)
    field_order: FieldOrder
    preset: Preset
    qtgmc_kwargs: dict[str, Any]
    frames_out: int
    fps_num: int
    fps_den: int
    y4m_ok: bool  # False for RGB/float/alpha/410/440 formats (no y4m path)


def open_source(vscore: VSCore, path: str, field_order: FieldOrder, *, cachepath: str | None = None) -> Any:
    """``core.bs.VideoSource(path, cachepath=...)`` + ``std.SetFieldBased`` from the analysis
    (never trust the container flag)."""
    raise NotImplementedError


def build(vscore: VSCore, analysis: AnalysisResult, settings: MasterSettings, *, trim: tuple[int, int] | None = None) -> Graph:
    """Resolve field order (settings override > analysis), preset (settings > default by
    resolution), build the clip.  ``trim`` = (first, last) source frames for previews."""
    raise NotImplementedError


def script_text(analysis: AnalysisResult, settings: MasterSettings) -> str:
    """Standalone .vpy equivalent (explicit LoadPlugin lines, same kwargs) for bug reports."""
    raise NotImplementedError
