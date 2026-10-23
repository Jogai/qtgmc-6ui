"""Scriptable fake ``qtgmc-6ui`` CLI for GUI tests (emits contract-conformant JSON / JSON lines).

Run as ``python tests/fake_backend.py <subcommand> ...``; the GUI uses it when
``QTGMC6UI_GUI_BACKEND`` names it.  Behaviour is driven by the input file name and env vars:

  file name contains   tele -> telecined, mixed -> mixed, prog -> progressive, undet -> undetermined,
                       tff -> interlaced TFF, missing file -> input_not_found; otherwise interlaced BFF
  FAKE_QTGMC_FRAMES    source frames (default 274)
  FAKE_QTGMC_DELAY     seconds per progress step of run-job (default 0.002)
  FAKE_QTGMC_DOCTOR    ok | fail (default ok)
  FAKE_QTGMC_FAIL      error code emitted by run-job after the setup stage (e.g. encode_failed)
  FAKE_QTGMC_TOOLS     comma list of optional tools present (default x264,x265,kvazaar,sox)
  FAKE_QTGMC_ARGV_LOG  append every argv (JSON) to this file
  FAKE_QTGMC_CANCEL_DELAY  seconds the "in-flight frame" takes before a cancel is honoured
  FAKE_QTGMC_UNPROBED  container whose verdicts are not cached (``--probe`` tests them)
  FAKE_QTGMC_SERVE     broken -> preview-serve exits at once (GUI falls back to preview-frame)
  FAKE_QTGMC_NO_AUDIO_MODES  1 -> ``audio-modes`` is unknown (backend older than the audio selector)
  FAKE_QTGMC_AUDIO_FAIL      1 -> run-job fails with audio_mode_unsupported for a non-copy mode
"""

from __future__ import annotations

import json
import os
import signal
import struct
import sys
import threading
import time
import tomllib
import zlib
from fractions import Fraction
from pathlib import Path

from qtgmc_6ui import models as m
from qtgmc_6ui.errors import ErrorCode, ExitCode, QtgmcError
from qtgmc_6ui.events import Emitter, JsonLinesSink

CANCEL = threading.Event()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _log_argv() -> None:
    if p := os.environ.get("FAKE_QTGMC_ARGV_LOG"):
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")


def _watch_cancel() -> None:
    def on_signal(signum, frame):  # noqa: ARG001
        CANCEL.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def reader() -> None:
        for line in sys.stdin:
            if line.strip() == "cancel":
                CANCEL.set()
                return

    threading.Thread(target=reader, daemon=True).start()


# --------------------------------------------------------------------------- analysis
def _content_for(path: str) -> m.ContentClass:
    name = Path(path).name.lower()
    for key, cls in (
        ("tele", m.ContentClass.TELECINED),
        ("mixed", m.ContentClass.MIXED),
        ("prog", m.ContentClass.PROGRESSIVE),
        ("undet", m.ContentClass.UNDETERMINED),
        ("tff", m.ContentClass.INTERLACED_TFF),
    ):
        if key in name:
            return cls
    return m.ContentClass.INTERLACED_BFF


ACTIONS = {
    m.ContentClass.INTERLACED_TFF: (m.RecommendedAction.DEINTERLACE, "Interlaced, top field first: deinterlace."),
    m.ContentClass.INTERLACED_BFF: (m.RecommendedAction.DEINTERLACE, "Interlaced, bottom field first: deinterlace."),
    m.ContentClass.TELECINED: (
        m.RecommendedAction.BLOCK_TELECINED,
        "This file is telecined (3:2 pulldown film). It needs inverse telecine (IVTC) with another tool; "
        "deinterlacing it with QTGMC gives inferior results.",
    ),
    m.ContentClass.MIXED: (
        m.RecommendedAction.BLOCK_MIXED,
        "This file is partly telecined. It needs IVTC with another tool for the telecined parts.",
    ),
    m.ContentClass.PROGRESSIVE: (m.RecommendedAction.SKIP_PROGRESSIVE, "No interlacing found: do not deinterlace."),
    m.ContentClass.UNDETERMINED: (
        m.RecommendedAction.CONFIRM_FIELD_ORDER,
        "Interlacing could not be determined reliably; confirm the field order.",
    ),
}


