"""Analysis orchestration (WP-A): probe -> idet windows -> telecine confirmation -> classify.

Emits stages probe/idet/fieldmatch/classify and progress per window.
Results are cached in paths.analysis_cache_dir() keyed by (path, size, mtime, schema).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from . import cachegc, classify, colorimetry, idet, osdep, paths, telecine
from . import ffprobe as ffprobe_mod
from .errors import ErrorCode, InputError, QtgmcError
from .events import Emitter
from .models import ANALYSIS_SCHEMA, AnalysisResult, AnalysisWarning, IdetWindow, Preset, ProbeInfo

STAGES = ("probe", "idet", "fieldmatch", "classify")
CACHE_VERSION = 3  # bump when the scan/classification logic changes in a way that invalidates caches

VFR_CODE = "variable_frame_rate"
VFR_RATE_TOLERANCE = 0.01  # r_frame_rate vs avg_frame_rate
VFR_SPAN_TOLERANCE = 0.02  # frames/span of an idet window vs nominal fps
VFR_MIN_SPAN_S = 1.0
VFR_HINT = (
    "Convert the source to a constant frame rate first (e.g. ffmpeg -fps_mode cfr -r <rate>), "
    "or expect an averaged, non-standard output rate and slightly uneven motion timing."
)


@dataclass(frozen=True)
class ScanParams:
    full_scan: bool = False
    windows: int | None = None
    window_frames: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {"full_scan": self.full_scan, "windows": self.windows, "window_frames": self.window_frames}


def _default_preset(width: int, height: int) -> Preset:
    try:
        from . import qtgmc

        p = qtgmc.default_preset(width, height)
        if isinstance(p, Preset):
            return p
        return Preset(str(p))
    except (NotImplementedError, ImportError, ValueError, AttributeError):
        if height <= 576:
            return Preset.SLOWER
        if height <= 720:
            return Preset.MEDIUM
        return Preset.FAST


def analyze(
    path: str,
    *,
    emitter: Emitter | None = None,
    full_scan: bool = False,
    windows: int | None = None,
    window_frames: int | None = None,
    use_cache: bool = True,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> AnalysisResult:
    """Full analysis of ``path`` (see module docstring); raises InputError/ComponentError."""
    em = emitter or Emitter()
    scan = ScanParams(full_scan=full_scan, windows=windows, window_frames=window_frames)
    t0 = time.monotonic()
    ffprobe_mod.check_input_exists(path)

    if use_cache:
        cached = load_cached(path, scan=scan)
        if cached is not None:
            em.stage("probe", 1, len(STAGES), "analysis (cached)")
            em.log("app", f"analysis loaded from cache for {path}")
            return cached

    em.stage("probe", 1, len(STAGES), "probing with ffprobe")
    info = ffprobe_mod.probe(path, ffprobe=ffprobe)
    tool_versions = {"ffmpeg": ffprobe_mod.ffmpeg_version(ffmpeg)}
    v = info.video

    plan = idet.plan_windows(
        v.nb_frames, v.duration_s or info.duration_s, float(v.fps),
        windows=windows, window_frames=window_frames, full_scan=full_scan,
    )
    sampled_all = len(plan) == 1 and plan[0][2] == 0
    em.stage("idet", 2, len(STAGES), "scanning the whole file with idet" if sampled_all else f"idet on {len(plan)} windows")
    tw = time.monotonic()

    def _progress(done: int, total: int, stage: str = "idet") -> None:
        el = time.monotonic() - tw
        rate = done / el if el > 0 else 0.0
        eta = (total - done) / rate if rate > 0 else None
        em.progress(stage, done, total, rate, eta, el)

    _progress(0, len(plan))
    try:
        wins = idet.analyze_windows(path, plan, ffmpeg=ffmpeg, progress=_progress)
    except QtgmcError:
        raise
    except Exception as e:  # pragma: no cover - defensive
        raise InputError(ErrorCode.ANALYSIS_FAILED, f"idet analysis failed: {e}", "", {"path": path}) from e

    fieldmatch_run = False

    def _confirm(ws: list) -> None:
        nonlocal fieldmatch_run, tw
        suspects = [w for w in ws if telecine.is_suspect(w)]
        if not suspects:
            return
        em.stage("fieldmatch", 3, len(STAGES), f"telecine confirmation on {len(suspects)} window(s)")
        tw = time.monotonic()
        telecine.confirm_windows(path, ws, ffmpeg=ffmpeg, progress=lambda d, t: _progress(d, t, "fieldmatch"))
        fieldmatch_run = True

    _confirm(wins)

    # A whole-file window with detectable content but no clear verdict (e.g. a telecined
    # part followed by an interlaced part) is re-scanned in sections so the parts separate.
    if sampled_all and len(wins) == 1 and _needs_sections(wins[0]):
        sections = idet.plan_sections(wins[0].multi.total, float(v.fps))
        if len(sections) > 1:
            em.stage("idet", 2, len(STAGES), f"re-scanning in {len(sections)} sections (mixed content?)")
            tw = time.monotonic()
            wins = idet.analyze_windows(path, sections, ffmpeg=ffmpeg, progress=_progress)
            sampled_all = False
            _confirm(wins)

    em.stage("classify", 4, len(STAGES), "classifying")
    classification, recommendation = classify.classify_file(wins, info)
    for warn in classification.warnings:
        em.warning("analysis", warn)
    source_warnings: list[AnalysisWarning] = []
    vfr = vfr_warning(info, wins)
    if vfr is not None:
        source_warnings.append(vfr)
        em.warning(vfr.code, vfr.message, vfr.hint)

    result = AnalysisResult(
        schema=ANALYSIS_SCHEMA,
        input=path,
        probe=info,
        windows=wins,
        totals_single=_sum_counts([w.single for w in wins]),
        totals_multi=_sum_counts([w.multi for w in wins]),
        totals_repeated=_sum_repeats([w.repeated for w in wins]),
        fieldmatch_run=fieldmatch_run,
        classification=classification,
        recommendation=recommendation,
        colorimetry=colorimetry.effective_colorimetry(v),
        preset_default=_default_preset(v.width, v.height),
        frames_out_double=(2 * v.nb_frames) if v.nb_frames else None,
        fps_out_double=Fraction(v.fps) * 2,
        sampled_all_frames=sampled_all,
        took_s=round(time.monotonic() - t0, 3),
        tool_versions=tool_versions,
        warnings=source_warnings,
    )
    if use_cache:
        store_cached(result, scan=scan)
    return result


def vfr_warning(info: ProbeInfo, wins: list[IdetWindow]) -> AnalysisWarning | None:
    """Cheap variable-frame-rate / dropped-frame detection: ffprobe's r_frame_rate vs
    avg_frame_rate, and each idet window's frames/timeline span vs the nominal rate."""
    v = info.video
    fps = float(v.fps) if v.fps and v.fps > 0 else 0.0
    if fps <= 0:
        return None
    evidence: list[str] = []
    if v.avg_fps:
        avg = float(v.avg_fps)
        if abs(avg - fps) / fps > VFR_RATE_TOLERANCE:
            evidence.append(f"ffprobe r_frame_rate {v.fps} vs avg_frame_rate {v.avg_fps} ({avg:.3f} fps)")
    for w in wins:
        if not w.span_s or w.span_s < VFR_MIN_SPAN_S or not w.span_frames:
            continue
        implied = w.span_frames / w.span_s
        if abs(implied - fps) / fps > VFR_SPAN_TOLERANCE:
            evidence.append(
                f"window at {w.start_s:.1f} s: {w.span_frames} frames span {w.span_s:.2f} s ({implied:.2f} fps, nominal {fps:.3f})"
            )
    if not evidence:
        return None
    return AnalysisWarning(
        VFR_CODE,
        "frame timing is not constant (variable frame rate or dropped frames): " + "; ".join(evidence),
        VFR_HINT,
    )


