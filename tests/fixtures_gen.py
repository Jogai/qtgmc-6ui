"""Deterministic synthetic test clips, generated with ffmpeg and cached by parameters.

    python tests/fixtures_gen.py [name ...]      # generate the named clips (default: all)
    python tests/fixtures_gen.py --list

Content is ``mandelbrot`` (smooth zoom with fine edge detail and real motion).  ``testsrc2`` is
NOT used: idet misclassifies progressive testsrc2 as interlaced.  Interlaced clips are built from
a progressive clip at twice the frame rate whose frame pairs are woven by ``tinterlace``, so the
two fields of every frame are genuinely temporally distinct.  Telecine uses ``telecine=23`` on a
24000/1001 source.  Cache: ``$QTGMC6UI_FIXTURES`` (default /workspace/qtgmc/fixtures/clips); the
file name carries a hash of every parameter, so changed parameters never reuse a stale file.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from pathlib import Path

FIXTURES_DIR = Path(os.environ.get("QTGMC6UI_FIXTURES", "/workspace/qtgmc/fixtures/clips"))
GEN_VERSION = 1  # bump to invalidate every cached clip

BT709 = ("bt709", "bt709", "bt709", "tv")
BT470BG = ("bt470bg", "bt470bg", "bt709", "tv")


@dataclass(frozen=True)
class ClipSpec:
    # tff | bff | tele | mixed | prog | prog_detail.  "prog" is smooth gradients plus a
    # horizontally moving box (idet says Progressive); "prog_detail" is progressive mandelbrot,
    # which idet can only call Undetermined (fine detail, no combing evidence).
    kind: str
    size: tuple[int, int] = (352, 288)
    rate: str = "25/1"  # frame rate of the clip (30000/1001 for 29.97i, 25 for 25i)
    pix_fmt: str = "yuv420p"
    sar: str = "1/1"
    audio: int = 0  # number of audio tracks
    seconds: float = 2.0
    codec: str = "ffv1"  # ffv1 (mkv) | dv (avi, dvvideo + pcm)
    color: tuple[str, str, str, str] | None = None  # matrix, primaries, transfer, range tags
    stem: str = ""  # readable file stem (may be non-ASCII)

    @property
    def expected(self) -> str:
        """Expected ContentClass value."""
        return {"tff": "interlaced_tff", "bff": "interlaced_bff", "tele": "telecined",
                "mixed": "mixed", "prog": "progressive", "prog_detail": "undetermined"}[self.kind]

    @property
    def frames(self) -> int:
        if self.kind == "mixed":
            return 2 * round(self.seconds * 30000 / 1001)
        return round(self.seconds * float(Fraction(self.rate)))

    @property
    def key(self) -> str:
        blob = repr((GEN_VERSION, sorted(asdict(self).items())))
        return hashlib.sha1(blob.encode()).hexdigest()[:10]

    @property
    def filename(self) -> str:
        ext = "avi" if self.codec == "dv" else "mkv"
        stem = self.stem or f"{self.kind}_{self.size[0]}x{self.size[1]}_{self.pix_fmt}"
        return f"{stem}-{self.key}.{ext}"


def _spec(**kw) -> ClipSpec:
    return ClipSpec(**kw)


PAL = dict(size=(352, 288), rate="25/1")
NTSC = dict(size=(352, 240), rate="30000/1001")

NAMED: dict[str, ClipSpec] = {
    # core classification set
    "pal_tff": _spec(kind="tff", **PAL),
    "pal_bff": _spec(kind="bff", **PAL),
    "ntsc_tff": _spec(kind="tff", **NTSC),
    "ntsc_bff": _spec(kind="bff", **NTSC),
    "ntsc30_tff": _spec(kind="tff", size=(352, 240), rate="30/1"),
    "ntsc30_bff": _spec(kind="bff", size=(352, 240), rate="30/1"),
    "ntsc_tele": _spec(kind="tele", size=(352, 240), rate="30000/1001", seconds=4.0),
    # 10 s telecined + 10 s interlaced, so each 300-frame window is pure
    "ntsc_mixed": _spec(kind="mixed", size=(352, 240), rate="30000/1001", seconds=10.0),
    "prog_pal": _spec(kind="prog", **PAL),
    "prog_ntsc": _spec(kind="prog", **NTSC),
    "prog_film": _spec(kind="prog", size=(352, 240), rate="24000/1001"),
    "prog_hd": _spec(kind="prog", size=(1920, 1080), rate="25/1", seconds=1.2),
    "prog_detail": _spec(kind="prog_detail", size=(352, 288), rate="25/1"),
    # resolutions
    "res_320x240_tff": _spec(kind="tff", size=(320, 240), rate="25/1"),
    "res_720x480_bff": _spec(kind="bff", size=(720, 480), rate="30000/1001", sar="8/9"),
    "res_720x576_tff": _spec(kind="tff", size=(720, 576), rate="25/1", sar="16/15"),
    "res_1440x1080_tff": _spec(kind="tff", size=(1440, 1080), rate="25/1", sar="4/3"),
    "res_1920x1080_tff": _spec(kind="tff", size=(1920, 1080), rate="25/1", seconds=1.2),
    "res_1920x1080_bff": _spec(kind="bff", size=(1920, 1080), rate="30000/1001", seconds=1.2),
    # pixel formats
    "fmt_411_bff": _spec(kind="bff", size=(352, 240), rate="30000/1001", pix_fmt="yuv411p"),
    "fmt_444_tff": _spec(kind="tff", size=(352, 288), rate="25/1", pix_fmt="yuv444p"),
    "fmt_422_tff": _spec(kind="tff", size=(352, 288), rate="25/1", pix_fmt="yuv422p"),
    "fmt_422p10_tff": _spec(kind="tff", size=(352, 288), rate="25/1", pix_fmt="yuv422p10le"),
    "fmt_422p10_bff": _spec(kind="bff", size=(352, 288), rate="25/1", pix_fmt="yuv422p10le"),
    # real DV streams (NTSC DV is 4:1:1, BFF)
    "dv_ntsc_bff": _spec(kind="bff", size=(720, 480), rate="30000/1001", pix_fmt="yuv411p",
                         sar="8/9", codec="dv", audio=1),
    "dv_pal_bff": _spec(kind="bff", size=(720, 576), rate="25/1", pix_fmt="yuv420p",
                        sar="16/15", codec="dv", audio=1),
    # audio / metadata
    "audio1_pal_tff": _spec(kind="tff", audio=1, **PAL),
    "audio2_pal_bff": _spec(kind="bff", audio=2, **PAL),
    "audio2_ntsc_tff": _spec(kind="tff", audio=2, **NTSC),
    "tagged_709_tff": _spec(kind="tff", size=(352, 288), rate="25/1", color=BT709),
    "tagged_601_bff": _spec(kind="bff", size=(352, 240), rate="30000/1001", color=BT470BG),
    "sar_pal_43": _spec(kind="tff", size=(352, 288), rate="25/1", sar="16/11"),
    "unicode_tff": _spec(kind="tff", audio=1, stem="vidéo ünï – 日本語 test", **PAL),
}

NAMED = {k: v if v.stem else replace(v, stem=k) for k, v in NAMED.items()}


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({r.returncode}): {' '.join(cmd)}\n{r.stderr[-2000:]}")


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found (run inside tools/archrun)")
    return exe


def _src(kind: str, size: tuple[int, int], rate: Fraction) -> list[str]:
    w, h = size
    r = f"{rate.numerator}/{rate.denominator}"
    if kind == "prog":
        # horizontal motion only: vertical motion in progressive material fools idet
        bw, bh = max(w // 9, 8), max(h // 4, 8)
        g = (f"gradients=size={w}x{h}:rate={r}:speed=0.05[b];color=c=white:s={bw}x{bh}:r={r}[c];"
             f"[b][c]overlay=x='mod(t*{w // 3},{w - bw})':y={h // 5}")
        return ["-f", "lavfi", "-i", g]
    return ["-f", "lavfi", "-i", f"mandelbrot=size={w}x{h}:rate={r}"]


def _audio_inputs(n: int, secs: float) -> list[str]:
    out: list[str] = []
    for i in range(n):
        out += ["-f", "lavfi", "-i", f"sine=frequency={440 * (i + 1)}:sample_rate=48000:duration={secs:.4f}"]
    return out


def _video_tail(spec: ClipSpec) -> list[str]:
    a: list[str] = []
    if spec.codec == "dv":
        a += ["-c:v", "dvvideo"]
    else:
        a += ["-c:v", "ffv1", "-level", "3", "-g", "1"]
    if spec.color:
        m, p, t, r = spec.color
        a += ["-colorspace", m, "-color_primaries", p, "-color_trc", t, "-color_range", r]
    return a


def _audio_tail(spec: ClipSpec) -> list[str]:
    if not spec.audio:
        return ["-an"]
    return ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2" if spec.codec == "dv" else "1"]


def _build_segment(spec: ClipSpec, kind: str, frames: int, out: Path, *, audio: int) -> None:
    """One segment of a single kind (tff/bff/prog/tele) with ``frames`` output frames."""
    rate = Fraction(spec.rate)
    secs = frames / float(rate)
    fmt = f"format={spec.pix_fmt}"
    if kind in ("tff", "bff"):
        src_rate = rate * 2
        weave = "interleave_top" if kind == "tff" else "interleave_bottom"
        vf = f"{fmt},tinterlace={weave},setsar={spec.sar.replace(':', '/')}"
    elif kind == "tele":
        src_rate = rate * 4 / 5
        vf = f"{fmt},telecine=pattern=23,setsar={spec.sar.replace(':', '/')}"
    else:
        src_rate = rate
        vf = f"{fmt},setsar={spec.sar.replace(':', '/')}"
    if spec.color:
        m, p, t, r = spec.color
        vf += f",setparams=colorspace={m}:color_primaries={p}:color_trc={t}:range={r}"
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *_src(kind, spec.size, src_rate),
           *_audio_inputs(audio, secs)]
    cmd += ["-map", "0:v"] + [x for i in range(audio) for x in ("-map", f"{i + 1}:a")]
    cmd += ["-vf", vf, "-frames:v", str(frames), *_video_tail(spec),
            *(_audio_tail(spec) if audio else ["-an"])]
    cmd.append(str(out))
    _run(cmd)


def generate(spec: ClipSpec, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="gen-", dir=dest.parent))
    try:
        part = tmp / ("out" + dest.suffix)
        if spec.kind == "mixed":
            half = spec.frames // 2
            a, b = tmp / ("a" + dest.suffix), tmp / ("b" + dest.suffix)
            _build_segment(spec, "tele", half, a, audio=spec.audio)
            _build_segment(spec, "tff", half, b, audio=spec.audio)
            (tmp / "list.txt").write_text(f"file '{a.name}'\nfile '{b.name}'\n")
            _run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                  "-i", "list.txt", "-c", "copy", part.name], cwd=tmp)
        else:
            _build_segment(spec, spec.kind, spec.frames, part, audio=spec.audio)
        part.replace(dest)  # atomic within the same directory tree
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def get_clip(name_or_spec: str | ClipSpec, base: Path | None = None) -> Path:
    """Return the path of the (cached) clip, generating it on first use."""
    spec = NAMED[name_or_spec] if isinstance(name_or_spec, str) else name_or_spec
    base = base or FIXTURES_DIR
    path = base / spec.filename
    if not path.is_file():
        tmp_dest = base / f".{spec.filename}.{os.getpid()}.tmp"
        base.mkdir(parents=True, exist_ok=True)
        try:
            generate(spec, tmp_dest.with_suffix(path.suffix))
            tmp_dest.with_suffix(path.suffix).replace(path)
        finally:
            tmp_dest.with_suffix(path.suffix).unlink(missing_ok=True)
    return path


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for n, s in NAMED.items():
            print(f"{n:22s} {s.expected:15s} {s.size} {s.rate} {s.pix_fmt} frames={s.frames} {s.filename}")
        return 0
    names = argv or list(NAMED)
    for n in names:
        print(n, get_clip(n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
