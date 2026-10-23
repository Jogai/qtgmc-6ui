"""Master pipeline (WP-B): analysis -> graph -> in-process y4m -> ffmpeg stdin -> master.mkv.

Execution model (verified): ``graph.clip.output(ffmpeg.stdin, y4m=True, progress_update=cb)``
in a worker thread; the main thread owns the CancelToken (SIGTERM/SIGINT/stdin
"cancel").  Cancel = SIGTERM to ffmpeg -> output() raises BrokenPipeError ->
partial file removed -> CancelledError.  ffmpeg stderr is drained on a thread
and forwarded as log events (level warning/error by prefix).

ffmpeg writes ``<output>.partial``; it is renamed onto the output only after the
verification passed, so a failed or cancelled run never leaves a half master and an
existing master survives a failed ``--overwrite`` run.  The input file is only ever
opened for reading (bestsource, ffmpeg -i); outputs are refused when they resolve to
the input path (same inode included).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import audiomodes, qtgmc, runtime, vsenv
from . import graph as graph_mod
from . import master as master_mod
from .errors import (
    BlockedError,
    CancelledError,
    EncodeError,
    ErrorCode,
    InputError,
    OutputError,
)
from .events import Emitter, ProgressTracker
from .models import (
    AnalysisResult,
    ChromaMode,
    Colorimetry,
    FieldOrderChoice,
    JobResult,
    JobSpec,
    MasterSettings,
    OutputFile,
    RecommendedAction,
)
from .runtime import CancelToken

KILL_GRACE_S = runtime.KILL_GRACE_S
PROBE_TIMEOUT_S = 600.0  # ffprobe of a finished master (packet count of a long file)
PROGRESS_INTERVAL_S = 0.25
STDERR_TAIL = 50
PARTIAL_SUFFIX = ".partial"


def partial_path(output: str) -> str:
    """Hidden temp name next to the output: ``<dir>/.<name>.partial``."""
    d, n = os.path.split(output)
    return os.path.join(d, f".{n}{PARTIAL_SUFFIX}")
AV_OFFSET_TOLERANCE_S = 0.005  # matroska timestamps are 1 ms
AUDIO_DURATION_TOLERANCE_S = 0.06  # one lossy codec frame (AAC 1024 samples, Opus 20 ms) plus priming

_BLOCK_CODES = {
    RecommendedAction.BLOCK_TELECINED: ErrorCode.BLOCKED_TELECINED,
    RecommendedAction.BLOCK_MIXED: ErrorCode.BLOCKED_MIXED,
    RecommendedAction.SKIP_PROGRESSIVE: ErrorCode.BLOCKED_PROGRESSIVE,
}


def check_blocked(analysis: AnalysisResult, settings: MasterSettings, force: bool) -> None:
    """Raise BlockedError (BLOCKED_TELECINED/MIXED/PROGRESSIVE/UNDETERMINED) unless ``force``
    or, for UNDETERMINED, an explicit field order is given."""
    rec = analysis.recommendation
    explicit = settings.field_order != FieldOrderChoice.AUTO
    if rec.action == RecommendedAction.CONFIRM_FIELD_ORDER and not explicit:
        raise BlockedError(
            ErrorCode.BLOCKED_UNDETERMINED,
            rec.message or "the field order could not be determined",
            "check the fields preview and pass --field-order tff|bff (and --force if the content is not interlaced)",
            {"classification": analysis.classification.content.value},
        )
    if force:
        if analysis.classification.field_order is None and not explicit:
            raise BlockedError(
                ErrorCode.BLOCKED_UNDETERMINED,
                "--force needs an explicit field order for this content",
                "pass --field-order tff|bff",
                {"classification": analysis.classification.content.value},
            )
        return
    code = _BLOCK_CODES.get(rec.action)
    if code is not None:
        raise BlockedError(
            code,
            rec.message or f"analysis recommends {rec.action.value}",
            "use --force to deinterlace anyway (not recommended for telecined/mixed content: it needs IVTC)",
            {"classification": analysis.classification.content.value, "action": rec.action.value},
        )


def master_output_path(job: JobSpec) -> str:
    """``<output_dir>/<name_template formatted with stem>.mkv`` unless job.master_output."""
    if job.master_output:
        return job.master_output
    inp = Path(job.input)
    out_dir = Path(job.output_dir) if job.output_dir else inp.parent
    name = (job.name_template or "{stem}.deint").format(stem=inp.stem)
    return str(out_dir / f"{name}.mkv")


def _is_input(path: str, inp: str) -> bool:
    if os.path.abspath(path) == os.path.abspath(inp):
        return True
    try:
        if os.path.exists(path) and os.path.samefile(path, inp):
            return True
        return Path(path).resolve() == Path(inp).resolve()
    except OSError:
        return False


def _check_output(output: str, inp: str, overwrite: bool) -> None:
    op = Path(output)
    partial = partial_path(output)
    if _is_input(output, inp) or _is_input(partial, inp):
        raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, "the output path is the input file", "choose another output path", {"path": output})
    if op.exists() and not overwrite:
        raise OutputError(ErrorCode.OUTPUT_EXISTS, f"output exists: {output}", "use --overwrite or another name", {"path": output})
    if op.exists() and op.is_dir():
        raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"output is a directory: {output}", "", {"path": output})
    try:
        op.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"cannot create {op.parent}: {e}", "", {"path": str(op.parent)}) from e
    if not os.access(op.parent, os.W_OK):
        raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"directory not writable: {op.parent}", "", {"path": str(op.parent)})
    if os.path.lexists(partial):  # stale partial of a killed run: ours by construction
        _remove(partial)


def _remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _level(line: str) -> str:
    low = line.lower()
    if "error" in low or "invalid" in low or "failed" in low:
        return "error"
    if "warning" in low or "deprecated" in low:
        return "warning"
    return "info"


class _Encoder:
    """ffmpeg child (runtime.StreamingProcess: own process group, tracked for cancel, dies with
    this process) fed with y4m on stdin; stderr lines become log events, stats lines are kept."""

    def __init__(self, cmd: list[str], emitter: Emitter, cancel: CancelToken) -> None:
        self.cmd = cmd
        self.emitter = emitter
        self.cancel = cancel
        self.last_stats: dict[str, str] = {}
        env = runtime.subprocess_env()
        env.setdefault("AV_LOG_FORCE_NOCOLOR", "1")
        self.sp = runtime.StreamingProcess(cmd, stdin=subprocess.PIPE, on_stderr_line=self._line, env=env)
        self.proc = self.sp.proc

    @property
    def tail(self) -> list[str]:
        return list(self.sp.tail)

    def _line(self, line: str) -> None:
        stats = master_mod.parse_ffmpeg_progress(line)
        if stats:
            self.last_stats = stats
            return
        try:
            self.emitter.log("ffmpeg", line, _level(line))
        except (OSError, ValueError):  # event stdout gone (GUI died): stop the run cleanly
            self.cancel.cancel("event pipe broken")

    def terminate(self) -> int:
        self._close_stdin()
        if self.proc.poll() is None:
            self.sp.terminate(KILL_GRACE_S)
        return int(self.sp.wait().returncode or 0)

    def _close_stdin(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass

    def finish(self) -> int:
        self._close_stdin()
        return int(self.sp.wait().returncode)


def _probe(path: str, *, count_packets: bool, ffprobe: str = "ffprobe") -> dict[str, Any]:
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    if count_packets:
        cmd.append("-count_packets")
    cmd.append(path)
    r = runtime.run_capture(cmd, timeout=PROBE_TIMEOUT_S)
    if r.returncode != 0:
        raise EncodeError(ErrorCode.ENCODE_FAILED, f"ffprobe cannot read {path}: {r.stderr.strip()[-500:]}", "", {"path": path})
    return json.loads(r.stdout or "{}")


def _probe_output(path: str, ffprobe: str = "ffprobe") -> dict[str, Any]:
    return _probe(path, count_packets=True, ffprobe=ffprobe)


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def source_timing(info: dict[str, Any]) -> dict[str, Any]:
    """Start offsets of a probed file: how much later than the earliest stream the first video
    stream starts (``video_offset_s``), and the video-minus-first-audio offset (``av_offset_s``)."""
    streams = info.get("streams", [])
    starts = [s for s in ((_f(s.get("start_time")), s.get("codec_type")) for s in streams) if s[0] is not None]
    fmt_start = _f(info.get("format", {}).get("start_time"))
    earliest = min([st for st, _ in starts] + ([fmt_start] if fmt_start is not None else []), default=0.0)
    video = next((st for st, kind in starts if kind == "video"), None)
    audio = next((st for st, kind in starts if kind == "audio"), None)
    video_offset = max(0.0, (video - earliest)) if video is not None else 0.0
    av = (video - audio) if (video is not None and audio is not None) else None
    return {"video_offset_s": round(video_offset, 6), "av_offset_s": None if av is None else round(av, 6)}


def _expected_pix_fmt(analysis: AnalysisResult, settings: MasterSettings) -> str:
    src = (analysis.probe.video.pix_fmt or "").replace("yuvj", "yuv")
    if src.startswith("yuv411p") and settings.chroma == ChromaMode.YUV422:
        return "yuv422p"
    return src


def _audio_md5(path: str, track: int, sample_codec: str, ffmpeg: str = "ffmpeg") -> str:
    """MD5 of one audio track decoded to ``sample_codec`` (pcm_s32le / pcm_f64le): the same for
    source and master when the master's audio is lossless."""
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", path, "-vn", "-sn", "-dn", "-map", f"0:a:{track}", "-c:a", sample_codec, "-f", "md5", "-"]
    r = runtime.run_capture(cmd, timeout=PROBE_TIMEOUT_S * 10)
    if r.returncode != 0:
        raise EncodeError(ErrorCode.ENCODE_FAILED, f"cannot decode audio track {track} of {path}: {r.stderr.strip()[-300:]}", "", {"path": path})
    return r.stdout.strip().split("=", 1)[-1]


