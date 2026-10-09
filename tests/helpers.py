"""Shared test helpers (WP-H): ffprobe wrappers, CLI runner, and the standalone field-fidelity
oracle.  The oracle decodes both source and master with plain ffmpeg to raw planes, so it shares
no code with the implementation under test."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

# (bytes per sample, chroma width divisor, chroma height divisor)
PIX_FMTS: dict[str, tuple[int, int, int]] = {
    "yuv420p": (1, 2, 2),
    "yuv422p": (1, 2, 1),
    "yuv444p": (1, 1, 1),
    "yuv411p": (1, 4, 1),
    "yuv420p10le": (2, 2, 2),
    "yuv422p10le": (2, 2, 1),
}


# ---------------------------------------------------------------- ffprobe

def ffprobe(path: Path | str) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


@dataclass
class MediaInfo:
    frames: int
    fps: Fraction
    width: int
    height: int
    pix_fmt: str
    sar: str
    codec: str
    color: tuple[str, str, str, str]  # matrix, primaries, transfer, range ("" when untagged)
    audio: list[dict]
    duration: float


def media_info(path: Path | str) -> MediaInfo:
    d = ffprobe(path)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    audio = [s for s in d["streams"] if s["codec_type"] == "audio"]

    def tag(k: str) -> str:
        x = v.get(k, "")
        return "" if x in ("unknown", "unspecified") else x

    return MediaInfo(
        frames=int(v.get("nb_read_frames") or v.get("nb_frames") or 0),
        fps=Fraction(v["r_frame_rate"]), width=v["width"], height=v["height"],
        pix_fmt=v["pix_fmt"], sar=v.get("sample_aspect_ratio", "1:1").replace("/", ":"),
        codec=v["codec_name"],
        color=(tag("color_space"), tag("color_primaries"), tag("color_transfer"), tag("color_range")),
        audio=audio, duration=float(d["format"].get("duration", 0)))


def audio_duration(path: Path | str, index: int = 0) -> float:
    """Decoded duration of audio stream ``index`` in seconds (packet-exact, not container tags)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"a:{index}", "-show_entries",
         "packet=pts_time,duration_time", "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    pk = json.loads(r.stdout)["packets"]
    if not pk:
        return 0.0
    last = pk[-1]
    return float(last["pts_time"]) + float(last.get("duration_time", 0))


# ---------------------------------------------------------------- CLI

def cli(*args: str, env: dict[str, str | None] | None = None, timeout: float = 900,
        cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m qtgmc_6ui ...`` as a user would; stdout/stderr captured as text.
    ``env`` values of None remove the variable (e.g. LC_ALL for the LANG=C runs)."""
    e = dict(os.environ)
    for k, v in (env or {}).items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return subprocess.run([sys.executable, "-m", "qtgmc_6ui", *args], capture_output=True,
                          text=True, env=e, timeout=timeout, cwd=cwd)


def cli_json(*args: str, **kw) -> tuple[int, dict | None, str]:
    """Run with --json; returns (exit code, parsed stdout document or None, stderr)."""
    p = cli(*args, "--json", **kw)
    try:
        doc = json.loads(p.stdout) if p.stdout.strip() else None
    except json.JSONDecodeError:
        doc = None
    return p.returncode, doc, p.stderr


# ---------------------------------------------------------------- field oracle

def read_frames(path: Path | str, pix_fmt: str, width: int, height: int):
    """Decode the video stream to a list of per-frame plane lists (numpy arrays), no conversion."""
    import numpy as np

    bps, cw, ch = PIX_FMTS[pix_fmt]
    dt = np.uint8 if bps == 1 else np.dtype("<u2")
    shapes = [(height, width), (height // ch, width // cw), (height // ch, width // cw)]
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0", "-f", "rawvideo",
         "-pix_fmt", pix_fmt, "-"], capture_output=True, check=True)
    buf = np.frombuffer(p.stdout, dtype=dt)
    sizes = [h * w for h, w in shapes]
    fsize = sum(sizes)
    if buf.size % fsize:
        raise AssertionError(f"raw size {buf.size} not a multiple of frame size {fsize}")
    frames = []
    for i in range(buf.size // fsize):
        off = i * fsize
        planes = []
        for (h, w), n in zip(shapes, sizes, strict=True):
            planes.append(buf[off:off + n].reshape(h, w))
            off += n
        frames.append(planes)
    return frames


def field_planes(frame, parity: int):
    """Rows of one field (parity 0 = even/top lines, 1 = odd/bottom lines) of every plane."""
    return [pl[parity::2] for pl in frame]


def count_field_mismatches(src, out, tff: bool, rate: str = "double") -> tuple[int, int]:
    """Compare source fields with the matching fields of the deinterlaced frames.

    ``src`` / ``out`` are lists of frames (list of planes).  Double rate: output frame 2k is the
    first-in-time field of source frame k, 2k+1 the second; single rate: output frame k keeps the
    first field of source frame k.  Returns (mismatching field-planes, compared field-planes).
    A field-plane is one plane of one original field, so lossless means 0."""
    import numpy as np

    first = 0 if tff else 1
    bad = total = 0
    for k, sf in enumerate(src):
        pairs = [(2 * k, first), (2 * k + 1, 1 - first)] if rate == "double" else [(k, first)]
        for oi, parity in pairs:
            if oi >= len(out):
                raise AssertionError(f"output has {len(out)} frames, need index {oi}")
            for a, b in zip(field_planes(sf, parity), field_planes(out[oi], parity), strict=True):
                total += 1
                bad += not np.array_equal(a, b)
    return bad, total


def expected_out_frames(src_frames: int, rate: str) -> int:
    return src_frames * 2 if rate == "double" else src_frames


def deint_master(clip: Path, out: Path, *extra: str, field_order: str = "auto", rate: str = "double",
                 preset: str | None = "draft", env: dict[str, str | None] | None = None,
                 timeout: float = 1800) -> subprocess.CompletedProcess:
    """Run ``deint`` to ``out``; asserts success and that the file exists."""
    args = ["deint", str(clip), "-o", str(out), "--rate", rate, "--overwrite"]
    if field_order != "auto":
        args += ["--field-order", field_order]
    if preset:
        args += ["--preset", preset]
    p = cli(*args, *extra, env=env, timeout=timeout)
    assert p.returncode == 0, (
        f"deint rc={p.returncode}\nstdout={p.stdout[-800:]}\nstderr={p.stderr[-1500:]}")
    assert out.is_file(), "deint reported success but wrote no master"
    return p
