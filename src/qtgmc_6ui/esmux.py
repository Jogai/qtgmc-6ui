"""Raw HEVC (x265, kvazaar) elementary stream -> MPEG-TS with real timestamps (WP-C helper).
x264 writes mp4 itself (lsmash muxer) and does not need this.

Why: ffmpeg 9 cannot stream-copy a raw Annex B elementary stream that uses B-frames into a
container correctly.  The raw demuxer yields packets without pts/dts, ffmpeg then assigns
pts = dts = decode index, so B-frames end up displayed in decode order (verified: mse per frame
against the master jumps 0.4 -> 300 for the reordered frames) and matroska/mpegts/nut refuse
the packets outright ("Can't write packet with unknown timestamp").  mkvmerge/MP4Box are not
dependencies, so this module does what they do:

  1. ``packets()``   ffprobe -show_packets: access units in decode order (pos, size, key flag)
  2. ``display_order()``  ffprobe -show_frames: the decoder emits frames in display order and
     reports ``pkt_pos`` (the packet each frame came from) -> display index per packet.
  3. ``write_ts()``  a minimal single-program MPEG-TS (PAT, PMT with stream_type 0x24 HEVC /
     0x1B H.264, one PES per access unit with PTS and DTS, PCR on keyframes) that ffmpeg reads
     with ``-f mpegts`` and remuxes with ``-c:v copy`` into any container.

The decode pass costs a fraction of the encode (HEVC decoding runs at several hundred fps for
SD) and needs no bitstream parsing of our own.
"""

from __future__ import annotations

import os
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

from . import osdep, runtime
from .errors import CancelledError, EncodeError, ErrorCode
from .runtime import CancelToken

TS_PACKET = 188
PAT_PID = 0x0000
PMT_PID = 0x1000
VIDEO_PID = 0x0100
STREAM_TYPE = {"h264": 0x1B, "hevc": 0x24}
TIMEBASE = 90000


@dataclass
class AccessUnit:
    pos: int
    size: int
    key: bool
    display: int = -1  # display index (0-based), -1 = unknown


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def _env() -> dict[str, str]:
    from . import encoders

    return encoders.subprocess_env()