def _stream_duration(s: dict[str, Any], info: dict[str, Any]) -> float | None:
    d = _f(s.get("duration"))
    if d is not None:
        return d
    t = (s.get("tags") or {}).get("DURATION") or (s.get("tags") or {}).get("duration")
    if t:
        try:
            h, m, sec = t.split(":")
            return int(h) * 3600 + int(m) * 60 + float(sec)
        except ValueError:
            pass
    return _f(info.get("format", {}).get("duration"))


def _verify_audio(output: str, source: str, analysis: AnalysisResult, aplan: audiomodes.AudioPlan, audio: list[dict[str, Any]],
                  info: dict[str, Any], emitter: Emitter) -> tuple[list[str], list[str]]:
    """Per track: codec as planned; lossless tracks decode bit-identically to the source
    (decoded-audio MD5), lossy tracks keep the duration within a codec frame."""
    problems: list[str] = []
    warnings: list[str] = []
    if len(audio) != len(aplan.tracks):
        problems.append(f"master has {len(audio)} audio stream(s), expected {len(aplan.tracks)}")
        return problems, warnings
    verified: list[str] = []
    for n, (a, t, src_a) in enumerate(zip(audio, aplan.tracks, analysis.probe.audio, strict=False)):
        if a.get("codec_name") != t.expect:
            problems.append(f"audio track {n} is {a.get('codec_name')}, expected {t.expect} ({aplan.mode.value})")
            continue
        if t.lossless:
            kind, _bits = audiomodes._sample_class(src_a)
            fmt = "pcm_f64le" if kind == "float" else "pcm_s32le"
            src_md5 = _audio_md5(source, n, fmt)
            out_md5 = _audio_md5(output, n, fmt)
            if src_md5 != out_md5:
                problems.append(f"audio track {n} ({t.expect}) does not decode bit-identically to the source")
            else:
                verified.append(f"#{n} {t.expect} {src_md5[:12]}")
            bits = int(a.get("bits_per_raw_sample") or 0)
            if t.codec != "copy" and src_a.bit_depth and bits and bits < src_a.bit_depth and kind != "float":
                warnings.append(f"audio stream {a.get('index')}: {src_a.bit_depth}-bit source stored as {bits}-bit {t.expect}")
        else:
            want = src_a.duration_s or _f(info.get("format", {}).get("duration"))
            have = _stream_duration(a, info)
            if want and have is not None and abs(have - want) > AUDIO_DURATION_TOLERANCE_S:
                warnings.append(f"audio track {n} ({t.expect}) lasts {have:.3f} s, the source {want:.3f} s")
    if verified:
        emitter.log("app", "audio_verified: " + ", ".join(verified) + " (decoded MD5 identical to the source)")
    return problems, warnings


