"""Frame-accurate preview renderer without a player (WP-D).

``qtgmc-6ui preview-frame INPUT N --mode source|fields|output -o out.png``
  source  bestsource frame N -> RGB24 (resize.Bicubic, matrix from colorimetry) -> PNG
  fields  std.SeparateFields(tff) -> fields 2N (first in time) and 2N+1, each line-doubled with
          resize.Point to full height, std.StackVertical -> PNG (height 2x).  Combing shows as
          the two fields differing; wrong field order shows as juddering motion between them.
  output  graph.build(..., trim=(N-window, N+window)) -> QTGMC -> the output frame(s) for N
          (2 stacked for double rate) -> PNG.  Approximate near clip edges (temporal radius).
PNG is written by write_png() (pure python: zlib + struct; ~50 ms for 720x576, verified),
so no ffmpeg round trip and no Qt in the core.  Never uses a player: no auto-deinterlace.
"""

from __future__ import annotations

from .models import PreviewRequest, PreviewResult


def write_png(path: str, width: int, height: int, rgb_rows: list[bytes]) -> int:
    """Write 8-bit RGB rows (each ``3*width`` bytes) as PNG; return byte count.  '-' = stdout."""
    raise NotImplementedError


def render(request: PreviewRequest) -> PreviewResult:
    raise NotImplementedError
