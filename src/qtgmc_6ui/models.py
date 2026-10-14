"""Typed data contracts shared by all layers (core, CLI, GUI, tests).

Every dataclass round-trips through ``to_dict()`` / ``from_dict()`` to plain
JSON-able dicts.  Enums serialise as their string value, ``Fraction`` as
``"num/den"``.  Schema versions are per top-level document and bumped when a
field changes meaning; adding optional fields with defaults is not a bump.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any, TypeVar

from . import osdep

T = TypeVar("T")

ANALYSIS_SCHEMA = 1
JOB_SCHEMA = 1
PROFILE_SCHEMA = 1
DOCTOR_SCHEMA = 1
BUILD_INFO_SCHEMA = 1


# --------------------------------------------------------------------------- enums
class FieldOrder(StrEnum):
    TFF = "tff"
    BFF = "bff"


class FieldOrderChoice(StrEnum):
    AUTO = "auto"
    TFF = "tff"
    BFF = "bff"


class ContentClass(StrEnum):
    INTERLACED_TFF = "interlaced_tff"
    INTERLACED_BFF = "interlaced_bff"
    TELECINED = "telecined"
    MIXED = "mixed"
    PROGRESSIVE = "progressive"
    UNDETERMINED = "undetermined"


class RecommendedAction(StrEnum):
    DEINTERLACE = "deinterlace"  # proceed with detected field order
    BLOCK_TELECINED = "block_telecined"  # needs IVTC with another tool; --force allowed
    BLOCK_MIXED = "block_mixed"  # partly telecined; --force allowed
    SKIP_PROGRESSIVE = "skip_progressive"  # recommend not deinterlacing; --force allowed
    CONFIRM_FIELD_ORDER = "confirm_field_order"  # undetermined: user must pick tff/bff


class RateMode(StrEnum):
    DOUBLE = "double"  # bob: one frame per field (25i -> 50p)
    SINGLE = "single"  # bob()[::2]: first field of each frame kept


class ChromaMode(StrEnum):
    KEEP = "keep"  # master keeps source subsampling (4:1:1 goes 422 -> QTGMC -> 411 bit-exact)
    YUV422 = "422"  # keep the 4:2:2 intermediate for 4:1:1 sources


class Preset(StrEnum):
    """Adapter preset names (QTempGaussMC itself has no presets; see qtgmc.PRESET_TABLE)."""

    DRAFT = "draft"
    FASTER = "faster"
    FAST = "fast"
    MEDIUM = "medium"
    SLOW = "slow"
    SLOWER = "slower"
    VERY_SLOW = "very_slow"
    PLACEBO = "placebo"


class PreviewMode(StrEnum):
    SOURCE = "source"  # raw frame N, no deinterlacing, no player-side processing
    FIELDS = "fields"  # both fields of frame N, line-doubled, stacked (first field on top)
    OUTPUT = "output"  # QTGMC output frame(s) for source frame N (2 frames when double rate)


class ScaleMode(StrEnum):
    KEEP = "keep"  # keep anamorphic SAR
    SQUARE = "square"  # resize to square pixels (720x576 SAR 16:15 -> 768x576)
    CUSTOM = "custom"


class ViewRateMode(StrEnum):
    KEEP = "keep"
    SINGLE = "single"  # drop every second frame of a double-rate master


class RateControl(StrEnum):
    CRF = "crf"
    QP = "qp"
    BITRATE = "bitrate"
    CQ = "cq"  # libvpx/libaom constrained quality
    LOSSLESS = "lossless"


class VideoBackend(StrEnum):
    FFMPEG = "ffmpeg"  # encoder is an ffmpeg encoder name
    X264_CLI = "x264"  # standalone x264 fed with y4m, elementary stream muxed by ffmpeg
    X265_CLI = "x265"
    KVAZAAR_CLI = "kvazaar"


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


class MuxVerdict(StrEnum):
    OK = "ok"
    OK_WITH_WARNING = "ok_with_warning"  # e.g. "MS style video codec tag, the file may be unplayable!"
    FAIL = "fail"
    UNKNOWN = "unknown"  # not probed yet


# --------------------------------------------------------------------------- (de)serialisation
_converter: Any = None


def converter() -> Any:
    """The cattrs converter of all contracts: Enum <-> value, Fraction <-> "num/den", scalars as
    they are; missing keys take the defaults, unknown keys are ignored (profiles.from_toml is
    the strict path)."""
    global _converter
    if _converter is None:
        import cattrs

        c = cattrs.Converter(detailed_validation=False)  # plain ValueError/TypeError for callers
        c.register_unstructure_hook(Fraction, lambda f: f"{f.numerator}/{f.denominator}")
        # by runtime type, like plain data (a list[PluginInfo] may hold dicts)
        c.register_unstructure_hook_factory(
            is_dataclass, lambda _cl: lambda o: {f.name: c.unstructure(getattr(o, f.name)) for f in fields(o)}
        )
        c.register_structure_hook(Fraction, lambda v, _t: Fraction(v))
        for tp in (str, int, float, bool):  # no coercion: values are taken as they come
            c.register_structure_hook(tp, lambda v, _t: v)
        c.register_structure_hook(StrEnum, lambda v, t: t(v))  # before str in the MRO
        _converter = c
    return _converter


def to_dict(obj: Any) -> dict[str, Any]:
    return converter().unstructure(obj)


def from_dict(cls: type[T], data: dict[str, Any]) -> T:
    return converter().structure(data, cls)


class Serializable:
    def to_dict(self) -> dict[str, Any]:
        return to_dict(self)

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any]) -> T:
        return from_dict(cls, data)


# --------------------------------------------------------------------------- probe / analysis
@dataclass
class Colorimetry(Serializable):
    """FFmpeg names (``-colorspace``/``-color_primaries``/``-color_trc``/``-color_range`` vocabulary)."""

    matrix: str = "unknown"  # bt470bg | smpte170m | bt709 | ...
    primaries: str = "unknown"
    transfer: str = "unknown"
    range: str = "tv"  # tv | pc
    source: str = "default"  # "tags" (from the file) | "default" (by resolution/rate) | "override"


@dataclass
class VideoProbe(Serializable):
    codec: str = ""
    codec_long: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    bit_depth: int = 8
    fps: Fraction = Fraction(25, 1)  # r_frame_rate
    avg_fps: Fraction | None = None  # avg_frame_rate when ffprobe reports one (differs from fps on VFR sources)
    start_time_s: float = 0.0  # stream start_time (A/V offset = video - audio start)
    sar: Fraction = Fraction(1, 1)
    dar: Fraction | None = None
    nb_frames: int | None = None  # exact when the container knows it, else estimated
    nb_frames_exact: bool = False
    duration_s: float = 0.0
    container_field_order: str = "unknown"  # ffprobe field_order: tt bb tb bt progressive unknown
    color_tags: Colorimetry = field(default_factory=Colorimetry)  # raw tags from ffprobe


@dataclass
class AudioProbe(Serializable):
    index: int = 0  # absolute stream index in the input
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    channel_layout: str = ""
    bit_depth: int | None = None
    duration_s: float = 0.0
    start_time_s: float = 0.0
    language: str = ""


@dataclass
class ProbeInfo(Serializable):
    path: str = ""
    container: str = ""  # ffprobe format_name
    size_bytes: int = 0
    duration_s: float = 0.0
    start_time_s: float = 0.0  # format start_time (earliest stream)
    video: VideoProbe = field(default_factory=VideoProbe)
    audio: list[AudioProbe] = field(default_factory=list)
    video_stream_index: int = 0


@dataclass
class IdetCounts(Serializable):
    tff: int = 0
    bff: int = 0
    progressive: int = 0
    undetermined: int = 0

    @property
    def determined(self) -> int:
        return self.tff + self.bff + self.progressive

    @property
    def total(self) -> int:
        return self.determined + self.undetermined


@dataclass
class RepeatCounts(Serializable):
    neither: int = 0
    top: int = 0
    bottom: int = 0

    @property
    def total(self) -> int:
        return self.neither + self.top + self.bottom


@dataclass
class IdetWindow(Serializable):
    """One ``idet`` pass over ``frames`` frames starting at ``start_s`` seconds / ``start_frame``."""

    start_frame: int = 0
    start_s: float = 0.0
    frames: int = 0
    single: IdetCounts = field(default_factory=IdetCounts)
    multi: IdetCounts = field(default_factory=IdetCounts)
    repeated: RepeatCounts = field(default_factory=RepeatCounts)
    after_fieldmatch: IdetCounts | None = None  # second pass "fieldmatch,idet" (multi), if run
    repeated_after_fieldmatch: RepeatCounts | None = None
    window_class: ContentClass = ContentClass.UNDETERMINED
    combed_after_fieldmatch: int | None = None  # fieldmatch's "still interlaced" frames in the confirmation pass
    span_frames: int | None = None  # frames ffmpeg output in this window (its final "frame=" count)
    span_s: float | None = None  # their timeline span (final "time="); span_frames/span_s != fps -> VFR


@dataclass
class Classification(Serializable):
    content: ContentClass = ContentClass.UNDETERMINED
    field_order: FieldOrder | None = None
    confidence: float = 0.0  # 0..1
    reasons: list[str] = field(default_factory=list)  # human-readable evidence lines
    warnings: list[str] = field(default_factory=list)


@dataclass
class Recommendation(Serializable):
    action: RecommendedAction = RecommendedAction.CONFIRM_FIELD_ORDER
    message: str = ""  # full explanation for the user (IVTC text for telecined/mixed)
    force_allowed: bool = True


@dataclass
class AnalysisWarning(Serializable):
    """Coded, machine-readable warning about the source (e.g. ``variable_frame_rate``)."""

    code: str
    message: str
    hint: str = ""


@dataclass
class AnalysisResult(Serializable):
    schema: int = ANALYSIS_SCHEMA
    input: str = ""
    probe: ProbeInfo = field(default_factory=ProbeInfo)
    windows: list[IdetWindow] = field(default_factory=list)
    totals_single: IdetCounts = field(default_factory=IdetCounts)
    totals_multi: IdetCounts = field(default_factory=IdetCounts)
    totals_repeated: RepeatCounts = field(default_factory=RepeatCounts)
    fieldmatch_run: bool = False
    classification: Classification = field(default_factory=Classification)
    recommendation: Recommendation = field(default_factory=Recommendation)
    colorimetry: Colorimetry = field(default_factory=Colorimetry)  # effective: tags or defaults
    preset_default: Preset = Preset.SLOWER
    frames_out_double: int | None = None
    fps_out_double: Fraction | None = None
    sampled_all_frames: bool = False
    took_s: float = 0.0
    tool_versions: dict[str, str] = field(default_factory=dict)  # {"ffmpeg": "n9.0.2", ...}
    warnings: list[AnalysisWarning] = field(default_factory=list)  # source problems (codes: variable_frame_rate, ...)


# --------------------------------------------------------------------------- jobs
@dataclass
class MasterSettings(Serializable):
    """Archival master options (everything the user may change; the ffv1/flac recipe is fixed)."""

    field_order: FieldOrderChoice = FieldOrderChoice.AUTO
    rate: RateMode = RateMode.DOUBLE
    preset: Preset | None = None  # None = per-resolution default from analysis
    source_match: bool = True
    lossless: bool = True
    denoise_sigma: float = 0.0  # 0 = off; >0 enables DFTTest denoise (breaks field preservation)
    chroma: ChromaMode = ChromaMode.KEEP
    colorimetry_override: Colorimetry | None = None
    threads: int = 0  # 0 = all cores
    cache_mb: int = 0  # 0 = default by resolution (SD 2048, HD 6144)


@dataclass
class ViewCopySpec(Serializable):
    profile: str  # profile id (built-in or user) or path to a .toml
    output: str | None = None  # None = derived from name_template + profile extension


@dataclass
class JobSpec(Serializable):
    schema: int = JOB_SCHEMA
    input: str = ""
    output_dir: str = ""  # default: directory of the input
    name_template: str = "{stem}.deint"  # master -> <template>.mkv, views -> <template>.<profile>.<ext>
    master: MasterSettings = field(default_factory=MasterSettings)
    views: list[ViewCopySpec] = field(default_factory=list)
    force: bool = False  # process despite telecined/mixed/progressive classification
    overwrite: bool = False
    master_output: str | None = None  # explicit master path (-o)
    skip_master: bool = False  # views only, from an existing master given in master_output
    # False: the master is still encoded (views are always made from it) but into a hidden
    # temporary file in output_dir that is deleted afterwards; needs >= 1 view, no master_output
    keep_master: bool = True
    analysis: AnalysisResult | None = None  # reuse a previous analysis instead of re-running


@dataclass
class OutputFile(Serializable):
    kind: str  # "master" | "view"
    path: str
    profile: str | None = None
    frames: int | None = None
    duration_s: float | None = None
    size_bytes: int | None = None


@dataclass
class JobResult(Serializable):
    schema: int = JOB_SCHEMA
    input: str = ""
    analysis: AnalysisResult | None = None
    outputs: list[OutputFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    average_fps: float = 0.0
    effective_master: MasterSettings | None = None
    qtgmc_kwargs: dict[str, Any] = field(default_factory=dict)  # what the adapter actually used


# --------------------------------------------------------------------------- profiles
@dataclass
class ScaleSettings(Serializable):
    mode: ScaleMode = ScaleMode.KEEP
    width: int = 0
    height: int = 0
    algorithm: str = "lanczos"  # ffmpeg scale flag / zscale filter name


@dataclass
class VideoSettings(Serializable):
    backend: VideoBackend = VideoBackend.FFMPEG
    encoder: str = "libx264"  # ffmpeg encoder name; for CLI backends the tool name
    rate_control: RateControl = RateControl.CRF
    quality: float = 18.0  # crf / qp / cq value
    bitrate_kbps: int = 0
    preset: str = ""
    tune: str = ""
    profile: str = ""
    level: str = ""
    pix_fmt: str = ""  # "" = keep master pixel format
    extra_args: list[str] = field(default_factory=list)  # verbatim, advanced
    scale: ScaleSettings = field(default_factory=ScaleSettings)
    rate: ViewRateMode = ViewRateMode.KEEP


@dataclass
class SoxSettings(Serializable):
    enabled: bool = False
    effects: list[str] = field(default_factory=list)  # e.g. ["rate", "-v", "48000", "dither", "-s"]


@dataclass
class AudioSettings(Serializable):
    encoder: str = "aac"  # ffmpeg encoder name or "copy"
    bitrate_kbps: int = 0
    quality: float | None = None  # encoder-specific VBR quality (-q:a / -compression_level)
    sample_rate: int = 0  # 0 = keep
    channels: int = 0  # 0 = keep
    extra_args: list[str] = field(default_factory=list)
    sox: SoxSettings = field(default_factory=SoxSettings)


@dataclass
class Profile(Serializable):
    schema: int = PROFILE_SCHEMA
    id: str = ""  # file stem; [a-z0-9-]+
    name: str = ""
    description: str = ""
    container: str = "matroska"  # ffmpeg muxer name
    extension: str = "mkv"
    video: VideoSettings = field(default_factory=VideoSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    builtin: bool = False  # not stored in TOML; set by the loader
    path: str | None = None  # not stored in TOML; where it was loaded from


# --------------------------------------------------------------------------- encoders / tools
@dataclass
class EncoderOption(Serializable):
    name: str
    type: str  # int float string boolean flags ...
    help: str = ""
    default: str | None = None
    minimum: str | None = None
    maximum: str | None = None
    choices: list[str] = field(default_factory=list)


@dataclass
class EncoderInfo(Serializable):
    name: str
    kind: str  # "video" | "audio"
    description: str = ""
    codec: str = ""  # codec id as ffmpeg prints it (h264, hevc, aac, ...)
    pix_fmts: list[str] = field(default_factory=list)
    sample_fmts: list[str] = field(default_factory=list)
    sample_rates: list[int] = field(default_factory=list)
    options: list[EncoderOption] = field(default_factory=list)  # lazily filled


@dataclass
class MuxerInfo(Serializable):
    name: str
    description: str = ""
    extensions: list[str] = field(default_factory=list)
    default_video: str = ""
    default_audio: str = ""


@dataclass
class ToolInfo(Serializable):
    name: str  # ffmpeg ffprobe x264 x265 kvazaar sox
    path: str | None = None
    version: str | None = None
    available: bool = False
    note: str = ""


@dataclass
class MuxProbe(Serializable):
    container: str
    encoder: str
    verdict: MuxVerdict = MuxVerdict.UNKNOWN
    note: str = ""


# --------------------------------------------------------------------------- doctor / build info
@dataclass
class PluginInfo(Serializable):
    name: str  # component name (akarin, resize2, ...)
    namespace: str = ""
    identifier: str = ""
    version: str = ""  # pinned upstream tag
    file: str = ""  # file name inside the plugin dir (or absolute for repo plugins)
    required: bool = True
    origin: str = "bundled"  # bundled | repo


@dataclass
class BuildInfo(Serializable):
    """Written by packaging at build time to <install root>/build-info.json (paths.build_info_path)."""

    schema: int = BUILD_INFO_SCHEMA
    pkgver: str = ""
    pkgrel: str = ""
    built_at: str = ""  # ISO 8601 UTC (from SOURCE_DATE_EPOCH)
    march: str = "x86-64"
    python_version: str = ""  # "3.14.7"
    python_minor: list[int] = field(default_factory=list)  # [3, 14]
    python_executable: str = field(default_factory=osdep.default_python_executable)
    vapoursynth_pkgver: str = ""  # "80-1"
    vapoursynth_core: int = 0  # 80
    vapoursynth_api: list[int] = field(default_factory=list)  # [4, 3]
    ffmpeg_pkgver: str = ""
    pyside6_pkgver: str = ""
    plugins: list[PluginInfo] = field(default_factory=list)
    python_packages: dict[str, str] = field(default_factory=dict)  # {"vsjetpack": "2.2.6", ...}


@dataclass
class DoctorCheck(Serializable):
    id: str
    title: str
    status: Status = Status.OK
    detail: str = ""
    hint: str = ""


@dataclass
class DoctorReport(Serializable):
    schema: int = DOCTOR_SCHEMA
    ok: bool = False
    summary: str = ""
    checks: list[DoctorCheck] = field(default_factory=list)
    build_info: BuildInfo | None = None
    runtime: dict[str, str] = field(default_factory=dict)  # python, vapoursynth, api, vsjetpack, ...
    plugins: list[dict[str, Any]] = field(default_factory=list)  # {name, loaded, error, path}
    tools: list[ToolInfo] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)  # relevant env vars, locale


# --------------------------------------------------------------------------- preview
@dataclass
class PreviewRequest(Serializable):
    input: str
    frame: int
    mode: PreviewMode = PreviewMode.SOURCE
    field_order: FieldOrder | None = None  # required for FIELDS/OUTPUT when analysis is absent
    master: MasterSettings = field(default_factory=MasterSettings)
    output: str = ""  # PNG path; "-" = raw PNG bytes on stdout
    window: int = 10  # frames of context on each side for OUTPUT mode


@dataclass
class PreviewResult(Serializable):
    output: str
    width: int
    height: int
    frames: int = 1  # 2 for OUTPUT with double rate (stacked vertically)
    took_s: float = 0.0
    note: str = ""