def _verify_master(output: str, g: graph_mod.Graph, analysis: AnalysisResult, settings: MasterSettings,
                   colorimetry: Colorimetry, audio_tracks: int, source_info: dict[str, Any] | None,
                   emitter: Emitter, aplan: audiomodes.AudioPlan | None = None, source: str = "") -> tuple[OutputFile, list[str]]:
    info = _probe_output(output)
    streams = info.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    problems: list[str] = []
    warnings: list[str] = []
    v: dict[str, Any] = video[0] if video else {}
    if not video:
        problems.append("no video stream")
    frames = int(v.get("nb_read_packets") or v.get("nb_frames") or 0)
    if frames != g.frames_out:
        problems.append(f"frame count {frames} != {g.frames_out}")
    # matroska rounds timestamps to 1 ms, so ffprobe reports e.g. 19001/317 for 60000/1001:
    # compare with a 0.1 % tolerance (the frame count above is exact)
    want_fps = Fraction(g.fps_num, g.fps_den)
    fps = v.get("avg_frame_rate") or v.get("r_frame_rate") or ""
    try:
        have_fps = Fraction(fps) if fps and not fps.startswith("0/") else None
    except (ValueError, ZeroDivisionError):
        have_fps = None
    if have_fps is not None and abs(have_fps / want_fps - 1) > Fraction(1, 1000):
        problems.append(f"frame rate {fps} != {want_fps}")
    pv = analysis.probe.video
    want_w, want_h = (pv.width, pv.height) if pv.width and pv.height else (g.width, g.height)
    if (int(v.get("width", 0)), int(v.get("height", 0))) != (want_w, want_h):
        problems.append(f"size {v.get('width')}x{v.get('height')} != source {want_w}x{want_h}")
    sar = pv.sar
    want_sar = f"{sar.numerator}:{sar.denominator}" if sar and sar > 0 else "1:1"
    if v.get("sample_aspect_ratio", "") != want_sar:
        problems.append(f"SAR {v.get('sample_aspect_ratio')} != {want_sar}")
    if v.get("codec_name") != "ffv1":
        problems.append(f"video codec {v.get('codec_name')} != ffv1")
    want_fmt = _expected_pix_fmt(analysis, settings)
    if want_fmt and v.get("pix_fmt") and v.get("pix_fmt") != want_fmt:
        problems.append(f"pix_fmt {v.get('pix_fmt')} != source {want_fmt}")
    want_range = master_mod.normalize_range(colorimetry.range) or "tv"
    if master_mod.normalize_range(v.get("color_range", "")) != want_range:
        problems.append(f"color_range {v.get('color_range') or 'unknown'} != source {want_range}")
    for key, want in (("color_space", colorimetry.matrix), ("color_primaries", colorimetry.primaries), ("color_transfer", colorimetry.transfer)):
        if want not in master_mod._UNKNOWN and v.get(key) != want:
            warnings.append(f"master {key} is {v.get(key) or 'unknown'}, expected {want}")
    if g.chroma_location and v.get("chroma_location", "") not in ("", "unspecified", g.chroma_location):
        warnings.append(f"master chroma_location is {v.get('chroma_location')}, source {g.chroma_location}")
    if aplan is not None and audio_tracks != 0 and source:
        ap, aw = _verify_audio(output, source, analysis, aplan, audio, info, emitter)
        problems += ap
        warnings += aw
    else:
        want_audio = len(analysis.probe.audio) if audio_tracks < 0 else audio_tracks
        if len(audio) != want_audio:
            warnings.append(f"master has {len(audio)} audio stream(s), the source has {want_audio}")
    if source_info is not None and audio:
        want_av = source_timing(source_info)["av_offset_s"]
        have_av = source_timing(info)["av_offset_s"]
        if want_av is not None and have_av is not None and abs(want_av - have_av) > AV_OFFSET_TOLERANCE_S:
            warnings.append(f"A/V start offset is {have_av:+.3f} s in the master, {want_av:+.3f} s in the source")
    if problems:
        raise EncodeError(
            ErrorCode.ENCODE_FAILED,
            "master verification failed: " + "; ".join(problems),
            "the output does not match the source; please report this with `qtgmc-6ui doctor` output",
            {"path": output, "problems": problems},
        )
    for w in warnings:
        emitter.warning("verify", w)
    dur = float(info.get("format", {}).get("duration") or 0.0)
    size = int(info.get("format", {}).get("size") or os.path.getsize(output))
    return OutputFile("master", output, None, frames, dur, size), warnings


