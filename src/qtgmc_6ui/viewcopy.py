"""Viewing-copy encoders (WP-C): from the MASTER, never from the original.

Backends:
  ffmpeg        ffmpeg -i MASTER [-vf framestep/scale/setsar/setparams] -c:v ENC <rate control>
                [-pix_fmt] -c:a ENC ... -f CONTAINER OUT                        (one process)
  x264/x265/kvazaar (CLI)
                1. ffmpeg -i MASTER -map 0:v:0 [-vf framestep/scale] -r FPS -strict -1 -pix_fmt .. -f yuv4mpegpipe -
                   | TOOL (y4m from stdin, --sar, --colorprim/--transfer/--colormatrix/--range)
                   x264 writes a temporary mp4 itself (``--muxer mp4``: exact CFR timestamps);
                   x265/kvazaar write a raw .hevc elementary stream
                2. (x265/kvazaar only) esmux.es_to_ts: the raw stream gets real PTS/DTS in a temporary
                   MPEG-TS (ffmpeg 9 cannot stream-copy a raw B-frame stream in display order; see esmux.py)
                3. ffmpeg -i <tmp.mp4 | -f mpegts tmp.ts> -i MASTER -map 0:v:0 -map 1:a? -c:v copy -c:a ENC ... OUT
  sox audio chain (optional, before the audio encoder), one step per audio track:
                ffmpeg -i MASTER -map 0:a:N -f sox - | sox -t sox - -b BITS -t w64 OUT.aN.w64 EFFECTS
                then the final mux takes the .w64 files instead of the master's audio (global and
                per-track metadata still come from the master via -map_metadata).
                (A temp Wave64 per track rather than a pipe: multi-track masters stay simple, no 4 GB
                limit, the step is cancel-safe; BITS = the master track's bit depth, so "dither" is
                meaningful.)
Scaling: square pixels = width*SAR rounded to even (height untouched: 720x576 16:15 -> 768x576,
1440x1080 4:3 -> 1920x1080, 720x480 8:9 -> 640x480), after deinterlacing (the master is already
deinterlaced).  Single rate from a double-rate master: ``framestep=2`` (keeps the first field's
frame of each pair, halves the frame rate exactly, 60000/1001 -> 30000/1001).  Timestamps are
regenerated (``setpts=N/(FPS)/TB`` + ``-r FPS`` with FPS snapped to the standard rate) because a
Matroska master carries 1 ms-rounded timestamps (59.94 reads as 19001/317); mp4 output then has
an exact CFR timescale.  Colour tags: copied from the master via setparams in the filter chain
(ffmpeg backend) or the tool's VUI flags (CLI backends); HEVC in mp4/mov is tagged hvc1.
Progress: ``frame=`` stats of the ffmpeg process that decodes the master (per 0.5 s); multi-step
plans report one continuous count (encode, timestamps, mux) over the sum of the steps.
Cancel: GRACEFUL stop of every child tree of the running step, FORCE after 5 s (osdep.stop_trees),
partial output and temp files removed, CancelledError(removed=[...]).  Children are started with
osdep.popen_kwargs() (own process group, die with the CLI) and registered with runtime.track(),
so a killed CLI never leaves an orphan encoder.  The copy is written to a hidden ``.<name>.partial``
file and renamed to its final name only after the ffprobe verification; a stale partial of the
same output is removed before the next attempt.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from . import encoders, esmux, muxmatrix, osdep, profiles, runtime
from .errors import CancelledError, ComponentError, EncodeError, ErrorCode, OutputError, UsageError
from .events import Emitter, ProgressTracker
from .models import (
    Colorimetry,
    EncoderInfo,
    MuxVerdict,
    OutputFile,
    ProbeInfo,
    Profile,
    RateControl,
    ScaleMode,
    ScaleSettings,
    VideoBackend,
    VideoProbe,
    VideoSettings,
    ViewRateMode,
)
from .runtime import CancelToken

DOUBLE_RATE_MIN_FPS = 48.0  # a master at >= 48 fps is a bobbed (double-rate) master
STATS_PERIOD = "0.5"
KILL_GRACE_S = runtime.KILL_GRACE_S
_TOOL_OF = {VideoBackend.X264_CLI: "x264", VideoBackend.X265_CLI: "x265", VideoBackend.KVAZAAR_CLI: "kvazaar"}
_UNKNOWN = {"", "unknown", "unspecified", "reserved"}
_FRAME_RE = re.compile(r"frame=\s*(\d+)")
_MP4_FAMILY = {"mp4", "mov", "ipod", "psp", "3gp", "3g2", "ismv", "f4v"}
STANDARD_RATES = (Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30), Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60), Fraction(100), Fraction(120000, 1001), Fraction(120))
# input formats the CLI tools accept (y4m), per tool; others are mapped by tool_pix_fmt()
_X264_FORMATS = ("yuv420p", "yuv422p", "yuv444p", "yuv420p10le", "yuv422p10le", "yuv444p10le", "gray", "gray10le")
_X265_FORMATS = _X264_FORMATS + ("yuv420p12le", "yuv422p12le", "yuv444p12le", "gray12le")
_KVAZAAR_FORMATS = ("yuv420p",)


# --------------------------------------------------------------------------- plan
@dataclass
class Step:
    label: str  # encode | wrap | mux | sox
    cmds: list[list[str]]  # piped left to right (stdout -> stdin)
    progress_index: int = -1  # index of the cmd whose stderr carries ffmpeg "frame=" stats
    total_frames: int | None = None
    creates: list[str] = field(default_factory=list)  # files written by this step
    func: Callable[[Emitter, CancelToken, int, int | None], None] | None = None  # in-process step (emitter, cancel, progress offset, grand total)
    progress_offset: int = 0  # frames done by earlier steps (continuous progress across steps)


@dataclass
class ViewPlan:
    output: str
    steps: list[Step]
    temps: list[str]
    width: int
    height: int
    sar: Fraction
    fps: Fraction
    total_frames: int | None
    tools: list[str]  # external tools used (ffmpeg always)


def _frac(f: Fraction) -> str:
    return f"{f.numerator}/{f.denominator}"


def _even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def is_double_rate(fps: Fraction) -> bool:
    return float(fps) >= DOUBLE_RATE_MIN_FPS


def snap_fps(fps: Fraction) -> Fraction:
    """The exact standard rate within 0.1 % of ``fps`` (Matroska rounds timestamps to 1 ms, so a
    59.94 master probes as 19001/317); other rates are returned unchanged."""
    for std in STANDARD_RATES:
        if abs(float(fps) - float(std)) <= float(std) * 0.001:
            return std
    return fps


def tool_pix_fmt(backend: VideoBackend, pix_fmt: str) -> str:
    """Nearest y4m format the CLI tool accepts (4:1:1 -> 4:2:2, 4:1:0/4:4:0 -> 4:2:0, depth clamped)."""
    allowed = {VideoBackend.X264_CLI: _X264_FORMATS, VideoBackend.X265_CLI: _X265_FORMATS, VideoBackend.KVAZAAR_CLI: _KVAZAAR_FORMATS}.get(backend, _X264_FORMATS)
    if pix_fmt in allowed:
        return pix_fmt
    if backend == VideoBackend.KVAZAAR_CLI:
        return "yuv420p"
    depth = bit_depth(pix_fmt)
    depths = (8, 10, 12) if backend == VideoBackend.X265_CLI else (8, 10)
    depth = max(d for d in depths if d <= max(depth, 8))
    if pix_fmt.startswith("gray"):
        base = "gray"
    elif "411" in pix_fmt or "422" in pix_fmt:
        base = "yuv422p"
    elif "444" in pix_fmt or pix_fmt.startswith(("gbr", "rgb", "bgr")):
        base = "yuv444p"
    else:
        base = "yuv420p"
    if base == "gray":
        return "gray" if depth == 8 else f"gray{depth}le"
    return base if depth == 8 else f"{base}{depth}le"


def bit_depth(pix_fmt: str) -> int:
    m = re.search(r"(\d+)(?:le|be)$", pix_fmt or "")
    return int(m.group(1)) if m else 8


def output_geometry(video: VideoProbe, scale: ScaleSettings) -> tuple[int, int, Fraction, str | None]:
    """(width, height, sar, scale filter or None) for the chosen scale mode."""
    w, h, sar = video.width, video.height, video.sar or Fraction(1, 1)
    alg = scale.algorithm or "lanczos"
    if scale.mode == ScaleMode.KEEP or (scale.mode == ScaleMode.SQUARE and sar == 1):
        return w, h, sar, None
    if scale.mode == ScaleMode.SQUARE:
        # always the width: the line structure of the deinterlaced picture is kept
        ow, oh = _even(w * sar), h
        return ow, oh, Fraction(1, 1), f"scale={ow}:{oh}:flags={alg}"
    # custom: explicit size, square pixels; a 0 dimension follows the display aspect ratio
    dar = Fraction(w, h) * sar
    ow, oh = scale.width, scale.height
    if ow <= 0 and oh <= 0:
        ow, oh = w, h
    elif ow <= 0:
        ow = _even(oh * dar)
    elif oh <= 0:
        oh = _even(ow / dar)
    return ow, oh, Fraction(1, 1), f"scale={ow}:{oh}:flags={alg}"


def _tags(video: VideoProbe) -> Colorimetry:
    """Colour tags from the master; unknown fields filled from the resolution defaults when WP-A's
    colorimetry module is available."""
    t = video.color_tags
    if all(getattr(t, k) not in _UNKNOWN for k in ("matrix", "primaries", "transfer")):
        return t
    try:
        from . import colorimetry

        return colorimetry.effective_colorimetry(video)
    except Exception:
        return t


def setparams_filter(c: Colorimetry) -> str | None:
    parts = []
    if c.matrix not in _UNKNOWN:
        parts.append(f"colorspace={c.matrix}")
    if c.primaries not in _UNKNOWN:
        parts.append(f"color_primaries={c.primaries}")
    if c.transfer not in _UNKNOWN:
        parts.append(f"color_trc={c.transfer}")
    if c.range in ("tv", "pc"):
        parts.append(f"range={c.range}")
    return "setparams=" + ":".join(parts) if parts else None


def _ffmpeg_base(ffmpeg: str = "ffmpeg", *, stats: bool = True) -> list[str]:
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning"]
    if stats:
        cmd += ["-stats", "-stats_period", STATS_PERIOD]
    return cmd


def rate_control_args(v: VideoSettings, info: EncoderInfo | None) -> list[str]:
    """ffmpeg backend rate-control flags for the encoder (crf/qp/bitrate/cq/lossless)."""
    names = {o.name for o in info.options} if info else set()
    q = f"{v.quality:g}"
    enc = v.encoder
    if v.rate_control == RateControl.CRF:
        if enc in profiles.CQ_ENCODERS:
            return ["-crf", q, "-b:v", "0"]
        return ["-crf", q]
    if v.rate_control == RateControl.QP:
        if "qp" in names:
            return ["-qp", q]
        if "cqp" in names:
            return ["-rc", "constqp", "-qp", q]
        return ["-q:v", q]
    if v.rate_control == RateControl.CQ:
        if enc in profiles.CQ_ENCODERS:
            return ["-crf", q, "-b:v", "0"]
        return ["-cq", q]
    if v.rate_control == RateControl.LOSSLESS:
        return list(profiles.LOSSLESS_ARGS.get(enc, []))
    args = ["-b:v", f"{v.bitrate_kbps}k"]
    if enc in profiles.CQ_ENCODERS and v.quality > 0:
        args += ["-crf", q]
    return args


def video_args_ffmpeg(v: VideoSettings, info: EncoderInfo | None, container: str = "") -> list[str]:
    args = ["-c:v", v.encoder, *rate_control_args(v, info)]
    codec = info.codec if info else ("hevc" if "265" in v.encoder or "hevc" in v.encoder else "")
    args += muxmatrix.video_tag_args(container, codec)
    if v.preset:
        args += ["-preset", v.preset]
    if v.tune:
        args += ["-tune", v.tune]
    if v.profile:
        args += ["-profile:v", v.profile]
    if v.level:
        args += ["-level", v.level]
    if v.pix_fmt:
        args += ["-pix_fmt", v.pix_fmt]
    args += list(v.extra_args)
    return args


def audio_args(profile: Profile) -> list[str]:
    a = profile.audio
    if a.encoder == "copy":
        return ["-c:a", "copy"]
    args = ["-c:a", a.encoder]
    if a.bitrate_kbps > 0:
        args += ["-b:a", f"{a.bitrate_kbps}k"]
    if a.quality is not None:
        if a.encoder == "flac":
            args += ["-compression_level", f"{int(a.quality)}"]
        else:
            args += ["-q:a", f"{a.quality:g}"]
    if a.sample_rate > 0:
        args += ["-ar", str(a.sample_rate)]
    if a.channels > 0:
        args += ["-ac", str(a.channels)]
    args += list(a.extra_args)
    return args


def _range_name(tool: str, rng: str) -> str | None:
    if rng not in ("tv", "pc"):
        return None
    if tool == "x265":
        return "limited" if rng == "tv" else "full"
    return rng


def tool_command(
    backend: VideoBackend,
    v: VideoSettings,
    *,
    width: int,
    height: int,
    sar: Fraction,
    fps: Fraction,
    tags: Colorimetry,
    out_pix_fmt: str,
    output: str,
) -> list[str]:
    tool = _TOOL_OF[backend]
    depth = bit_depth(out_pix_fmt)
    q = f"{v.quality:g}"
    colour: list[str] = []
    if tags.primaries not in _UNKNOWN:
        colour += ["--colorprim", tags.primaries]
    if tags.transfer not in _UNKNOWN:
        colour += ["--transfer", tags.transfer]
    if tags.matrix not in _UNKNOWN:
        colour += ["--colormatrix", tags.matrix]
    rn = _range_name(tool, tags.range)
    if rn:
        colour += ["--range", rn]
    sar_arg = ["--sar", f"{sar.numerator}:{sar.denominator}"]
    if backend == VideoBackend.X264_CLI:
        cmd = [tool, "--demuxer", "y4m", "--muxer", "mp4", "--no-progress", "--output-depth", str(depth)]
        if v.rate_control == RateControl.CRF:
            cmd += ["--crf", q]
        elif v.rate_control == RateControl.QP:
            cmd += ["--qp", f"{int(v.quality)}"]
        elif v.rate_control == RateControl.LOSSLESS:
            cmd += ["--qp", "0"]
        else:
            cmd += ["--bitrate", str(v.bitrate_kbps)]
        if v.preset:
            cmd += ["--preset", v.preset]
        if v.tune:
            cmd += ["--tune", v.tune]
        if v.profile:
            cmd += ["--profile", v.profile]
        if v.level:
            cmd += ["--level", v.level]
        cmd += sar_arg + colour + list(v.extra_args) + ["-o", output, "-"]
        return cmd
    if backend == VideoBackend.X265_CLI:
        cmd = [tool, "--y4m", "--input", "-", "--no-progress", "--log-level", "warning", "--output-depth", str(depth)]
        if v.rate_control == RateControl.CRF:
            cmd += ["--crf", q]
        elif v.rate_control == RateControl.QP:
            cmd += ["--qp", f"{int(v.quality)}"]
        elif v.rate_control == RateControl.LOSSLESS:
            cmd += ["--lossless"]
        else:
            cmd += ["--bitrate", str(v.bitrate_kbps)]
        if v.preset:
            cmd += ["--preset", v.preset]
        if v.tune:
            cmd += ["--tune", v.tune]
        if v.profile:
            cmd += ["--profile", v.profile]
        if v.level:
            cmd += ["--level-idc", v.level]
        cmd += sar_arg + colour + list(v.extra_args) + ["-o", output]
        return cmd
    # kvazaar
    cmd = [tool, "-i", "-", "--input-file-format", "y4m", "--input-res", f"{width}x{height}", "--input-fps", _frac(fps), "--no-psnr"]
    if v.rate_control == RateControl.LOSSLESS:
        cmd += ["--lossless"]
    elif v.rate_control == RateControl.BITRATE:
        cmd += ["--bitrate", str(v.bitrate_kbps * 1000)]
    else:
        cmd += ["--qp", f"{int(v.quality)}"]
    if v.preset:
        cmd += ["--preset", v.preset]
    if v.level:
        cmd += ["--level", v.level]
    cmd += sar_arg + colour + list(v.extra_args) + ["-o", output]
    return cmd


def _temp(output: str, tag: str, ext: str) -> str:
    p = Path(output)
    return str(p.with_name(f".{p.name}.tmp{os.getpid()}.{tag}.{ext}"))


def _stale_temps(output: str) -> list[str]:
    """``_temp`` files of ``output`` left by processes that no longer run (a killed CLI)."""
    p = Path(output)
    prefix = f".{p.name}.tmp"
    out = []
    try:
        names = os.listdir(p.parent)
    except OSError:
        return out
    for n in names:
        pid = n[len(prefix):].split(".", 1)[0] if n.startswith(prefix) else ""
        if pid.isdigit() and int(pid) != os.getpid() and not osdep.process_alive(int(pid)):
            out.append(str(p.with_name(n)))
    return out


def build_plan(master: str, master_probe: ProbeInfo, profile: Profile, output: str, *, ffmpeg: str = "ffmpeg") -> ViewPlan:
    v, a, video = profile.video, profile.audio, master_probe.video
    tags = _tags(video)
    width, height, sar, scale_f = output_geometry(video, v.scale)
    fps = snap_fps(video.fps)
    pre: list[str] = []
    if v.rate == ViewRateMode.SINGLE and is_double_rate(fps):
        pre.append("framestep=2")
        fps = fps / 2
    if scale_f:
        pre.append(scale_f)
    pre.append(f"setpts=N/({_frac(fps)})/TB")  # exact CFR timestamps (mkv masters are 1 ms-rounded)
    rate_args = ["-r", _frac(fps)]
    timescale = ["-video_track_timescale", str(fps.numerator)] if profile.container in _MP4_FAMILY else []
    total = video.nb_frames
    if total is None and video.duration_s and video.fps:
        total = int(round(video.duration_s * float(video.fps)))
    if total is not None and "framestep=2" in pre:
        total = (total + 1) // 2
    steps: list[Step] = []
    temps: list[str] = []
    tools = ["ffmpeg"]

    # ---- audio: sox pre-processing per track
    audio_inputs: list[str] = []  # extra -i files replacing the master's audio
    if a.sox.enabled and a.sox.effects and master_probe.audio:
        tools.append("sox")
        for i, tr in enumerate(master_probe.audio):
            wav = _temp(output, f"a{i}", "w64")
            temps.append(wav)
            bits = tr.bit_depth if tr.bit_depth in (16, 24, 32) else 16
            feeder = [*_ffmpeg_base(ffmpeg, stats=False), "-i", master, "-map", f"0:a:{i}", "-f", "sox", "-"]
            sox = ["sox", "-t", "sox", "-", "-b", str(bits), "-t", "w64", wav, *a.sox.effects]
            steps.append(Step(label=f"sox a{i}", cmds=[feeder, sox], creates=[wav]))
            audio_inputs.append(wav)

    def audio_maps(first_input: int, master_input: int | None) -> list[str]:
        if audio_inputs:
            return [x for i in range(len(audio_inputs)) for x in ("-map", f"{first_input + i}:a:0")]
        if master_input is None:
            return []
        return ["-map", f"{master_input}:a?"]

    def sox_metadata(master_input: int) -> list[str]:
        """With sox the audio comes from temp files: carry global + per-track metadata from the master."""
        if not audio_inputs:
            return []
        args = ["-map_metadata", str(master_input)]
        for i, tr in enumerate(master_probe.audio):
            args += ["-map_metadata:s:a:" + str(i), f"{master_input}:s:a:{i}"]
            if tr.language:
                args += [f"-metadata:s:a:{i}", f"language={tr.language}"]
        return args

    # ---- video
    if v.backend == VideoBackend.FFMPEG:
        info = None
        try:
            info = encoders.encoder_details(v.encoder, ffmpeg)
        except Exception:
            pass
        vf = [*pre, f"setsar={_frac(sar)}"]
        sp = setparams_filter(tags)
        if sp:
            vf.append(sp)
        cmd = [*_ffmpeg_base(ffmpeg), "-y", "-i", master]
        for wav in audio_inputs:
            cmd += ["-i", wav]
        cmd += ["-map", "0:v:0", *audio_maps(1, 0), *sox_metadata(0), "-vf", ",".join(vf), *rate_args]
        cmd += video_args_ffmpeg(v, info, profile.container)
        cmd += audio_args(profile)
        cmd += [*timescale, "-f", profile.container, output]
        steps.append(Step(label="encode", cmds=[cmd], progress_index=0, total_frames=total, creates=[output]))
    else:
        tool = _TOOL_OF[v.backend]
        tools.append(tool)
        out_pix_fmt = tool_pix_fmt(v.backend, v.pix_fmt or video.pix_fmt)
        feeder = [*_ffmpeg_base(ffmpeg), "-i", master, "-map", "0:v:0", "-an", "-vf", ",".join(pre), *rate_args]
        feeder += ["-strict", "-1", "-pix_fmt", out_pix_fmt, "-f", "yuv4mpegpipe", "-"]
        if v.backend == VideoBackend.X264_CLI:
            video_file = _temp(output, "video", "mp4")  # x264's own mp4 muxer: exact CFR timestamps
            temps.append(video_file)
            enc = tool_command(v.backend, v, width=width, height=height, sar=sar, fps=fps, tags=tags, out_pix_fmt=out_pix_fmt, output=video_file)
            steps.append(Step(label=f"encode ({tool})", cmds=[feeder, enc], progress_index=0, total_frames=total, creates=[video_file]))
            video_input = ["-i", video_file]
        else:
            es = _temp(output, "video", "hevc")
            ts = _temp(output, "video", "ts")
            temps += [es, ts]
            enc = tool_command(v.backend, v, width=width, height=height, sar=sar, fps=fps, tags=tags, out_pix_fmt=out_pix_fmt, output=es)
            steps.append(Step(label=f"encode ({tool})", cmds=[feeder, enc], progress_index=0, total_frames=total, creates=[es]))
            fps_out = fps

            def _timestamps(emitter: Emitter, cancel: CancelToken, offset: int, grand_total: int | None) -> None:
                tracker = ProgressTracker(grand_total)

                def progress(frames: int) -> None:
                    f, eta, elapsed = tracker.update(offset + frames)
                    emitter.progress("view", offset + frames, grand_total, f, eta, elapsed)

                esmux.es_to_ts(es, "hevc", fps_out, ts, cancel=cancel, progress=progress)

            steps.append(Step(label="timestamps", cmds=[], func=_timestamps, total_frames=total, creates=[ts]))
            video_input = ["-f", "mpegts", "-i", ts]
        mux = [*_ffmpeg_base(ffmpeg), "-y", *video_input, "-i", master]
        for wav in audio_inputs:
            mux += ["-i", wav]
        mux += ["-map", "0:v:0", *audio_maps(2, 1), *sox_metadata(1)]
        mux += ["-c:v", "copy", *muxmatrix.video_tag_args(profile.container, "hevc" if tool != "x264" else "h264"), *audio_args(profile)]
        mux += [*timescale, "-f", profile.container, output]
        steps.append(Step(label="mux", cmds=[mux], progress_index=0, total_frames=total, creates=[output]))
    offset = 0
    for st in steps:
        st.progress_offset = offset
        offset += st.total_frames or 0
    return ViewPlan(output=output, steps=steps, temps=temps, width=width, height=height, sar=sar, fps=fps, total_frames=offset or None, tools=tools)


def build_commands(master: str, master_probe: ProbeInfo, profile: Profile, output: str, *, overwrite: bool = False) -> list[list[str]]:
    """The ordered command list (one for ffmpeg backend; pipeline stages for CLI backends)."""
    plan = build_plan(master, master_probe, profile, output)
    return [cmd for step in plan.steps for cmd in step.cmds]


def view_output_path(input_path: str, profile: Profile, name_template: str, output_dir: str) -> str:
    """The one place that names a viewing copy (jobs.view_path and pipeline use it):
    ``<output_dir>/<template>.<profile.id>.<profile.extension>`` where the template knows
    ``{stem}`` (input file name without extension), ``{name}`` (with extension) and
    ``{profile}`` (profile id; when used, the id is not appended again).  Bad templates raise
    UsageError."""
    src = Path(input_path)
    fields = {"stem": src.stem, "name": src.name, "profile": profile.id}
    try:
        name = name_template.format(**fields)
    except (KeyError, IndexError, ValueError) as e:
        raise UsageError(f"bad name template {name_template!r}: {e}", "available placeholders: {stem} {name} {profile}") from None
    reason = osdep.invalid_name_reason(name)
    if reason:
        raise UsageError(f"bad name template {name_template!r}: gives {name!r}", f"the file name {reason}")
    if "{profile}" not in name_template:
        name = f"{name}.{profile.id}"
    return str(Path(output_dir) / f"{name}.{profile.extension}")


# --------------------------------------------------------------------------- execution
def _env() -> dict[str, str]:
    return encoders.subprocess_env()


def _remove(paths: list[str]) -> list[str]:
    removed = []
    for p in paths:
        try:
            os.unlink(p)
            removed.append(p)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return removed


def _level(line: str) -> str:
    low = line.lower()
    if "error" in low or "failed" in low or "invalid" in low:
        return "error"
    if "warning" in low or "deprecated" in low:
        return "warning"
    return "info"


@dataclass
class _StepResult:
    returncodes: list[int]
    tails: list[list[str]]
    cancelled: bool = False


def _run_step(step: Step, *, emitter: Emitter, cancel: CancelToken, stage: str = "view", grand_total: int | None = None) -> _StepResult:
    procs: list[subprocess.Popen[bytes]] = []
    tails: list[list[str]] = [[] for _ in step.cmds]
    tracker = ProgressTracker(grand_total)
    lock = threading.Lock()
    env = _env()
    prev_stdout = None
    for i, cmd in enumerate(step.cmds):
        if shutil.which(cmd[0]) is None:
            for p in procs:
                p.kill()
            raise ComponentError(ErrorCode.TOOL_MISSING, f"{cmd[0]} not found in PATH", f"install the package providing {cmd[0]}", {"tool": cmd[0]})
        last = i == len(step.cmds) - 1
        p = subprocess.Popen(
            cmd,
            stdin=prev_stdout if prev_stdout is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL if last else subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **osdep.popen_kwargs(),
        )
        runtime.track(p)
        if prev_stdout is not None:
            prev_stdout.close()  # only the child keeps the read end
        prev_stdout = p.stdout
        procs.append(p)

    def reader(idx: int, proc: subprocess.Popen[bytes]) -> None:
        name = os.path.basename(step.cmds[idx][0])
        buf = b""
        assert proc.stderr is not None
        while True:
            chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                raw, buf = buf[: m.start()], buf[m.end():]
                _handle_line(raw.decode("utf-8", "replace").strip(), idx, name)
        if buf.strip():
            _handle_line(buf.decode("utf-8", "replace").strip(), idx, name)

    def _handle_line(line: str, idx: int, name: str) -> None:
        if not line:
            return
        with lock:
            tails[idx].append(line)
            if len(tails[idx]) > 60:
                del tails[idx][0]
        if idx == step.progress_index and line.startswith("frame="):
            m = _FRAME_RE.search(line)
            if m:
                done = step.progress_offset + int(m.group(1))
                fps, eta, elapsed = tracker.update(done)
                emitter.progress(stage, done, grand_total, fps, eta, elapsed)
            return
        if line.startswith("frame="):
            return
        emitter.log(name, line, _level(line))

    threads = [threading.Thread(target=reader, args=(i, p), daemon=True) for i, p in enumerate(procs)]
    for t in threads:
        t.start()
    cancelled = False
    while True:
        if all(p.poll() is not None for p in procs):
            break
        if cancel.cancelled:
            cancelled = True
            _terminate(procs)
            break
        time.sleep(0.1)
    for t in threads:
        t.join(timeout=5)
    for p in procs:
        p.wait()
        runtime.untrack(p)
    return _StepResult([p.returncode for p in procs], tails, cancelled)


def _terminate(procs: list[subprocess.Popen[bytes]]) -> None:
    osdep.stop_trees(procs, KILL_GRACE_S)


def probe_output(path: str, ffprobe: str = "ffprobe") -> tuple[int | None, float | None, int | None]:
    """(frames, duration_s, size_bytes) of a finished file via ffprobe (packet count of v:0)."""
    size = os.path.getsize(path) if os.path.exists(path) else None
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets:format=duration", "-of", "json", path]
    try:
        r = runtime.run_capture(cmd, timeout=600, env=_env())
        if r.timed_out:
            return None, None, size
        d = json.loads(r.stdout or "{}")
    except (ComponentError, ValueError):
        return None, None, size
    frames = None
    for s in d.get("streams") or []:
        if s.get("nb_read_packets") not in (None, "N/A"):
            frames = int(s["nb_read_packets"])
    dur = d.get("format", {}).get("duration")
    return frames, (float(dur) if dur not in (None, "N/A") else None), size


def run_view(
    master: str,
    master_probe: ProbeInfo,
    profile: Profile,
    output: str,
    *,
    emitter: Emitter,
    cancel: CancelToken,
    overwrite: bool = False,
) -> OutputFile:
    """Encode one viewing copy with progress (frames from ffmpeg stats or the tool's own output).
    Emits ``progress`` (stage "view") and ``log`` events; the caller emits the ``stage`` event."""
    master = os.path.abspath(master)
    output = os.path.abspath(output)
    if not os.path.isfile(master):
        raise EncodeError(ErrorCode.INPUT_NOT_FOUND, f"master not found: {master}", "", {"master": master})
    out_dir = os.path.dirname(output) or "."
    if os.path.exists(output) and not overwrite:
        raise OutputError(ErrorCode.OUTPUT_EXISTS, f"output exists: {output}", "use --overwrite to replace it", {"output": output})
    if not os.path.isdir(out_dir) or not os.access(out_dir, os.W_OK):
        raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"output directory is not writable: {out_dir}", "", {"output": output})
    for tool in ("ffmpeg", "ffprobe", *profiles.required_tools(profile)):
        if shutil.which(tool) is None:
            raise ComponentError(ErrorCode.TOOL_MISSING, f"profile '{profile.id}' needs {tool}, which is not installed", osdep.install_hint(tool), {"tool": tool, "profile": profile.id})
    problems = profiles.validate(profile, check_tools=False)
    if problems:
        raise EncodeError(ErrorCode.PROFILE_INVALID, f"profile '{profile.id}' is invalid: {problems[0]}", "run `qtgmc-6ui profiles validate`", {"profile": profile.id, "problems": problems})
    partial = partial_path(output)
    _remove([partial, *_stale_temps(partial)])  # leftovers of an earlier killed run
    plan = build_plan(master, master_probe, profile, partial)
    _warn_mux(profile, emitter)
    if cancel.cancelled:
        raise CancelledError(removed=[], kept=[])
    created: list[str] = []
    try:
        for step in plan.steps:
            created += step.creates
            if step.func is not None:
                emitter.log("app", f"view {profile.id}: {step.label} (elementary stream -> MPEG-TS with display-order timestamps)")
                try:
                    if cancel.cancelled:
                        raise CancelledError()
                    step.func(emitter, cancel, step.progress_offset, plan.total_frames)
                except CancelledError:
                    raise CancelledError(removed=_removed_output(created, partial, output), kept=[]) from None
                continue
            emitter.log("app", f"view {profile.id}: {step.label}: " + " | ".join(" ".join(c) for c in step.cmds))
            res = _run_step(step, emitter=emitter, cancel=cancel, grand_total=plan.total_frames)
            if res.cancelled:
                raise CancelledError(removed=_removed_output(created, partial, output), kept=[])
            bad = [i for i, rc in enumerate(res.returncodes) if rc != 0]
            if bad:
                i = bad[-1]  # the downstream process is the culprit when a pipe breaks
                tool = os.path.basename(step.cmds[i][0])
                tail = res.tails[i][-25:]
                _remove(created)
                hint = next((ln for ln in tail if _level(ln) == "error" and "Terminating thread" not in ln and "Task finished" not in ln), tail[-1] if tail else "")
                raise EncodeError(
                    ErrorCode.ENCODE_FAILED,
                    f"{tool} failed (rc {res.returncodes[i]}) during '{step.label}' of viewing copy {profile.id}",
                    hint,
                    {"command": step.cmds[i], "stderr_tail": tail, "profile": profile.id, "output": output},
                )
    finally:
        _remove(plan.temps)
    frames, duration, size = probe_output(partial)
    if size is None or size == 0 or frames in (None, 0):
        _remove([partial])
        raise EncodeError(ErrorCode.ENCODE_FAILED, f"viewing copy {profile.id} produced no readable output", "", {"profile": profile.id, "output": output})
    try:
        os.replace(partial, output)
    except OSError as e:
        _remove([partial])
        raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"cannot move the finished copy to {output}: {e}", "", {"output": output}) from e
    return OutputFile(kind="view", path=output, profile=profile.id, frames=frames, duration_s=duration, size_bytes=size)


def partial_path(output: str) -> str:
    """Hidden in-progress name next to the final output: ``.<name>.partial``."""
    p = Path(output)
    return str(p.with_name(f".{p.name}.partial"))


def _removed_output(created: list[str], partial: str, output: str) -> list[str]:
    """Remove everything this run created; report the output (as its final name) when a
    partial of it existed."""
    removed = _remove(created)
    return [output] if partial in removed else []


def _warn_mux(profile: Profile, emitter: Emitter) -> None:
    v, a = profile.video, profile.audio
    venc = {VideoBackend.X264_CLI: "libx264", VideoBackend.X265_CLI: "libx265", VideoBackend.KVAZAAR_CLI: "libx265"}.get(v.backend, v.encoder)
    aenc = "flac" if a.encoder == "copy" else a.encoder
    for enc, kind in ((venc, "video"), (aenc, "audio")):
        try:
            p = muxmatrix.cached(profile.container, enc, kind=kind)
        except Exception:
            continue
        if p.verdict == MuxVerdict.OK_WITH_WARNING:
            emitter.warning("mux_warning", muxmatrix.reason(profile.container, enc, kind=kind), "the file may not play everywhere")