def _run_csv(cmd: list[str], *, cancel: CancelToken | None, on_line: Callable[[str], None] | None = None) -> tuple[int, list[str], str]:
    """Run ffprobe with ``-of compact``; stream stdout lines, poll ``cancel`` every 0.1 s (GRACEFUL
    stop, then FORCE).  Returns (rc, lines, stderr tail)."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=_env(),
        **osdep.popen_kwargs(),
    )
    runtime.track(proc)
    lines: list[str] = []
    err: list[bytes] = []

    def reader() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                lines.append(line)
                if on_line:
                    on_line(line)

    def err_reader() -> None:
        assert proc.stderr is not None
        err.append(proc.stderr.read())

    threads = [threading.Thread(target=reader, daemon=True), threading.Thread(target=err_reader, daemon=True)]
    for t in threads:
        t.start()
    cancelled = False
    while proc.poll() is None:
        if cancel is not None and cancel.cancelled:
            cancelled = True
            osdep.stop_tree(proc, runtime.KILL_GRACE_S)
            break
        time.sleep(0.1)
    proc.wait()
    runtime.untrack(proc)
    for t in threads:
        t.join(timeout=5)
    if cancelled:
        raise CancelledError()
    return proc.returncode, lines, (b"".join(err)).decode("utf-8", "replace")[-1000:]


def packets(es_path: str, fmt: str, ffprobe: str = "ffprobe", *, cancel: CancelToken | None = None) -> list[AccessUnit]:
    """Access units in decode order via the raw demuxer + parser."""
    cmd = [ffprobe, "-v", "error", "-f", fmt, "-show_entries", "packet=pos,size,flags", "-of", "compact=p=0", es_path]
    rc, lines, err = _run_csv(cmd, cancel=cancel)
    if rc != 0:
        raise EncodeError(ErrorCode.ENCODE_FAILED, f"ffprobe could not read the {fmt} elementary stream", "", {"stderr_tail": err})
    out: list[AccessUnit] = []
    for line in lines:
        kv = _kv(line)
        if kv.get("pos") in (None, "N/A"):
            continue
        out.append(AccessUnit(pos=int(kv["pos"]), size=int(kv.get("size", "0")), key="K" in kv.get("flags", "")))
    return out


def _kv(line: str) -> dict[str, str]:
    """``-of compact=p=0`` line (key=value|key=value) -> dict."""
    out: dict[str, str] = {}
    for part in line.split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def display_order(
    es_path: str,
    fmt: str,
    ffprobe: str = "ffprobe",
    threads: int = 0,
    *,
    cancel: CancelToken | None = None,
    progress: Callable[[int], None] | None = None,
) -> list[int]:
    """Packet byte positions in display order (one per decoded frame); ``progress(frames)`` is
    called while decoding."""
    cmd = [ffprobe, "-v", "error", "-f", fmt, "-threads", str(threads), "-show_entries", "frame=pkt_pos", "-of", "compact=p=0", es_path]
    count = 0
    last = [time.monotonic()]

    def on_line(_line: str) -> None:
        nonlocal count
        count += 1
        now = time.monotonic()
        if progress and now - last[0] >= 0.5:
            last[0] = now
            progress(count)

    rc, lines, err = _run_csv(cmd, cancel=cancel, on_line=on_line)
    if rc != 0:
        raise EncodeError(ErrorCode.ENCODE_FAILED, f"ffprobe could not decode the {fmt} elementary stream", "", {"stderr_tail": err})
    out: list[int] = []
    for line in lines:
        pos = _kv(line).get("pkt_pos", "")
        if pos in ("", "N/A"):
            raise EncodeError(
                ErrorCode.ENCODE_FAILED,
                "ffprobe reports no pkt_pos per decoded frame; cannot restore the display order of the elementary stream",
                "use the ffmpeg backend (libx265) for this profile",
            )
        out.append(int(pos))
    if progress:
        progress(count)
    return out


def annotate(aus: list[AccessUnit], order: list[int]) -> tuple[list[AccessUnit], int]:
    """Attach display indices; returns (aus, reorder delay in frames)."""
    index = {pos: i for i, pos in enumerate(order)}
    if len(index) != len(aus) or any(au.pos not in index for au in aus):
        raise EncodeError(
            ErrorCode.ENCODE_FAILED,
            f"elementary stream packet/frame mismatch ({len(aus)} access units, {len(order)} frames)",
            "the encoder output is incomplete or not frame-based",
        )
    delay = 0
    for i, au in enumerate(aus):
        au.display = index[au.pos]
        delay = max(delay, i - au.display)
    return aus, delay


# --------------------------------------------------------------------------- TS writing
def _section(table_id: int, id16: int, body: bytes) -> bytes:
    length = len(body) + 5 + 4  # after section_length: id16(2) ver(1) sec(1) last(1) + body + crc
    head = struct.pack(">BHHBBB", table_id, 0xB000 | length, id16, 0xC1, 0, 0)
    sec = head + body
    return sec + struct.pack(">I", _crc32_mpeg(sec))


def _pat() -> bytes:
    return _section(0x00, 1, struct.pack(">HH", 1, 0xE000 | PMT_PID))


def _pmt(stream_type: int) -> bytes:
    body = struct.pack(">HH", 0xE000 | VIDEO_PID, 0xF000) + struct.pack(">BHH", stream_type, 0xE000 | VIDEO_PID, 0xF000)
    return _section(0x02, 1, body)


def _psi_packet(pid: int, section: bytes, cc: int) -> bytes:
    payload = b"\x00" + section
    pkt = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10 | (cc & 0x0F)]) + payload
    return pkt + b"\xff" * (TS_PACKET - len(pkt))


def _ts5(prefix: int, t: int) -> bytes:
    t &= (1 << 33) - 1
    return bytes(
        [
            (prefix << 4) | (((t >> 30) & 0x07) << 1) | 1,
            (t >> 22) & 0xFF,
            (((t >> 15) & 0x7F) << 1) | 1,
            (t >> 7) & 0xFF,
            ((t & 0x7F) << 1) | 1,
        ]
    )


def _pes(payload: bytes, pts: int, dts: int) -> bytes:
    header = b"\x00\x00\x01\xe0" + struct.pack(">H", 0) + bytes([0x80, 0xC0, 10]) + _ts5(3, pts) + _ts5(1, dts)
    return header + payload


class _Writer:
    def __init__(self, fh) -> None:  # noqa: ANN001
        self.fh = fh
        self.cc: dict[int, int] = {}

    def _next_cc(self, pid: int) -> int:
        c = self.cc.get(pid, 15)
        c = (c + 1) & 0x0F
        self.cc[pid] = c
        return c

    def psi(self, pid: int, section: bytes) -> None:
        self.fh.write(_psi_packet(pid, section, self._next_cc(pid)))

    def pes(self, pid: int, data: bytes, *, pcr: int | None, random_access: bool) -> None:
        first = True
        view = memoryview(data)
        while first or len(view):
            af_body = b""  # adaptation field after its length byte: flags [+ PCR]
            if first and (pcr is not None or random_access):
                af_body = bytes([(0x10 if pcr is not None else 0) | (0x40 if random_access else 0)])
                if pcr is not None:
                    af_body += struct.pack(">Q", ((pcr & ((1 << 33) - 1)) << 15) | 0x7E00)[2:]
            room = TS_PACKET - 4 - (1 + len(af_body) if af_body else 0)
            chunk = bytes(view[:room])
            view = view[len(chunk):]
            pad = room - len(chunk)
            if af_body or pad:
                if not af_body:
                    af = b"\x00" if pad == 1 else bytes([pad - 1, 0x00]) + b"\xff" * (pad - 2)
                else:
                    af = bytes([len(af_body) + pad]) + af_body + b"\xff" * pad
                afc = 0x30
            else:
                af, afc = b"", 0x10
            hdr = bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, afc | self._next_cc(pid)])
            pkt = hdr + af + chunk
            assert len(pkt) == TS_PACKET, len(pkt)
            self.fh.write(pkt)
            first = False


def write_ts(es_path: str, aus: list[AccessUnit], delay: int, fps: Fraction, codec: str, out_path: str, *, cancel: CancelToken | None = None) -> None:
    """Write ``out_path`` (MPEG-TS) from the access units of ``es_path`` with
    pts = display index, dts = decode index - delay (90 kHz), both offset by one delay."""
    dur = Fraction(TIMEBASE) * fps.denominator / fps.numerator
    base = int((delay + 1) * dur)
    stream_type = STREAM_TYPE[codec]
    with open(es_path, "rb") as src, open(out_path, "wb") as dst:
        w = _Writer(dst)
        w.psi(PAT_PID, _pat())
        w.psi(PMT_PID, _pmt(stream_type))
        for i, au in enumerate(aus):
            src.seek(au.pos)
            data = src.read(au.size)
            pts = base + int(round(au.display * dur))
            dts = base + int(round((i - delay) * dur))
            if i and i % 500 == 0:
                w.psi(PAT_PID, _pat())
                w.psi(PMT_PID, _pmt(stream_type))
                if cancel is not None and cancel.cancelled:
                    raise CancelledError()
            w.pes(VIDEO_PID, _pes(data, pts, dts), pcr=dts if au.key else None, random_access=au.key)


def es_to_ts(
    es_path: str,
    codec: str,
    fps: Fraction,
    out_path: str,
    ffprobe: str = "ffprobe",
    *,
    cancel: CancelToken | None = None,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Full conversion; returns the number of access units.  Cancellable (CancelledError within
    ~0.1 s), ``progress(frames decoded)`` every 0.5 s."""
    aus = packets(es_path, codec, ffprobe, cancel=cancel)
    if not aus:
        raise EncodeError(ErrorCode.ENCODE_FAILED, "the encoder produced an empty elementary stream", "", {"es": es_path})
    order = display_order(es_path, codec, ffprobe, cancel=cancel, progress=progress)
    aus, delay = annotate(aus, order)
    write_ts(es_path, aus, delay, fps, codec, out_path, cancel=cancel)
    try:
        os.unlink(es_path)
    except OSError:
        pass
    return len(aus)