def effective_settings(analysis: AnalysisResult, settings: MasterSettings) -> MasterSettings:
    fo = graph_mod.resolve_field_order(analysis, settings)
    preset = graph_mod.resolve_preset(analysis, settings)
    v = analysis.probe.video
    return replace(
        settings,
        field_order=FieldOrderChoice(fo.value),
        preset=preset,
        cache_mb=settings.cache_mb or vsenv.default_cache_mb(v.width, v.height),
    )


def run_master(
    job: JobSpec,
    analysis: AnalysisResult,
    output: str,
    *,
    emitter: Emitter,
    cancel: CancelToken,
    stage_offset: int = 0,
    stage_total: int = 3,
    audio_tracks: int = -1,
) -> OutputFile:
    """Deinterlace ``job.input`` into ``output`` (ffv1 + the chosen audio mode, mkv).  Emits stage setup/master
    with per-frame progress, verifies the result with ffprobe (frames, fps, SAR, colour)."""
    settings = job.master
    _check_output(output, job.input, job.overwrite)
    partial = partial_path(output)
    eff = effective_settings(analysis, settings)
    for w in qtgmc.fidelity_warnings(eff):
        emitter.warning("fidelity", w)
    aplan = audiomodes.plan(eff.audio, analysis.probe.audio) if audio_tracks != 0 else audiomodes.AudioPlan(eff.audio)
    for code, msg, hint in aplan.warnings:
        emitter.warning(code, msg, hint)

    emitter.stage("setup", stage_offset + 1, stage_total, "setup: VapourSynth core, plugins, source index, QTGMC graph")
    owned = vsenv.current() is None
    vc = vsenv.current() or vsenv.open_core(threads=eff.threads, cache_mb=eff.cache_mb)
    t_start = time.monotonic()
    worker: threading.Thread | None = None
    try:
        g = graph_mod.build(vc, analysis, eff)
        for w in g.warnings:
            emitter.warning("vfr" if "frame rate" in w or "frames" in w else "source", w)
        colorimetry = analysis.colorimetry
        src_range = master_mod.normalize_range(colorimetry.range)
        if g.color_range and src_range and g.color_range != src_range:
            emitter.warning("colorimetry", f"the decoder reports {g.color_range} range, the analysis {src_range}; using {g.color_range}")
        if g.color_range:
            colorimetry = replace(colorimetry, range=g.color_range)
        emitter.log("app", f"field order {g.field_order.value}, preset {g.preset.value}, {g.frames_out} frames @ {g.fps_num}/{g.fps_den}, "
                           f"{g.format_name}, range {colorimetry.range}, chroma location {g.chroma_location or 'unknown'}")
        if cancel.cancelled:
            raise CancelledError(removed=[], kept=[])

        source_info = _probe(job.input, count_packets=False)
        timing = source_timing(source_info)
        if timing["video_offset_s"] > 0:
            emitter.log("app", f"video starts {timing['video_offset_s']:.3f} s after the earliest stream; keeping the offset")
        cmd = master_mod.master_command(job.input, partial, analysis.probe.video.sar, colorimetry, overwrite=True,
                                        audio_tracks=audio_tracks, chroma_location=g.chroma_location,
                                        video_offset_s=timing["video_offset_s"], audio_args=aplan.ffmpeg_args())
        for t in aplan.tracks:
            emitter.log("app", f"audio track #{t.index}: {t.source_codec} -> {t.codec}" + (f" ({t.note})" if t.note else ""))
        emitter.log("app", "ffmpeg command: " + shlex.join(cmd))
        audio_label = ", ".join(sorted({t.expect for t in aplan.tracks})) or "no audio"
        emitter.stage("master", stage_offset + 2, stage_total, f"deinterlace + encode master (ffv1 + {audio_label})")
        enc = _Encoder(cmd, emitter, cancel)
        tracker = ProgressTracker(g.frames_out)
        state: dict[str, Any] = {"last": 0.0, "done": 0, "exc": None}
        t_enc = time.monotonic()

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            state["done"] = done
            if done == total or now - state["last"] >= PROGRESS_INTERVAL_S:
                state["last"] = now
                fps, eta, elapsed = tracker.update(done)
                try:
                    emitter.progress("master", done, total, fps, eta, elapsed)
                except (OSError, ValueError):  # event stdout gone (GUI died): cancel + clean up
                    cancel.cancel("event pipe broken")

        def feed() -> None:
            try:
                g.clip.output(enc.proc.stdin, y4m=True, progress_update=progress)
            except BaseException as e:  # BrokenPipeError on cancel/ffmpeg death, vs.Error on filter failure
                state["exc"] = e

        worker = threading.Thread(target=feed, name="vs-output", daemon=True)
        worker.start()
        cancelled = False
        while worker.is_alive():
            worker.join(0.2)
            if cancel.cancelled and worker.is_alive():
                cancelled = True
                enc.terminate()  # closes the pipe: output() fails on its next write
                worker.join()  # must finish before the core is destroyed
                break
        if cancelled or cancel.cancelled:
            enc.terminate()
            removed = [partial] if _remove(partial) else []
            raise CancelledError(removed=removed, kept=[])
        exc = state["exc"]
        rc = enc.terminate() if exc is not None else enc.finish()
        if exc is not None and not isinstance(exc, BrokenPipeError):
            _remove(partial)
            raise EncodeError(
                ErrorCode.ENCODE_FAILED,
                f"VapourSynth failed while producing frame {state['done']}: {exc}",
                "run `qtgmc-6ui doctor`; if every plugin loads, report this with the log",
                {"frame": state["done"], "error": str(exc), "ffmpeg_tail": list(enc.tail)},
            ) from exc
        if rc != 0 or exc is not None:
            _remove(partial)
            code = ErrorCode.PIPE_BROKEN if exc is not None and rc == 0 else ErrorCode.ENCODE_FAILED
            raise EncodeError(
                code,
                f"ffmpeg exited with status {rc} after {state['done']} frames",
                "check the ffmpeg log (the last lines are in the error details); run `qtgmc-6ui doctor` if ffmpeg itself seems broken",
                {"returncode": rc, "frame": state["done"], "ffmpeg_tail": list(enc.tail)},
            )
        enc_elapsed = time.monotonic() - t_enc
        emitter.progress("master", g.frames_out, g.frames_out, g.frames_out / enc_elapsed if enc_elapsed else 0.0, 0.0, enc_elapsed)
        emitter.log("app", f"encoded {g.frames_out} frames in {enc_elapsed:.1f} s ({g.frames_out / max(enc_elapsed, 1e-6):.1f} fps)")

        emitter.stage("verify", stage_offset + 3, stage_total, "verify master with ffprobe")
        try:
            out, _ = _verify_master(partial, g, analysis, eff, colorimetry, audio_tracks, source_info, emitter, aplan=aplan, source=job.input)
        except EncodeError:
            _remove(partial)
            raise
        if cancel.cancelled:
            removed = [partial] if _remove(partial) else []
            raise CancelledError(removed=removed, kept=[])
        try:
            os.replace(partial, output)
        except OSError as e:
            _remove(partial)
            raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"cannot move the master into place: {e}", "", {"path": output}) from e
        out.path = output
        emitter.log("app", f"master ok: {out.frames} frames, {out.duration_s:.2f} s, {out.size_bytes} bytes, total {time.monotonic() - t_start:.1f} s")
        return out
    finally:
        if worker is not None and worker.is_alive():
            worker.join()
        if owned:
            vc.close()


