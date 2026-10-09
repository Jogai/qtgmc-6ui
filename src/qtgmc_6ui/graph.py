"""Filter graph builder (WP-B): source -> (chroma wrap) -> QTGMC -> output clip, plus
an equivalent .vpy text for debugging (``qtgmc-6ui script``).

Order of operations is fixed: deinterlace first, everything else (resize, crop, format
conversion for viewing copies) after it; ``build(post=...)`` is the hook for such steps.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import buildinfo, paths, qtgmc
from .errors import BlockedError, ErrorCode, InputError
from .models import AnalysisResult, FieldOrder, FieldOrderChoice, MasterSettings, Preset, RateMode
from .vsenv import VSCore

# y4m formats ffmpeg can read back (arch-facts §3g): YUV 420/422/444/411 and GRAY, integer samples
_Y4M_SUBSAMPLING = {(1, 1), (1, 0), (0, 0), (2, 0)}


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
    y4m_problem: str = ""
    width: int = 0
    height: int = 0
    format_name: str = ""


def resolve_field_order(analysis: AnalysisResult, settings: MasterSettings) -> FieldOrder:
    """settings override > analysis; neither -> BlockedError(BLOCKED_UNDETERMINED)."""
    if settings.field_order != FieldOrderChoice.AUTO:
        return FieldOrder(settings.field_order.value)
    fo = analysis.classification.field_order
    if fo is None:
        raise BlockedError(
            ErrorCode.BLOCKED_UNDETERMINED,
            "the field order could not be determined from the content",
            "check the fields preview and pass --field-order tff|bff explicitly",
            {"classification": analysis.classification.content.value},
        )
    return fo


def resolve_preset(analysis: AnalysisResult, settings: MasterSettings) -> Preset:
    if settings.preset is not None:
        return settings.preset
    v = analysis.probe.video
    return qtgmc.default_preset(v.width, v.height)


def y4m_support(fmt: Any) -> str:
    """'' when the format can be piped as y4m and read by ffmpeg, else the reason."""
    if fmt is None:
        return "variable format clips cannot be piped"
    fam = fmt.color_family.name
    if fam == "RGB":
        return "RGB sources have no y4m representation"
    if fam not in ("YUV", "GRAY"):
        return f"colour family {fam} has no y4m representation"
    if fmt.sample_type.name != "INTEGER":
        return "float sample formats have no y4m representation"
    if fam == "YUV" and (fmt.subsampling_w, fmt.subsampling_h) not in _Y4M_SUBSAMPLING:
        return f"ffmpeg cannot read y4m with subsampling {fmt.subsampling_w}/{fmt.subsampling_h} (4:1:0 / 4:4:0)"
    return ""


def open_source(vscore: VSCore, path: str, field_order: FieldOrder, *, cachepath: str | None = None) -> Any:
    """``core.bs.VideoSource(path, cachepath=...)`` + ``std.SetFieldBased`` from the analysis
    (never trust the container flag)."""
    import vapoursynth as vs

    if not os.path.isfile(path):
        raise InputError(ErrorCode.INPUT_NOT_FOUND, f"input not found: {path}", "", {"path": path})
    core = vscore.core
    if cachepath is None:
        cachepath = str(paths.bs_index_dir())
    Path(cachepath).mkdir(parents=True, exist_ok=True)
    try:
        clip = core.bs.VideoSource(path, cachepath=cachepath)
    except vs.Error as e:
        raise InputError(
            ErrorCode.INPUT_UNREADABLE, f"bestsource cannot open {path}: {e}", "", {"path": path, "error": str(e)}
        ) from e
    return core.std.SetFieldBased(clip, 2 if field_order == FieldOrder.TFF else 1)


def build(
    vscore: VSCore,
    analysis: AnalysisResult,
    settings: MasterSettings,
    *,
    trim: tuple[int, int] | None = None,
    cachepath: str | None = None,
    post: Callable[[Any], Any] | None = None,
) -> Graph:
    """Resolve field order (settings override > analysis), preset (settings > default by
    resolution), build the clip.  ``trim`` = (first, last) source frames for previews;
    ``post`` is applied to the deinterlaced clip (viewing-copy resize/crop hooks)."""
    field_order = resolve_field_order(analysis, settings)
    preset = resolve_preset(analysis, settings)
    src = open_source(vscore, analysis.input or analysis.probe.path, field_order, cachepath=cachepath)
    clip = src
    if trim is not None:
        first, last = trim
        first = max(0, first)
        last = min(src.num_frames - 1, last)
        clip = src[first : last + 1]
    problem = y4m_support(clip.format)
    out = qtgmc.deinterlace(clip, settings, field_order, preset)
    if post is not None:
        out = post(out)
    kwargs = qtgmc.build_kwargs(settings, preset, clip.width, clip.height)
    return Graph(
        clip=out,
        source=src,
        field_order=field_order,
        preset=preset,
        qtgmc_kwargs=kwargs,
        frames_out=out.num_frames,
        fps_num=out.fps.numerator,
        fps_den=out.fps.denominator,
        y4m_ok=problem == "",
        y4m_problem=problem,
        width=out.width,
        height=out.height,
        format_name=out.format.name if out.format else "",
    )


def _py(v: Any) -> str:
    return json.dumps(v) if isinstance(v, (dict, list)) else repr(v)


def script_text(analysis: AnalysisResult, settings: MasterSettings) -> str:
    """Standalone .vpy equivalent (explicit LoadPlugin lines, same kwargs) for bug reports."""
    v = analysis.probe.video
    try:
        field_order = resolve_field_order(analysis, settings)
        fo_line = f"clip = core.std.SetFieldBased(clip, {2 if field_order == FieldOrder.TFF else 1})  # {field_order.value}"
        tff = field_order == FieldOrder.TFF
    except BlockedError:
        fo_line = "clip = core.std.SetFieldBased(clip, 1)  # FIELD ORDER UNDETERMINED: 1 = BFF, 2 = TFF"
        tff = False
    preset = resolve_preset(analysis, settings)
    kwargs = qtgmc.build_kwargs(settings, preset, v.width, v.height)
    try:
        bi = buildinfo.load_or_dev()
    except Exception:
        bi = None
    plugin_dir = paths.plugin_dir()
    sysdir = paths.system_vs_plugin_dir()
    lines = [
        "# generated by qtgmc-6ui script; run with: vspipe -c y4m this.vpy - | ffmpeg -i - ...",
        f"# input: {analysis.input}  {v.width}x{v.height} {v.pix_fmt} {v.fps} SAR {v.sar}",
        "import vapoursynth as vs",
        "from vsdeinterlace import QTempGaussMC",
        "from vsaa import NNEDI3",
        "from vskernels import Catrom",
        "core = vs.core",
        "# plugins are loaded explicitly (the app disables autoloading)",
    ]
    bs = (sysdir / "libbestsource.so") if sysdir else Path("/usr/lib/python3.14/site-packages/vapoursynth/plugins/libbestsource.so")
    lines.append(f"core.std.LoadPlugin({str(bs)!r})")
    for p in (bi.plugins if bi else []):
        if p.origin == "repo":
            continue
        path = Path(p.file) if Path(p.file).is_absolute() else plugin_dir / p.file
        lines.append(f"core.std.LoadPlugin({str(path)!r})" + ("" if p.required else "  # optional"))
    lines += [
        f"clip = core.bs.VideoSource({analysis.input!r})",
        fo_line,
    ]
    fmt = v.pix_fmt
    if fmt.startswith("yuv411"):
        lines.append("clip = core.resize.Point(clip, format=clip.format.replace(subsampling_w=1).id)  # 4:1:1 -> 4:2:2 (mvu.Super)")
    lines.append(f"# preset {preset.value}")
    items = []
    for k, val in kwargs.items():
        if k == "bobber":
            if "nnedi3" in val:
                a = ", ".join(f"{kk}={vv!r}" for kk, vv in val["nnedi3"].items())
                items.append(f"basic_bobber=NNEDI3({a})")
            else:
                items.append("basic_bobber=Catrom()")
        elif k == "denoise_func":
            items.append(f"denoise_func=DFTTest(sigma={val['dfttest']['sigma']!r})")
            lines.insert(4, "from vsdenoise import DFTTest")
        elif k == "source_match_mode":
            items.append(f"source_match_mode=QTempGaussMC.SourceMatchMode({val})")
        elif k == "lossless_mode":
            items.append(f"lossless_mode=QTempGaussMC.LosslessMode({val})")
        elif k == "back_blend_mode":
            items.append(f"back_blend_mode=QTempGaussMC.BackBlendMode({val})")
        elif k == "denoise_mode":
            items.append(f"denoise_mode=QTempGaussMC.NoiseProcessMode({val})")
        else:
            items.append(f"{k}={_py(val)}")
    lines.append("qtgmc = QTempGaussMC(\n    " + ",\n    ".join(items) + ",\n)")
    lines.append(f"out = qtgmc.bob(clip, tff={tff})")
    if settings.rate == RateMode.SINGLE:
        lines.append("out = out[::2]  # single rate: keep the frame of the first field (never fps_divisor=2)")
    if fmt.startswith("yuv411") and settings.chroma.value == "keep":
        lines.append("out = core.resize.Point(out, format=clip.format.replace(subsampling_w=2).id)  # back to 4:1:1")
    lines.append("out.set_output()")
    return "\n".join(lines) + "\n"