def _needs_sections(w) -> bool:
    c = w.multi
    if telecine.is_suspect(w):
        # A weaker-than-3:2 signature, or one fieldmatch could not clean, may be a telecined
        # part next to an interlaced part: sections separate them (mixed vs telecined).
        return not (telecine.is_strong(w) and telecine.confirmation_clean(w) is not False)
    if c.total <= 0 or c.determined / c.total < classify.MIN_DETERMINED_SHARE:
        return False  # static / smooth content: sections would not help
    return classify.window_verdict(w).kind == "undetermined"


def _sum_counts(items):
    from .models import IdetCounts

    out = IdetCounts()
    for c in items:
        out.tff += c.tff
        out.bff += c.bff
        out.progressive += c.progressive
        out.undetermined += c.undetermined
    return out


def _sum_repeats(items):
    from .models import RepeatCounts

    out = RepeatCounts()
    for r in items:
        out.neither += r.neither
        out.top += r.top
        out.bottom += r.bottom
    return out


# --------------------------------------------------------------------------- cache
def cache_key(path: str) -> str:
    """sha256 of (absolute path, size, mtime_ns, schema, cache version); stable across runs."""
    apath = os.path.abspath(path)
    try:
        st = os.stat(apath)
        size, mtime = st.st_size, st.st_mtime_ns
    except OSError:
        size, mtime = -1, -1
    blob = json.dumps([apath, size, mtime, ANALYSIS_SCHEMA, CACHE_VERSION], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8", "surrogateescape")).hexdigest()


def cache_path(path: str) -> Path:
    return paths.analysis_cache_dir() / f"{cache_key(path)}.json"


def load_cached(path: str, *, scan: ScanParams | None = None) -> AnalysisResult | None:
    """Cached result for ``path`` (same size/mtime) analysed with the same scan parameters, else None."""
    scan = scan or ScanParams()
    # surrogateescape: file names that are not valid UTF-8 round-trip through the JSON
    p = cache_path(path)
    doc = osdep.load_json(p, errors="surrogateescape")
    if not isinstance(doc, dict) or doc.get("schema") != ANALYSIS_SCHEMA:
        return None
    if doc.get("_scan") != scan.to_dict():
        return None
    try:
        result = AnalysisResult.from_dict(doc)
    except Exception:
        return None
    cachegc.touch(p)  # LRU by mtime
    return result


def store_cached(result: AnalysisResult, *, scan: ScanParams | None = None) -> None:
    scan = scan or ScanParams()
    doc = result.to_dict()
    doc["_scan"] = scan.to_dict()
    p = cache_path(result.input)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        osdep.atomic_write_text(p, json.dumps(doc, ensure_ascii=False, indent=1), errors="surrogateescape")
    except (OSError, ValueError):
        # cache is best effort: never fail an analysis because ~/.cache is unwritable
        # (ValueError covers UnicodeEncodeError for un-encodable content)
        pass
    cachegc.prune_analysis_cache()