def run_job(job: JobSpec, *, emitter: Emitter, cancel: CancelToken) -> JobResult:
    """analyze (unless job.analysis) -> check_blocked -> run_master -> viewcopy.run_view for each
    view -> JobResult.  Views always read from the master, never from the original."""
    t0 = time.monotonic()
    if not os.path.isfile(job.input):
        raise InputError(ErrorCode.INPUT_NOT_FOUND, f"input not found: {job.input}", "", {"path": job.input})
    analysis = job.analysis
    if analysis is None:
        from .analysis import analyze

        analysis = analyze(job.input, emitter=emitter)
    check_blocked(analysis, job.master, job.force)
    eff = effective_settings(analysis, job.master)
    v = analysis.probe.video
    kwargs = qtgmc.build_kwargs(eff, eff.preset, v.width, v.height)  # type: ignore[arg-type]
    result = JobResult(input=job.input, analysis=analysis, effective_master=eff, qtgmc_kwargs=kwargs)
    result.warnings.extend(qtgmc.fidelity_warnings(eff))
    result.audio_tracks = audiomodes.plan(eff.audio, analysis.probe.audio).tracks  # validates: coded error before any output

    from . import jobs as jobs_mod

    keep_master = job.keep_master or job.skip_master
    for p in jobs_mod.remove_stale_temp_masters(job.output_dir or str(Path(job.input).parent)):
        emitter.log("app", f"removed a temporary master left by an interrupted run: {p}")
    if not keep_master:
        jobs_mod.check_master_choice(job)
        master_path = jobs_mod.temp_master_path(job)
    else:
        master_path = master_output_path(job)
    try:
        return _run_stages(job, analysis, result, master_path, keep_master, emitter=emitter, cancel=cancel, t0=t0)
    finally:
        if not keep_master:  # success, error or cancel: the temporary master never survives
            _remove(master_path)
            _remove(partial_path(master_path))


