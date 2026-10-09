"""Frame-accurate preview renderer without a player (WP-D).

``qtgmc-6ui preview-frame INPUT N --mode source|fields|output -o out.png``
  source  bestsource frame N -> RGB24 (resize.Bicubic, matrix from props/colorimetry; chroma
          upsampled with point so the two fields' chroma is never blended) -> PNG
  fields  std.SeparateFields(tff) -> fields 2N (first in time) and 2N+1, each converted to RGB
          and line-doubled with resize.Point to full height, stacked vertically -> PNG (height
          2x).  Combing shows as the two fields differing; wrong field order shows as juddering
          motion between them.
  output  graph.build(..., trim=(first, last)) -> QTGMC -> the output frame(s) for N
          (2 stacked for double rate) -> PNG.  Approximate near clip edges (temporal radius).
PNG is written by write_png() (zlib + struct, numpy for interleaving when available), so no
ffmpeg round trip and no Qt in the core.  Never uses a player: no auto-deinterlace.

``preview-serve`` keeps one core and the opened sources/graphs alive between requests
(see serve()), which is what the GUI scrubber uses.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
import zlib
from typing import IO, Any

from .errors import ComponentError, ErrorCode, QtgmcError, UsageError
from .models import (
    AnalysisResult,
    Classification,
    ContentClass,
    FieldOrder,
    FieldOrderChoice,
    MasterSettings,
    PreviewMode,
    PreviewRequest,
    PreviewResult,
    RateMode,
)

PNG_LEVEL = 3  # 720x576: ~15 ms deflate at level 3 vs ~40 ms at 6; size difference is small


def write_png(path: str, width: int, height: int, rgb_rows: list[bytes]) -> int:
    """Write 8-bit RGB rows (each ``3*width`` bytes) as PNG; return byte count.  '-' = stdout."""
    if len(rgb_rows) != height or any(len(r) != 3 * width for r in rgb_rows):
        raise ValueError("rgb_rows must be `height` rows of 3*width bytes")
    raw = b"".join(b"\x00" + r for r in rgb_rows)
    return _write_png_raw(path, width, height, raw)


def png_bytes(width: int, height: int, filtered: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(filtered, PNG_LEVEL)) + chunk(b"IEND", b"")


def _write_png_raw(path: str, width: int, height: int, filtered: bytes) -> int:
    data = png_bytes(width, height, filtered)
    if path == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return len(data)
    tmp = f"{path}.part-{os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)  # the GUI never sees a half-written PNG
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        from .errors import OutputError

        raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"cannot write {path}: {e.strerror}") from None
    return len(data)


def _frame_to_filtered(frame: Any) -> tuple[int, int, bytes]:
    """RGB24 VideoFrame -> (w, h, PNG scanlines with filter byte 0)."""
    w, h = frame.width, frame.height
    try:
        import numpy as np

        rgb = np.empty((h, 3 * w + 1), dtype=np.uint8)
        rgb[:, 0] = 0
        px = rgb[:, 1:].reshape(h, w, 3)
        for i in range(3):
            px[:, :, i] = np.asarray(frame[i])
        return w, h, rgb.tobytes()
    except ImportError:
        pass
    planes = [bytes(frame[i]) for i in range(3)]  # contiguous w*h each (no stride padding)
    out = bytearray((3 * w + 1) * h)
    for y in range(h):
        o = y * (3 * w + 1) + 1
        for i in range(3):
            out[o + i : o + 3 * w : 3] = planes[i][y * w : (y + 1) * w]
    return w, h, bytes(out)


# --------------------------------------------------------------------------- renderer
class Renderer:
    """One VapourSynth core per process (vsenv rule); sources and output graphs are cached."""

    def __init__(self) -> None:
        self.vscore: Any = None
        self.full = False  # all bundled plugins loaded (needed for OUTPUT)
        self.sources: dict[str, Any] = {}
        self.matrix: dict[str, tuple[int, int]] = {}
        self.graphs: dict[tuple[Any, ...], Any] = {}
        self.analyses: dict[str, AnalysisResult] = {}

    # ---- core
    def _open(self, full: bool) -> Any:
        from . import vsenv

        if self.vscore is not None and (self.full or not full):
            return self.vscore
        if self.vscore is not None:  # minimal core open but QTGMC needed: reopen
            self.close()
        try:
            self.vscore = vsenv.open_core(load_optional=False)
            self.full = True
        except ComponentError:
            if full:
                raise
            # source/fields only need bestsource: a core with just the repo plugins
            from .models import BuildInfo

            self.vscore = vsenv.open_core(build_info=BuildInfo(plugins=[]), check_abi=False, load_optional=False)
            self.full = False
        import atexit

        # registered after vapoursynth's own exit hooks, so it runs before them (LIFO)
        atexit.unregister(self.close)
        atexit.register(self.close)
        return self.vscore

    def close(self) -> None:
        self.sources.clear()
        self.graphs.clear()
        if self.vscore is not None:
            try:
                self.vscore.close()
            finally:
                self.vscore = None
                self.full = False

    def source(self, path: str, full: bool = False) -> Any:
        vsc = self._open(full)
        if path not in self.sources:
            from . import paths

            core = vsc.core
            if not vsc.has("bs") and not hasattr(core, "bs"):
                raise ComponentError(ErrorCode.PLUGIN_LOAD_FAILED, "bestsource is not loaded", "install vapoursynth-plugin-bestsource")
            cache = paths.bs_index_dir()
            try:
                cache.mkdir(parents=True, exist_ok=True)
                clip = core.bs.VideoSource(source=path, cachepath=str(cache))
            except Exception as e:  # vs.Error: unreadable / unsupported input
                raise QtgmcError(
                    ErrorCode.INPUT_UNREADABLE, f"cannot open {path} with bestsource: {e}", "the file may be damaged or in an unsupported format"
                ) from None
            self.sources[path] = clip
            f0 = clip.get_frame(0)
            m = int(f0.props.get("_Matrix", 2))
            r = int(f0.props.get("_ColorRange", 1))
            if m in (0, 2, 3) or m > 14:  # rgb/unspecified/reserved -> by resolution (601 vs 709)
                m = 1 if clip.height > 576 else 5
            self.matrix[path] = (m, r)
        return self.sources[path]

    def info(self, path: str) -> dict[str, Any]:
        clip = self.source(path)
        f0 = clip.get_frame(0)
        fb = int(f0.props.get("_FieldBased", 0))
        sar = (int(f0.props.get("_SARNum", 1)) or 1, int(f0.props.get("_SARDen", 1)) or 1)
        return {
            "input": path,
            "frames": clip.num_frames,
            "fps": f"{clip.fps.numerator}/{clip.fps.denominator}",
            "fps_float": round(clip.fps.numerator / clip.fps.denominator, 6) if clip.fps.denominator else 0.0,
            "width": clip.width,
            "height": clip.height,
            "format": clip.format.name if clip.format else "variable",
            "sar": f"{sar[0]}:{sar[1]}",
            "field_based_prop": {0: "progressive", 1: "bff", 2: "tff"}.get(fb, "unknown"),
            "duration_s": round(clip.num_frames * clip.fps.denominator / clip.fps.numerator, 3) if clip.fps.numerator else 0.0,
        }

    # ---- conversion
    def _rgb(self, clip: Any, path: str, point_chroma: bool) -> Any:
        import vapoursynth as vs

        core = self.vscore.core
        if clip.format.color_family == vs.RGB:
            return core.resize.Point(clip, format=vs.RGB24) if clip.format.id != vs.RGB24 else clip
        m, r = self.matrix.get(path, (5, 1))
        kw: dict[str, Any] = {"format": vs.RGB24, "matrix_in": m, "range_in": r}
        if point_chroma:
            kw["resample_filter_uv"] = "point"
        if clip.format.color_family == vs.GRAY:
            kw.pop("matrix_in")
        return core.resize.Bicubic(clip, **kw)

    def _field_order(self, req: PreviewRequest, clip: Any) -> tuple[FieldOrder | None, str]:
        if req.field_order is not None:
            return req.field_order, ""
        if req.master.field_order != FieldOrderChoice.AUTO:
            return FieldOrder(req.master.field_order.value), ""
        a = self._cached_analysis(req.input)
        if a is not None and a.classification.field_order is not None:
            return a.classification.field_order, f"field order {a.classification.field_order} from analysis"
        fb = int(clip.get_frame(0).props.get("_FieldBased", 0))
        if fb in (1, 2):
            fo = FieldOrder.BFF if fb == 1 else FieldOrder.TFF
            return fo, f"field order {fo} from the stream flag (unverified)"
        return None, ""

    def _cached_analysis(self, path: str) -> AnalysisResult | None:
        if path in self.analyses:
            return self.analyses[path]
        try:
            from . import analysis

            a = analysis.load_cached(path)
        except Exception:  # noqa: BLE001 - cache is optional
            a = None
        if a is not None:
            self.analyses[path] = a
        return a

    # ---- modes
    def render(self, req: PreviewRequest) -> PreviewResult:
        t0 = time.monotonic()
        mode = PreviewMode(req.mode)
        full = mode == PreviewMode.OUTPUT
        src = self.source(req.input, full=full)
        if req.frame < 0 or req.frame >= src.num_frames:
            raise UsageError(f"frame {req.frame} out of range 0..{src.num_frames - 1}")
        note = ""
        frames = 1
        if mode == PreviewMode.SOURCE:
            out = self._rgb(src[req.frame], req.input, point_chroma=True)
            fr = out.get_frame(0)
        elif mode == PreviewMode.FIELDS:
            fo, note = self._field_order(req, src)
            if fo is None:
                fo = FieldOrder.TFF
                note = "field order unknown: showing the top field first; choose a field order to change it"
            fr = self._fields(src, req, fo)
            frames = 2
        else:
            fr, frames, note = self._output(src, req)
        w, h, filtered = _frame_to_filtered(fr)
        _write_png_raw(req.output, w, h, filtered)
        return PreviewResult(output=req.output, width=w, height=h, frames=frames, took_s=round(time.monotonic() - t0, 4), note=note)

    def _fields(self, src: Any, req: PreviewRequest, fo: FieldOrder) -> Any:
        core = self.vscore.core
        n = req.frame
        sep = core.std.SeparateFields(core.std.SetFieldBased(src[n], 0), tff=fo == FieldOrder.TFF)
        rows = []
        for i in (0, 1):  # 0 = first in time
            f = self._rgb(sep[i], req.input, point_chroma=False)
            rows.append(core.resize.Point(f, height=src.height))
        return core.std.StackVertical(rows).get_frame(0)

    def _analysis_for_output(self, req: PreviewRequest, src: Any) -> AnalysisResult:
        a = self._cached_analysis(req.input)
        if a is not None:
            return a
        fo = req.field_order or (FieldOrder(req.master.field_order.value) if req.master.field_order != FieldOrderChoice.AUTO else None)
        if fo is not None:
            from . import ffprobe

            probe = ffprobe.probe(req.input)
            try:
                from . import colorimetry

                col = colorimetry.effective_colorimetry(probe.video)
            except Exception:  # noqa: BLE001
                from .models import Colorimetry

                col = Colorimetry()
            content = ContentClass.INTERLACED_TFF if fo == FieldOrder.TFF else ContentClass.INTERLACED_BFF
            a = AnalysisResult(
                input=req.input,
                probe=probe,
                classification=Classification(content, fo, 0.0, ["field order given for the preview"]),
                colorimetry=col,
            )
            try:
                from . import qtgmc

                a.preset_default = qtgmc.default_preset(probe.video.width, probe.video.height)
            except Exception:  # noqa: BLE001
                pass
        else:
            from . import analysis

            a = analysis.analyze(req.input)
        self.analyses[req.input] = a
        return a

    def _output(self, src: Any, req: PreviewRequest) -> tuple[Any, int, str]:
        from . import graph

        core = self.vscore.core
        a = self._analysis_for_output(req, src)
        settings = MasterSettings.from_dict(req.master.to_dict())
        if req.field_order is not None:
            settings.field_order = FieldOrderChoice(req.field_order.value)
        if settings.field_order == FieldOrderChoice.AUTO and a.classification.field_order is None:
            raise QtgmcError(ErrorCode.BLOCKED_UNDETERMINED, "field order unknown for the output preview", "choose the field order explicitly (tff or bff)")
        n, nf, win = req.frame, src.num_frames, max(1, req.window)
        # aligned windows so consecutive frames reuse one graph, with >= win frames of context
        first = max(0, (n // win) * win - win)
        last = min(nf - 1, first + 3 * win - 1)
        key = (req.input, json.dumps(settings.to_dict(), sort_keys=True), first, last)
        g = self.graphs.get(key)
        if g is None:
            if len(self.graphs) > 4:
                self.graphs.clear()
            g = graph.build(self.vscore, a, settings, trim=(first, last))
            self.graphs[key] = g
        clip = g.clip
        idx = n - first
        double = settings.rate == RateMode.DOUBLE
        picks = [2 * idx, 2 * idx + 1] if double else [idx]
        picks = [min(p, clip.num_frames - 1) for p in picks]
        parts = [self._rgb(clip[p], req.input, point_chroma=False) for p in picks]
        out = core.std.StackVertical(parts) if len(parts) > 1 else parts[0]
        note = f"QTGMC preset {g.preset}, window {first}..{last}"
        if first > 0 and n - first < win or last < nf - 1 and last - n < win:
            note += " (approximate: near the window edge)"
        return out.get_frame(0), len(parts), note


_renderer: Renderer | None = None


def renderer() -> Renderer:
    global _renderer
    if _renderer is None:
        _renderer = Renderer()
    return _renderer


def render(request: PreviewRequest) -> PreviewResult:
    return renderer().render(request)


def source_info(path: str) -> dict[str, Any]:
    """Frame count / fps / size of the source as bestsource sees it (exact frame count)."""
    return renderer().info(path)


# --------------------------------------------------------------------------- server
def _request_from(d: dict[str, Any], path: str) -> PreviewRequest:
    if "frame" not in d or "output" not in d:
        raise UsageError("preview request needs 'frame' and 'output'")
    fo = d.get("field_order")
    settings = MasterSettings(
        field_order=FieldOrderChoice(fo) if fo else FieldOrderChoice.AUTO,
        rate=RateMode(d.get("rate", "double")),
        lossless=bool(d.get("lossless", True)),
        source_match=bool(d.get("source_match", True)),
    )
    if d.get("preset"):
        from .models import Preset

        settings.preset = Preset(d["preset"])
    out = str(d["output"])
    if out == "-":
        raise UsageError("preview-serve writes files only; give an output path")
    out = os.path.abspath(os.path.expanduser(out))
    if os.path.exists(out) and os.path.exists(path) and os.path.samefile(out, path):
        raise UsageError("preview output would overwrite the input")
    return PreviewRequest(
        input=path,
        frame=int(d["frame"]),
        mode=PreviewMode(d.get("mode", "source")),
        field_order=FieldOrder(fo) if fo else None,
        master=settings,
        output=out,
        window=int(d.get("window", 10)),
    )


def serve(path: str, stdin: IO[str], stdout: IO[str]) -> int:
    """Request/response loop for the GUI scrubber.

    stdin: one JSON object per line ``{"id": any, "frame": N, "mode": "source|fields|output",
    "output": "/path.png", "field_order"?: "tff|bff", "rate"?, "preset"?, "lossless"?, "window"?}``;
    the line ``quit`` (or EOF) ends the session; a closed stdout ends it too.
    This is the recommended GUI path: one process per input keeps the core, the source index
    and the last QTGMC window open (source/fields ~20-40 ms per frame instead of ~0.4 s).
    Requests are answered in order; a front end drops replies whose ``id`` is stale.
    stdout (events.py format): ``start``, then ``result{data:{kind:"info", ...source_info}}``,
    then per request ``result{data:{kind:"frame", id, ...PreviewResult}}`` or
    ``error{id, code, message, hint, details, exit_code}``; ``done{exit_code}`` last."""
    from .errors import ExitCode
    from .events import Emitter, JsonLinesSink

    em = Emitter(JsonLinesSink(stdout))
    em.start("preview-serve", path, [])
    r = renderer()
    try:
        em.result({"kind": "info", **r.info(path)})
    except QtgmcError as e:
        em.error(e.to_dict())
        em.done(e.exit_code)
        return int(e.exit_code)
    try:
        _serve_loop(r, em, path, stdin)
    except (BrokenPipeError, ConnectionResetError):  # reader went away
        r.close()
        return int(ExitCode.OK)
    r.close()
    try:
        em.done(ExitCode.OK)
    except (BrokenPipeError, ConnectionResetError):
        pass
    return int(ExitCode.OK)


def _serve_loop(r: Renderer, em: Any, path: str, stdin: IO[str]) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        if line in ("quit", "exit"):
            break
        rid: Any = None
        try:
            d = json.loads(line)
            if not isinstance(d, dict):
                raise UsageError("request must be a JSON object")
            rid = d.get("id")
            res = r.render(_request_from(d, path))
            em.result({"kind": "frame", "id": rid, **res.to_dict()})
        except ValueError as e:
            err = UsageError(f"bad request: {e}")
            em.error({**err.to_dict(), "id": rid})
        except QtgmcError as e:
            em.error({**e.to_dict(), "id": rid})
        except Exception as e:  # noqa: BLE001 - keep serving
            from .commands import internal_error

            em.error({**internal_error(e).to_dict(), "id": rid})