def make_analysis(path: str) -> m.AnalysisResult:
    frames = _env_int("FAKE_QTGMC_FRAMES", 274)
    content = _content_for(path)
    ntsc = content in (m.ContentClass.TELECINED, m.ContentClass.MIXED) or "ntsc" in Path(path).name.lower()
    fps = Fraction(30000, 1001) if ntsc else Fraction(25, 1)
    w, h = (720, 480) if ntsc else (720, 576)
    video = m.VideoProbe(
        codec="dvvideo", codec_long="DV (Digital Video)", width=w, height=h, pix_fmt="yuv420p", bit_depth=8,
        fps=fps, sar=Fraction(16, 15) if not ntsc else Fraction(8, 9), dar=Fraction(4, 3), nb_frames=frames,
        nb_frames_exact=True, duration_s=float(frames / fps), container_field_order="unknown",
        color_tags=m.Colorimetry(source="tags"),
    )
    probe = m.ProbeInfo(
        path=path, container="avi", size_bytes=41_703_544, duration_s=video.duration_s, video=video,
        audio=[m.AudioProbe(index=1, codec="pcm_s16le", sample_rate=48000, channels=2, channel_layout="stereo",
                            bit_depth=16, duration_s=video.duration_s)],
    )
    counts = {
        m.ContentClass.INTERLACED_TFF: m.IdetCounts(tff=frames),
        m.ContentClass.INTERLACED_BFF: m.IdetCounts(bff=frames),
        m.ContentClass.TELECINED: m.IdetCounts(tff=frames * 2 // 5, progressive=frames * 3 // 5),
        m.ContentClass.MIXED: m.IdetCounts(tff=frames * 3 // 4, progressive=frames // 4),
        m.ContentClass.PROGRESSIVE: m.IdetCounts(progressive=frames),
        m.ContentClass.UNDETERMINED: m.IdetCounts(tff=10, bff=12, undetermined=frames - 22),
    }[content]
    repeated = m.RepeatCounts(neither=frames)
    if content in (m.ContentClass.TELECINED, m.ContentClass.MIXED):
        repeated = m.RepeatCounts(neither=frames * 3 // 5, top=frames // 5, bottom=frames // 5)
    order = {m.ContentClass.INTERLACED_TFF: m.FieldOrder.TFF, m.ContentClass.INTERLACED_BFF: m.FieldOrder.BFF}.get(content)
    action, msg = ACTIONS[content]
    window = m.IdetWindow(start_frame=0, start_s=0.0, frames=frames, single=counts, multi=counts, repeated=repeated,
                          window_class=content)
    return m.AnalysisResult(
        input=path, probe=probe, windows=[window], totals_single=counts, totals_multi=counts,
        totals_repeated=repeated, fieldmatch_run=content in (m.ContentClass.TELECINED, m.ContentClass.MIXED),
        classification=m.Classification(content, order, 0.98, reasons=[f"idet multi: {counts.tff} TFF, {counts.bff} BFF, "
                                                                         f"{counts.progressive} progressive"]),
        recommendation=m.Recommendation(action, msg, True),
        colorimetry=m.Colorimetry("bt470bg", "bt470bg", "bt709", "tv", "default"),
        preset_default=m.Preset.SLOWER, frames_out_double=frames * 2, fps_out_double=fps * 2,
        sampled_all_frames=True, took_s=0.4, tool_versions={"ffmpeg": "n9.0.2"},
    )


def cmd_analyze(args, em: Emitter | None) -> int:
    if not Path(args.input).is_file():
        raise QtgmcError(ErrorCode.INPUT_NOT_FOUND, f"input not found: {args.input}", "Check the path.")
    res = make_analysis(args.input)
    if em:
        em.start("analyze", args.input, [])
        for i, stage in enumerate(("probe", "idet", "classify"), 1):
            em.stage(stage, i, 3)
            em.progress(stage, 50, 100, 0.0, None, 0.01)
            em.progress(stage, 100, 100, 0.0, None, 0.02)
        em.result(res.to_dict())
        em.done(0)
    else:
        print(json.dumps(res.to_dict(), ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- jobs
def _audio_error(audio: str) -> QtgmcError:
    e = QtgmcError(ErrorCode.USAGE, f"audio mode {audio!r} cannot be used with this source",
                   "choose another audio mode in the master settings")
    e.code = "audio_mode_unsupported"  # coded error as the real backend emits it
    return e


def cmd_run_job(args, em: Emitter) -> int:
    _watch_cancel()
    text = sys.stdin.read() if args.job == "-" else Path(args.job).read_text(encoding="utf-8")
    job = m.JobSpec.from_dict(json.loads(text))
    if not job.keep_master and not job.views:
        raise QtgmcError(ErrorCode.USAGE, "--no-master needs at least one viewing copy",
                         "add --view PROFILE, or keep the master")
    if not Path(job.input).is_file():
        raise QtgmcError(ErrorCode.INPUT_NOT_FOUND, f"input not found: {job.input}", "Check the path.")
    analysis = job.analysis or make_analysis(job.input)
    act = analysis.recommendation.action
    if act in (m.RecommendedAction.BLOCK_TELECINED, m.RecommendedAction.BLOCK_MIXED) and not job.force:
        raise QtgmcError(ErrorCode.BLOCKED_TELECINED, "telecined input", "Use IVTC or --force.")
    if act == m.RecommendedAction.CONFIRM_FIELD_ORDER and job.master.field_order == m.FieldOrderChoice.AUTO:
        raise QtgmcError(ErrorCode.BLOCKED_UNDETERMINED, "field order unknown", "Pass a field order.")

    src = Path(job.input)
    out_dir = Path(job.output_dir) if job.output_dir else src.parent
    stem = job.name_template.format(stem=src.stem)
    master = Path(job.master_output) if job.master_output else out_dir / f"{stem}.mkv"
    if not job.keep_master:  # hidden temporary master, deleted after the views (as jobs.temp_master_path)
        master = out_dir / f".{stem}.nomaster.{os.getpid()}.tmp.mkv"
    views = [(v.profile, out_dir / f"{stem}.{v.profile}.mp4") for v in job.views]
    planned = ([str(master)] if job.keep_master else []) + [str(p) for _, p in views]
    audio = json.loads(text).get("master", {}).get("audio") or "copy"
    mode = next((a for a in AUDIO_MODES if a["id"] == audio), None)
    if mode is None or (audio != "copy" and os.environ.get("FAKE_QTGMC_AUDIO_FAIL") == "1"):
        raise _audio_error(audio)
    em.start("run-job", job.input, planned)
    if mode["lossy"]:
        em.warning("audio_lossy", f"the master audio is encoded as {mode['label']}: it is no longer the original",
                   "choose a lossless audio mode to keep the original audio")
    stages = [("setup", "", None)] + [("master", "", master)] + [("view", pid, p) for pid, p in views]
    frames = (analysis.probe.video.nb_frames or 274) * (2 if job.master.rate == m.RateMode.DOUBLE else 1)
    delay = float(os.environ.get("FAKE_QTGMC_DELAY", "0.002"))
    finished: list[str] = []
    t0 = time.monotonic()
    for i, (name, label, out) in enumerate(stages, 1):
        em.stage(name, i, len(stages), label)
        if name == "setup":
            em.log("app", f"qtgmc kwargs for {src.name}: preset={job.master.preset or 'auto'}")
            if code := os.environ.get("FAKE_QTGMC_FAIL"):
                raise QtgmcError(ErrorCode(code), f"fake failure {code}", "This is the scripted failure.",
                                 {"stderr": "ffmpeg: Conversion failed!"})
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and not job.overwrite:
            raise QtgmcError(ErrorCode.OUTPUT_EXISTS, f"output exists: {out}", "Enable overwrite or change the name.")
        with open(out, "wb") as f:
            for done in range(0, frames + 1, 4):
                if CANCEL.is_set():
                    time.sleep(float(os.environ.get("FAKE_QTGMC_CANCEL_DELAY", "0")))
                    f.close()
                    out.unlink(missing_ok=True)
                    removed = [str(out)]
                    if not job.keep_master and master.exists():
                        master.unlink()
                        finished = [x for x in finished if x != str(master)]
                    em.cancelled(removed, finished)
                    em.done(int(ExitCode.CANCELLED))
                    return int(ExitCode.CANCELLED)
                f.write(b"\0" * 64)
                el = time.monotonic() - t0
                em.progress(name, done, frames, done / el if el else 0.0, (frames - done) * delay, el)
                if done % 40 == 0:
                    em.log("ffmpeg", f"frame={done} fps=50 q=-0.0 size={done}KiB")
                time.sleep(delay)
            em.progress(name, frames, frames, 50.0, 0.0, time.monotonic() - t0)
        finished.append(str(out))
    master_out = [m.OutputFile("master", str(master), None, frames, 10.96, master.stat().st_size)]
    if not job.keep_master:
        master.unlink()
        master_out = []
    result = m.JobResult(
        input=job.input, analysis=analysis,
        outputs=master_out
        + [m.OutputFile("view", str(p), pid, frames, 10.96, p.stat().st_size) for pid, p in views],
        elapsed_s=time.monotonic() - t0, average_fps=50.0, effective_master=job.master,
    )
    em.result(result.to_dict())
    em.done(0)
    return 0


# --------------------------------------------------------------------------- doctor / encoders / profiles
def cmd_doctor(args, em) -> int:  # noqa: ARG001
    fail = os.environ.get("FAKE_QTGMC_DOCTOR", "ok") == "fail"
    checks = [
        m.DoctorCheck("ffmpeg", "FFmpeg", m.Status.OK, "n9.0.2"),
        m.DoctorCheck("python_abi", "Python version matches build", m.Status.FAIL if fail else m.Status.OK,
                      "built for 3.14, running 3.15" if fail else "3.14",
                      "Arch updated python to 3.15; reinstall/rebuild qtgmc-6ui" if fail else ""),
        m.DoctorCheck("plugins", "Bundled plugins", m.Status.WARN, "dfttest2 not bundled", "Denoise is unavailable."),
    ]
    rep = m.DoctorReport(ok=not fail, summary="1 problem found" if fail else "all checks passed", checks=checks,
                         runtime={"python": "3.14.7", "vapoursynth": "R80"},
                         tools=[m.ToolInfo("ffmpeg", "/usr/bin/ffmpeg", "n9.0.2", True)],
                         environment={"LANG": os.environ.get("LANG", "")})
    print(json.dumps(rep.to_dict(), ensure_ascii=False))
    return int(ExitCode.COMPONENT) if fail else 0


MUXERS = [
    m.MuxerInfo("matroska", "Matroska", ["mkv"], "libx264", "libvorbis"),
    m.MuxerInfo("mp4", "MP4 (MPEG-4 Part 14)", ["mp4"], "libx264", "aac"),
    m.MuxerInfo("mov", "QuickTime / MOV", ["mov"], "libx264", "aac"),
    m.MuxerInfo("webm", "WebM", ["webm"], "libvpx-vp9", "libopus"),
]
VIDEO_ENC = {"libx264": "h264", "libx265": "hevc", "libvpx-vp9": "vp9", "ffv1": "ffv1"}
AUDIO_ENC = {"aac": "aac", "libopus": "opus", "flac": "flac", "pcm_s16le": "pcm_s16le", "libmp3lame": "mp3"}
FAILS = {
    ("mp4", "pcm_s16le"), ("mp4", "ffv1"), ("mov", "flac"), ("mov", "libopus"),
    ("webm", "libx264"), ("webm", "libx265"), ("webm", "ffv1"), ("webm", "aac"), ("webm", "flac"),
    ("webm", "pcm_s16le"), ("webm", "libmp3lame"),
}


def _tools() -> list[m.ToolInfo]:
    present = {t for t in os.environ.get("FAKE_QTGMC_TOOLS", "x264,x265,kvazaar,sox").split(",") if t}
    out = [m.ToolInfo("ffmpeg", "/usr/bin/ffmpeg", "n9.0.2", True), m.ToolInfo("ffprobe", "/usr/bin/ffprobe", "n9.0.2", True)]
    for t in ("x264", "x265", "kvazaar", "sox"):
        out.append(m.ToolInfo(t, f"/usr/bin/{t}" if t in present else None, "1.0" if t in present else None, t in present))
    return out


def _encoder_info(name: str) -> m.EncoderInfo:
    kind = "video" if name in VIDEO_ENC else "audio"
    info = m.EncoderInfo(name, kind, name, VIDEO_ENC.get(name) or AUDIO_ENC.get(name, ""))
    if name in ("libx264", "libx265"):
        info.pix_fmts = ["yuv420p", "yuv422p", "yuv444p", "yuv420p10le"]
        info.options = [
            m.EncoderOption("crf", "float", "constant rate factor", "-1", "-1", "51"),
            m.EncoderOption("qp", "int", "constant quantizer", "-1", "-1", "69"),
            m.EncoderOption("preset", "string", "encoding preset", "medium"),
            m.EncoderOption("tune", "string", "tune"),
            m.EncoderOption("profile", "string", "profile"),
        ]
    elif name == "libvpx-vp9":
        info.pix_fmts = ["yuv420p", "yuv420p10le"]
        info.options = [m.EncoderOption("crf", "int", "quality", "-1", "-1", "63"),
                        m.EncoderOption("deadline", "int", "deadline", "good", choices=["best", "good", "realtime"])]
    elif name == "ffv1":
        info.pix_fmts = ["yuv420p", "yuv422p10le"]
    else:
        info.sample_rates = [44100, 48000]
    return info


def cmd_encoders(args, em) -> int:  # noqa: ARG001
    """Same shapes as commands.cmd_encoders."""
    out: dict = {"tools": [t.to_dict() for t in _tools()]}
    encs = [e for e in [*VIDEO_ENC, *AUDIO_ENC] if not args.kind or (e in VIDEO_ENC) == (args.kind == "video")]
    if args.container:
        mux = next(x for x in MUXERS if x.name == args.container)
        out["muxer"] = mux.to_dict()
        unprobed = os.environ.get("FAKE_QTGMC_UNPROBED") == args.container and not args.probe
        if args.probe:
            time.sleep(0.3)
        out["probes"] = []
        for enc in ([args.encoder] if args.encoder else encs):
            bad = (args.container, enc) in FAILS
            if unprobed and enc in AUDIO_ENC:
                verdict, reason = "unknown", "not tested yet (qtgmc-6ui encoders --probe --container ... tests it)"
            else:
                verdict = "fail" if bad else "ok"
                reason = f"{args.container} does not accept {enc}: Could not write header" if bad else ""
            out["probes"].append({"container": args.container, "encoder": enc,
                                  "kind": "video" if enc in VIDEO_ENC else "audio",
                                  "verdict": verdict, "reason": reason, "note": ""})
        out["unprobed"] = sum(1 for p in out["probes"] if p["verdict"] == "unknown")
    elif args.encoder:
        out["encoder"] = _encoder_info(args.encoder).to_dict()
    else:
        out["muxers"] = [x.to_dict() for x in MUXERS]
        out["encoders"] = [_encoder_info(e).to_dict() | {"options": []} for e in encs]
    print(json.dumps(out))
    return 0


def _builtin_profiles() -> list[m.Profile]:
    a = m.Profile(id="mp4-h264-aac", name="MP4 – H.264 + AAC (compatible)", container="mp4", extension="mp4",
                  video=m.VideoSettings(encoder="libx264", quality=18, preset="slow",
                                        scale=m.ScaleSettings(m.ScaleMode.SQUARE)),
                  audio=m.AudioSettings("aac", 192), builtin=True)
    b = m.Profile(id="mkv-hevc10-opus", name="MKV – HEVC 10-bit + Opus (efficient)", container="matroska",
                  video=m.VideoSettings(encoder="libx265", quality=20, preset="slow", pix_fmt="yuv420p10le"),
                  audio=m.AudioSettings("libopus", 128), builtin=True)
    c = m.Profile(id="mkv-h264-flac", name="MKV – H.264 + FLAC (high quality)", container="matroska",
                  video=m.VideoSettings(encoder="libx264", quality=16, preset="slower"),
                  audio=m.AudioSettings("flac"), builtin=True)
    return [a, b, c]


def _profiles_dir() -> Path:
    base = os.environ.get("QTGMC6UI_CONFIG_DIR") or str(Path.home() / ".config" / "qtgmc-6ui")
    return Path(base) / "profiles"


def _user_profiles() -> list[m.Profile]:
    out = []
    d = _profiles_dir()
    for f in sorted(d.glob("*.toml")) if d.is_dir() else []:
        p = m.Profile.from_dict(tomllib.loads(f.read_text(encoding="utf-8")))
        p.path = str(f)
        out.append(p)
    return out


def _toml(p: m.Profile) -> str:
    """Tiny TOML writer for the fake (flat tables only)."""
    d = {k: v for k, v in p.to_dict().items() if k not in ("builtin", "path")}

    def val(v):
        return json.dumps(v, ensure_ascii=False) if not isinstance(v, bool) else str(v).lower()

    def table(name: str, t: dict) -> list[str]:
        lines = [f"[{name}]"] if name else []
        lines += [f"{k} = {val(v)}" for k, v in t.items() if not isinstance(v, dict) and v is not None]
        for k, v in t.items():
            if isinstance(v, dict):
                lines += table(f"{name}.{k}" if name else k, v)
        return lines

    return "\n".join(table("", d)) + "\n"


def cmd_profiles(args, em) -> int:  # noqa: ARG001
    if args.action == "path":
        print(json.dumps({"path": str(_profiles_dir())}))
        return 0
    profiles = {p.id: p for p in _builtin_profiles()}
    profiles.update({p.id: p for p in _user_profiles()})
    if args.action == "list":
        print(json.dumps([p.to_dict() | {"available": True} for p in sorted(profiles.values(), key=lambda p: p.name)],
                         ensure_ascii=False))
        return 0
    if args.action == "save":
        d = json.loads(sys.stdin.read() if args.target == "-" else Path(args.target).read_text(encoding="utf-8"))
        prof = m.Profile.from_dict(d)
        if args.id:
            prof.id = args.id
        doc = {"id": prof.id, "path": None, "valid": False, "problems": [], "profile": prof.to_dict()}
        target = _profiles_dir() / f"{prof.id}.toml"
        err = None
        if prof.id in {b.id for b in _builtin_profiles()}:
            err = QtgmcError(ErrorCode.PROFILE_INVALID, f"'{prof.id}' is the id of a built-in profile")
        elif target.exists() and not args.overwrite:
            err = QtgmcError(ErrorCode.OUTPUT_EXISTS, f"a user profile '{prof.id}' already exists")
        elif (prof.container, prof.video.encoder) in FAILS or (prof.container, prof.audio.encoder) in FAILS:
            doc["problems"] = [f"{prof.container} cannot hold this codec combination"]
            err = QtgmcError(ErrorCode.PROFILE_INVALID, "profile is invalid")
        if err:
            print(json.dumps(doc | {"error": err.to_dict()}))
            return int(err.exit_code)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_toml(prof), encoding="utf-8")
        print(json.dumps(doc | {"path": str(target), "valid": True}))
        return 0
    if args.action == "delete":
        if args.target in {b.id for b in _builtin_profiles()}:
            raise QtgmcError(ErrorCode.PROFILE_INVALID, "built-in profiles cannot be deleted")
        f = _profiles_dir() / f"{args.target}.toml"
        existed = f.exists()
        f.unlink(missing_ok=True)
        print(json.dumps({"id": args.target, "deleted": existed}))
        return 0
    if args.action == "validate":
        target = args.target or ""
        if target.endswith(".toml"):
            prof = m.Profile.from_dict(tomllib.loads(Path(target).read_text(encoding="utf-8")))
        else:
            prof = profiles[target]
        problems = []
        if (prof.container, prof.video.encoder) in FAILS or (prof.container, prof.audio.encoder) in FAILS:
            problems.append(f"{prof.container} cannot hold this codec combination")
        print(json.dumps({"id": prof.id, "path": prof.path, "valid": not problems, "problems": problems}))
        return 0 if not problems else int(ExitCode.USAGE)
    if args.action == "show":
        print(json.dumps(profiles[args.target].to_dict(), ensure_ascii=False))
        return 0
    raise QtgmcError(ErrorCode.USAGE, f"unsupported in fake: {args.action}")


# --------------------------------------------------------------------------- preview
def _png(path: str, w: int, h: int, rows: list[bytes]) -> None:
    raw = b"".join(b"\0" + r for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b"")
    Path(path).write_bytes(png)


def _frame_rows(w: int, h: int, n: int, combed: bool, shade: int) -> list[bytes]:
    rows = []
    for y in range(h):
        x0 = (n * 8 + (12 if combed and y % 2 else 0)) % (w - 80)
        line = bytearray(bytes((shade, shade, shade + 20)) * w)
        line[x0 * 3:(x0 + 80) * 3] = b"\xf0\xd0\x30" * 80
        rows.append(bytes(line))
    return rows


def cmd_preview_frame(args, em) -> int:  # noqa: ARG001
    if not Path(args.input).is_file():
        raise QtgmcError(ErrorCode.INPUT_NOT_FOUND, f"input not found: {args.input}")
    res = _render(args.input, args.frame, args.mode, args.output)
    print(json.dumps(res.to_dict()))
    return 0


def _render(path: str, frame: int, mode: str, out: str) -> m.PreviewResult:
    w, h = 720, 576
    if mode == "source":
        rows, frames = _frame_rows(w, h, frame, True, 40), 1
    elif mode == "fields":
        top = [r for r in _frame_rows(w, h, frame, False, 40)[0::2] for _ in (0, 1)]
        bot = [r for r in _frame_rows(w, h, frame + 1, False, 60)[1::2] for _ in (0, 1)]
        rows, frames = top + bot, 2
    else:
        rows = _frame_rows(w, h, frame * 2, False, 30) + _frame_rows(w, h, frame * 2 + 1, False, 30)
        frames = 2
    _png(out, w, len(rows), rows)
    return m.PreviewResult(out, w, len(rows), frames, 0.05, f"fake {mode}")


def cmd_preview_serve(args, em) -> int:  # noqa: ARG001
    em = Emitter(JsonLinesSink())
    em.start("preview-serve", args.input, [])
    if os.environ.get("FAKE_QTGMC_SERVE") == "broken":
        em.error(QtgmcError(ErrorCode.INTERNAL, "renderer broken (fake)").to_dict())
        em.done(1)
        return 1
    if not Path(args.input).is_file():
        e = QtgmcError(ErrorCode.INPUT_NOT_FOUND, f"input not found: {args.input}")
        em.error(e.to_dict())
        em.done(e.exit_code)
        return int(e.exit_code)
    em.result({"kind": "info", "frames": _env_int("FAKE_QTGMC_FRAMES", 274), "width": 720, "height": 576})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "quit":
            break
        d = json.loads(line)
        time.sleep(0.2 if d.get("mode") == "output" else 0.01)
        res = _render(args.input, int(d["frame"]), d.get("mode", "source"), d["output"])
        em.result({"kind": "frame", "id": d.get("id"), **res.to_dict()})
    em.done(0)
    return 0


def cmd_version(args, em) -> int:  # noqa: ARG001
    print(json.dumps({"version": "0.0.0-fake"}))
    return 0


AUDIO_MODES = [
    {"id": "copy", "label": "Copy source audio", "group": "lossless", "lossy": False,
     "tooltip": "Audio streams are copied unchanged from the original file."},
    {"id": "pcm", "label": "PCM (uncompressed)", "group": "lossless", "lossy": False,
     "tooltip": "Uncompressed PCM at the source bit depth."},
    {"id": "flac", "label": "FLAC (lossless)", "group": "lossless", "lossy": False,
     "tooltip": "Lossless compression, bit-identical when decoded."},
    {"id": "alac", "label": "ALAC (lossless)", "group": "lossless", "lossy": False,
     "tooltip": "Apple lossless compression."},
    {"id": "aac", "label": "AAC (lossy)", "group": "lossy", "lossy": True,
     "tooltip": "Lossy AAC; smaller, but no longer the original audio."},
    {"id": "opus", "label": "Opus (lossy)", "group": "lossy", "lossy": True,
     "tooltip": "Lossy Opus; smaller, but no longer the original audio."},
]


def cmd_audio_modes(argv: list[str]) -> int:
    if os.environ.get("FAKE_QTGMC_NO_AUDIO_MODES") == "1":
        print("usage: qtgmc-6ui ...\nqtgmc-6ui: error: argument command: invalid choice: 'audio-modes'", file=sys.stderr)
        if "--json" in argv:
            print(json.dumps({"error": QtgmcError(ErrorCode.USAGE, "invalid command line").to_dict()}))
        return 2
    print(json.dumps(AUDIO_MODES, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    from qtgmc_6ui.cli import build_parser

    _log_argv()
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "audio-modes":
        return cmd_audio_modes(argv)
    args = build_parser().parse_args(argv)
    events = getattr(args, "events", False)
    em = Emitter(JsonLinesSink()) if events else None
    handler = globals().get("cmd_" + (args.command or "version").replace("-", "_"))
    if handler is None:
        print(f"fake backend: unsupported command {args.command}", file=sys.stderr)
        return 2
    try:
        return handler(args, em)
    except QtgmcError as e:
        if em:
            em.error(e.to_dict())
            em.done(e.exit_code)
        else:
            print(json.dumps({"error": e.to_dict()}))
        print(str(e), file=sys.stderr)
        return int(e.exit_code)


if __name__ == "__main__":
    sys.exit(main())