def _run_stages(job: JobSpec, analysis: AnalysisResult, result: JobResult, master_path: str, keep_master: bool, *,
                emitter: Emitter, cancel: CancelToken, t0: float) -> JobResult:
    stage_total = 3 + len(job.views)
    kept: list[str] = []
    master_frames: int | None = None
    if job.skip_master:
        if not os.path.isfile(master_path):
            raise InputError(ErrorCode.INPUT_NOT_FOUND, f"master not found: {master_path}", "skip_master needs an existing master", {"path": master_path})
        emitter.log("app", f"using existing master {master_path}")
    else:
        collect = _CollectWarnings(emitter)
        mfile = run_master(job, analysis, master_path, emitter=collect, cancel=cancel, stage_total=stage_total)
        result.warnings.extend(collect.lines)
        master_frames = mfile.frames
        if keep_master:
            result.outputs.append(mfile)
            kept.append(master_path)
    if job.views:
        from . import ffprobe, profiles, viewcopy

        mprobe = ffprobe.probe(master_path)
        out_dir = job.output_dir or str(Path(job.input).parent)
        for i, view in enumerate(job.views):
            profile = profiles.load(view.profile)
            out_path = view.output or viewcopy.view_output_path(job.input, profile, job.name_template, out_dir)
            emitter.stage("view", 4 + i, stage_total, f"viewing copy: {profile.name or profile.id}")
            try:
                vfile = viewcopy.run_view(master_path, mprobe, profile, out_path, emitter=emitter, cancel=cancel, overwrite=job.overwrite)
            except CancelledError as e:
                raise CancelledError(removed=list(e.details.get("removed", [])), kept=kept + list(e.details.get("kept", []))) from None
            result.outputs.append(vfile)
            kept.append(out_path)
    result.elapsed_s = time.monotonic() - t0
    frames = master_frames
    if frames and result.elapsed_s:
        result.average_fps = round(frames / result.elapsed_s, 2)
    return result


class _CollectWarnings(Emitter):
    """Emitter wrapper that forwards everything and keeps the warning texts for JobResult."""

    def __init__(self, inner: Emitter) -> None:
        super().__init__(inner.sink)
        self.t0 = inner.t0
        self.lines: list[str] = []

    def warning(self, code: str, message: str, hint: str = "") -> None:
        if code not in ("fidelity",):  # fidelity warnings are already in JobResult.warnings
            self.lines.append(message)
        super().warning(code, message, hint)
