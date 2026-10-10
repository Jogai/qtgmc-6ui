"""Filter graph builder (WP-B): source -> (chroma wrap) -> QTGMC -> output clip, plus
an equivalent .vpy text for debugging (``qtgmc-6ui script``).

Order of operations is fixed: deinterlace first, everything else (resize, crop, format
conversion for viewing copies) after it; ``build(post=...)`` is the hook for such steps.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import buildinfo, osdep, paths, qtgmc
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
    color_range: str = ""  # "tv" | "pc" | "" from the decoder's _ColorRange (what y4m will carry)
    chroma_location: str = ""  # ffmpeg name (left, center, topleft, ...) from _ChromaLocation, "" if unknown
    warnings: list[str] = field(default_factory=list)  # e.g. variable frame rate


CHROMA_LOCATION_NAMES: tuple[str, ...] = ("left", "center", "topleft", "top", "bottomleft", "bottom")
FPS_TOLERANCE = Fraction(1, 1000)


def source_props(src: Any) -> tuple[str, str]:
    """(color_range, chroma_location) in ffmpeg vocabulary from the first frame's props."""
    try:
        props = src.get_frame(0).props
    except Exception:
        return "", ""
    rng = ""
    key = "_Range" if "_Range" in props else ("_ColorRange" if "_ColorRange" in props else "")  # _ColorRange: pre-R80 name
    if key:
        import vapoursynth as vs

        try:
            rng = "pc" if vs.Range(int(props[key])) == vs.RANGE_FULL else "tv"
        except (ValueError, TypeError):
            rng = ""
    loc = ""
    if "_ChromaLocation" in props:
        try:
            i = int(props["_ChromaLocation"])
            loc = CHROMA_LOCATION_NAMES[i] if 0 <= i < len(CHROMA_LOCATION_NAMES) else ""
        except (ValueError, TypeError):
            loc = ""
    return rng, loc


def geometry_problem(fmt: Any, width: int, height: int) -> str:
    """'' when the frame can be split into fields, else why not (odd field heights)."""
    if fmt is None:
        return ""
    step = 2 << fmt.subsampling_h
    if height % step:
        fh = height / 2
        suggest = 480 if 480 < height <= 496 else height - height % step  # 486-line NTSC D1 -> 480
        return (f"{height} lines cannot be split into two fields of {fh:g} lines with {fmt.name} chroma "
                f"(needs a multiple of {step}); crop to {suggest} lines first, or convert 4:2:0 to 4:2:2")
    return ""


def encodable_path(path: str) -> str:
    """VapourSynth needs a UTF-8 path; a name that is not valid UTF-8 (surrogate-escaped) is
    reached through an ASCII-named alias in the cache dir (osdep.ascii_alias)."""
    try:
        path.encode("utf-8")
        return path
    except UnicodeEncodeError:
        pass
    try:
        return osdep.ascii_alias(path, paths.cache_dir() / "links")
    except OSError as e:
        raise InputError(
            ErrorCode.INPUT_UNREADABLE,
            f"the file name is not valid UTF-8 and no link could be created: {e}",
            "rename the file to a UTF-8 name",
            {"path": path},
        ) from e


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
        clip = core.bs.VideoSource(encodable_path(path), cachepath=cachepath)
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
    require_y4m: bool = True,
) -> Graph:
    """Resolve field order (settings override > analysis), preset (settings > default by
    resolution), build the clip.  ``trim`` = (first, last) source frames for previews;
    ``post`` is applied to the deinterlaced clip (viewing-copy resize/crop hooks);
    ``require_y4m`` raises InputError(INPUT_FORMAT_UNSUPPORTED) for formats that cannot
    be piped to ffmpeg (previews may pass False and convert themselves)."""
    field_order = resolve_field_order(analysis, settings)
    preset = resolve_preset(analysis, settings)
    src = open_source(vscore, analysis.input or analysis.probe.path, field_order, cachepath=cachepath)
    pv = analysis.probe.video
    warnings: list[str] = []
    if pv.width and pv.height and (src.width, src.height) != (pv.width, pv.height):
        raise InputError(
            ErrorCode.INPUT_FORMAT_UNSUPPORTED,
            f"the source is {pv.width}x{pv.height} but the decoder delivers {src.width}x{src.height}: "
            "odd dimensions cannot be carried losslessly (they would be cropped silently)",
            "crop or pad the source to even dimensions explicitly first (e.g. ffmpeg -vf pad/crop with -c:v ffv1)",
            {"source": f"{pv.width}x{pv.height}", "decoded": f"{src.width}x{src.height}"},
        )
    gp = geometry_problem(src.format, src.width, src.height)
    if gp:
        raise InputError(ErrorCode.INPUT_FORMAT_UNSUPPORTED, gp,
                         "NTSC 486-line 4:2:0 captures: crop to 480 lines (ffmpeg -vf crop=720:480) or capture as 4:2:2",
                         {"width": src.width, "height": src.height, "format": src.format.name if src.format else ""})
    if pv.fps and src.fps.denominator and abs(Fraction(src.fps.numerator, src.fps.denominator) / pv.fps - 1) > FPS_TOLERANCE:
        warnings.append(f"variable or inconsistent frame rate: the container says {pv.fps} fps, the decoded stream averages "
                        f"{src.fps.numerator}/{src.fps.denominator}; the master gets the averaged constant rate and motion "
                        "timing is evened out (convert to constant frame rate first if this matters)")
    if pv.nb_frames_exact and pv.nb_frames and src.num_frames != pv.nb_frames:
        warnings.append(f"the container reports {pv.nb_frames} frames but the decoder delivers {src.num_frames}")
    color_range, chroma_location = source_props(src)
    clip = src
    if trim is not None:
        first, last = trim
        first = max(0, first)
        last = min(src.num_frames - 1, last)
        clip = src[first : last + 1]
    problem = y4m_support(clip.format)
    if problem and (require_y4m or clip.format.color_family.name not in ("YUV", "GRAY")):
        raise InputError(
            ErrorCode.INPUT_FORMAT_UNSUPPORTED,
            f"cannot process {clip.format.name if clip.format else 'this format'}: {problem}",
            "convert the source to an 8-16 bit YUV 4:2:0/4:2:2/4:4:4/4:1:1 (or gray) format first",
            {"format": clip.format.name if clip.format else ""},
        )
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
        color_range=color_range,
        chroma_location=chroma_location,
        warnings=warnings,
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
    bs = (sysdir or osdep.fallback_vs_plugin_dir()) / osdep.shared_lib_name("bestsource")
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
    post_calls = []
    for k, val in kwargs.items():
        if k in qtgmc.POST_CALL_KEYS:
            method, arg = qtgmc.POST_CALL_KEYS[k]
            post_calls.append(f"qtgmc.{method}({arg}={_py(val)})  # sharpen_ prefix clashes in __init__")
        elif k == "bobber":
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
    lines.extend(post_calls)
    lines.append(f"out = qtgmc.bob(clip, tff={tff})")
    if settings.rate == RateMode.SINGLE:
        lines.append("out = out[::2]  # single rate: keep the frame of the first field (never fps_divisor=2)")
    if fmt.startswith("yuv411") and settings.chroma.value == "keep":
        lines.append("out = core.resize.Point(out, format=clip.format.replace(subsampling_w=2).id)  # back to 4:1:1")
    lines.append("out.set_output()")
    return "\n".join(lines) + "\n"
