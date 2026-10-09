"""Field-preservation check (the brief §11 core guarantee), reusable by WP-H's test_fidelity.py.

With source match + lossless every output field line that corresponds to an original field
must be bit-identical to the source.  Mapping (``std.SeparateFields(clip, tff)`` gives the
temporally first field of a frame at index 2n, the second at 2n+1):

  double rate: output frame k comes from source field k; its original lines are the
               field at position (k % 2) of its own separation  -> out_sep[2k + k%2] == src_sep[k]
  single rate: output frame m comes from source field 2m       -> out_sep[2m]       == src_sep[2m]

Planes are compared with numpy; the return value counts mismatching planes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class FidelityResult:
    planes_compared: int = 0
    planes_mismatched: int = 0
    frames_compared: int = 0
    first_mismatch: tuple[int, int] | None = None  # (output frame, plane)
    details: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.planes_compared > 0 and self.planes_mismatched == 0


def _planes(frame: Any) -> list[np.ndarray]:
    return [np.asarray(frame[p]) for p in range(frame.format.num_planes)]


def compare_fields(source: Any, output: Any, tff: bool, double_rate: bool = True,
                   frames: range | None = None) -> FidelityResult:
    """``source``: interlaced clip (any _FieldBased), ``output``: deinterlaced clip at the same
    size/format.  ``frames`` selects output frames (default all)."""
    import vapoursynth as vs

    core = vs.core
    src_sep = core.std.SeparateFields(core.std.SetFieldBased(source, 0), tff=tff)
    out_sep = core.std.SeparateFields(core.std.SetFieldBased(output, 0), tff=tff)
    res = FidelityResult()
    for k in frames or range(output.num_frames):
        if double_rate:
            si, oi = k, 2 * k + (k % 2)
        else:
            si, oi = 2 * k, 2 * k
        if si >= src_sep.num_frames:
            break
        sp = _planes(src_sep.get_frame(si))
        op = _planes(out_sep.get_frame(oi))
        res.frames_compared += 1
        for p, (a, b) in enumerate(zip(sp, op, strict=True)):
            res.planes_compared += 1
            if a.shape != b.shape or not np.array_equal(a, b):
                res.planes_mismatched += 1
                if res.first_mismatch is None:
                    res.first_mismatch = (k, p)
                if len(res.details) < 5:
                    diff = int(np.abs(a.astype(np.int32) - b.astype(np.int32)).max()) if a.shape == b.shape else -1
                    res.details.append(f"frame {k} plane {p}: max abs diff {diff}")
    return res


def compare_files(vscore: Any, source_path: str, master_path: str, tff: bool, double_rate: bool = True,
                  frames: range | None = None, cachepath: str | None = None) -> FidelityResult:
    """Open both files with bestsource and compare (the master must keep the source format)."""
    core = vscore.core
    kw = {"cachepath": cachepath} if cachepath else {}
    src = core.bs.VideoSource(source_path, **kw)
    out = core.bs.VideoSource(master_path, **kw)
    if out.format.id != src.format.id:
        raise AssertionError(f"format differs: source {src.format.name}, master {out.format.name}")
    return compare_fields(src, out, tff, double_rate, frames)
