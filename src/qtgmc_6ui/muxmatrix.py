"""Container x codec validity by probe-mux (WP-C).

A static table is wrong for this ffmpeg (mp4 takes FLAC/Opus/PCM; mov rejects
them), so validity is established by a 0.1 s test mux per (muxer, encoder):
  ffmpeg -f lavfi -i testsrc2=size=320x240:rate=25 -t 0.1 -c:v ENC [-pix_fmt ..] -f MUXER /tmp/x
  ffmpeg -f lavfi -i sine=sample_rate=48000 -t 0.1 -ac 2 -c:a ENC -f MUXER ...
Verdict: rc 0, non-empty output AND the file reads back (ffprobe) with the encoder's codec
-> OK; "may be unplayable" in stderr -> OK_WITH_WARNING; else FAIL (note = stderr tail or the
read-back problem).  A plain rc/size check is fooled: avi+libx265 "succeeds" but reads back as
rawvideo, mpegts+flac/ffv1/prores are written as private data streams no demuxer recognises.
Timeouts and OS errors are transient and never cached.
Cached per ffmpeg BUILD (version + hash of ``-version`` + binary mtime) in
paths.mux_matrix_cache_dir()/matrix-<build>.json; ``data/mux_seed.json`` (probed on n9.0.2) seeds
the table when the version string matches, so the first start is instant.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import encoders, osdep, paths, runtime
from .errors import ComponentError
from .models import MuxProbe, MuxVerdict, from_dict, to_dict

SEED_PATH = Path(__file__).parent / "data" / "mux_seed.json"
PROBE_TIMEOUT_S = 60.0

_WARNING_MARKERS = ("may be unplayable",)
_FAIL_MARKERS = ("private data stream", "may not be recognized upon reading")
_BAD_CODECS = {"", "none", "bin_data", "rawvideo"}
_MP4_FAMILY = {"mp4", "mov", "ipod", "psp", "3gp", "3g2", "ismv", "f4v"}
TRANSIENT = "transient: "
# speed flags so a probe takes ~0.1 s even for slow encoders
_SPEED_ARGS: dict[str, list[str]] = {
    "libx264": ["-preset", "ultrafast"],
    "libx264rgb": ["-preset", "ultrafast"],
    "libx265": ["-preset", "ultrafast"],
    "libsvtav1": ["-preset", "13"],
    "libaom-av1": ["-cpu-used", "8", "-usage", "realtime"],
    "librav1e": ["-speed", "10"],
    "libvpx": ["-deadline", "realtime", "-cpu-used", "8"],
    "libvpx-vp9": ["-deadline", "realtime", "-cpu-used", "8"],
}

_lock = threading.RLock()
_mem: dict[str, dict[str, MuxProbe]] = {}  # version -> key -> probe
_loaded: set[str] = set()


def _key(container: str, encoder: str, kind: str) -> str:
    return f"{kind}:{container}:{encoder}"


def _version(ffmpeg: str) -> str:
    try:
        return encoders.ffmpeg_version(ffmpeg)
    except Exception:
        return "unknown"


def _build(ffmpeg: str) -> str:
    try:
        return encoders.ffmpeg_build_id(ffmpeg)
    except Exception:
        return "unknown"


def _cache_file(build: str) -> Path:
    return paths.mux_matrix_cache_dir() / f"matrix-{encoders.safe_name(build)}.json"


def _table(ffmpeg: str) -> dict[str, MuxProbe]:
    """In-memory table for this ffmpeg build; loads the seed (by version string) and the disk
    cache (by build id) on first use."""
    build = _build(ffmpeg)
    with _lock:
        t = _mem.setdefault(build, {})
        if build in _loaded:
            return t
        _loaded.add(build)
    _merge_file(t, SEED_PATH, _version(ffmpeg), overwrite=False)
    _merge_file(t, _cache_file(build), build, overwrite=True)
    return t


def _merge_file(t: dict[str, MuxProbe], path: Path, version: str, *, overwrite: bool) -> None:
    d = osdep.load_json(path)
    if not isinstance(d, dict) or d.get("ffmpeg_version") != version:
        return
    with _lock:
        for row in d.get("probes", []):
            try:
                p = from_dict(MuxProbe, row)
                kind = row.get("kind", "video")
            except (TypeError, ValueError, KeyError):
                continue
            k = _key(p.container, p.encoder, kind)
            if overwrite or k not in t:
                t[k] = p


def _save(ffmpeg: str) -> None:
    build = _build(ffmpeg)
    t = _mem.get(build, {})
    rows = []
    with _lock:
        for k, p in sorted(t.items()):
            rows.append({**to_dict(p), "kind": k.split(":", 1)[0]})
    p = _cache_file(build)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        osdep.atomic_write_text(p, json.dumps({"schema": 1, "ffmpeg_version": build, "probes": rows}, indent=0))
    except OSError:
        pass


def seed_from_package_data() -> None:
    """(Re)load data/mux_seed.json into the in-memory table for the running ffmpeg."""
    t = _table("ffmpeg")
    _merge_file(t, SEED_PATH, _version("ffmpeg"), overwrite=False)


def invalidate_cache() -> None:
    with _lock:
        _mem.clear()
        _loaded.clear()
    try:
        for p in paths.mux_matrix_cache_dir().glob("matrix-*.json"):
            p.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- probing
def classify_result(rc: int, size: int, stderr: str, readback: str | None = None, expected: str | None = None) -> tuple[MuxVerdict, str]:
    """``readback`` = codec_name ffprobe reports for the probe file (None = not checked),
    ``expected`` = the encoder's codec id."""
    lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    note = " | ".join(lines[-2:])[:300]
    if rc in (124, 127):
        return MuxVerdict.FAIL, TRANSIENT + (note or f"rc {rc}")
    if rc != 0 or size <= 0:
        return MuxVerdict.FAIL, note or f"ffmpeg exited with rc {rc}"
    if any(m in stderr for m in _FAIL_MARKERS):
        return MuxVerdict.FAIL, note
    if readback is not None:
        if readback in _BAD_CODECS and expected != readback:
            return MuxVerdict.FAIL, f"file reads back as '{readback or 'no stream'}' (not {expected})"
        if expected and readback != expected:
            return MuxVerdict.FAIL, f"file reads back as '{readback}' instead of {expected}"
    if any(m in stderr for m in _WARNING_MARKERS):
        return MuxVerdict.OK_WITH_WARNING, note
    return MuxVerdict.OK, ""


def _readback(path: str, ffprobe: str) -> str | None:
    """codec_name of the first stream ffprobe finds, "" when it finds none, None on error."""
    cmd = [ffprobe, "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", path]
    try:
        r = runtime.run_capture(cmd, timeout=PROBE_TIMEOUT_S, env=encoders.subprocess_env())
    except ComponentError:
        return None
    if r.timed_out:
        return None
    if r.returncode != 0:
        return ""
    names = [ln.strip().strip(",") for ln in r.stdout.splitlines() if ln.strip()]
    return names[0] if names else ""


def _codec_of(encoder: str, ffmpeg: str) -> str | None:
    try:
        return encoders.encoder_details(encoder, ffmpeg).codec or encoder
    except Exception:
        return None


def video_tag_args(container: str, codec: str) -> list[str]:
    """HEVC in the mp4 family is tagged hvc1 (Apple players reject hev1)."""
    if codec == "hevc" and container in _MP4_FAMILY:
        return ["-tag:v", "hvc1"]
    return []


def _probe_cmd(container: str, encoder: str, kind: str, out: str, ffmpeg: str) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y", "-threads", "1", "-filter_threads", "1"]
    if kind == "video":
        cmd += ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "0.1", "-an", "-c:v", encoder]
        try:
            info = encoders.encoder_details(encoder, ffmpeg)
            if info.pix_fmts:
                cmd += ["-pix_fmt", info.pix_fmts[0]]
            cmd += video_tag_args(container, info.codec)
        except Exception:
            pass
        cmd += _SPEED_ARGS.get(encoder, [])
    else:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.1", "-vn", "-ac", "2"]
        try:
            info = encoders.encoder_details(encoder, ffmpeg)
            if info.sample_rates and 48000 not in info.sample_rates:
                cmd += ["-ar", str(max(info.sample_rates))]
        except Exception:
            pass
        cmd += ["-c:a", encoder]
    cmd += ["-f", container, out]
    return cmd


def run_probe(container: str, encoder: str, *, kind: str, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> MuxProbe:
    """Execute one probe mux and read the result back (no cache)."""
    with tempfile.TemporaryDirectory(prefix="qtgmc-mux-") as td:
        out = os.path.join(td, f"probe.{container}")
        cmd = _probe_cmd(container, encoder, kind, out, ffmpeg)
        try:
            r = runtime.run_capture(cmd, timeout=PROBE_TIMEOUT_S, env=encoders.subprocess_env())
            rc, stderr = (124, "probe timed out") if r.timed_out else (r.returncode, r.stderr)
        except ComponentError as e:
            rc, stderr = 127, e.message
        size = os.path.getsize(out) if os.path.exists(out) else 0
        readback = _readback(out, ffprobe) if rc == 0 and size > 0 else None
    verdict, note = classify_result(rc, size, stderr, readback, _codec_of(encoder, ffmpeg))
    return MuxProbe(container=container, encoder=encoder, verdict=verdict, note=note)


def is_transient(p: MuxProbe) -> bool:
    return p.note.startswith(TRANSIENT)


def probe(container: str, encoder: str, *, kind: str, ffmpeg: str = "ffmpeg") -> MuxProbe:
    """kind: 'video' | 'audio'.  Uses the cache; probes on demand."""
    t = _table(ffmpeg)
    k = _key(container, encoder, kind)
    with _lock:
        p = t.get(k)
    if p is not None and p.verdict != MuxVerdict.UNKNOWN:
        return p
    p = run_probe(container, encoder, kind=kind, ffmpeg=ffmpeg)
    if is_transient(p):
        return p  # timeout / resource problem: try again next time
    with _lock:
        t[k] = p
    _save(ffmpeg)
    return p


def cached(container: str, encoder: str, *, kind: str, ffmpeg: str = "ffmpeg") -> MuxProbe:
    """Cache lookup only (verdict UNKNOWN when never probed); never runs ffmpeg."""
    t = _table(ffmpeg)
    with _lock:
        return t.get(_key(container, encoder, kind)) or MuxProbe(container=container, encoder=encoder)


def verdict(container: str, encoder: str, *, kind: str) -> MuxVerdict:
    return probe(container, encoder, kind=kind).verdict


def reason(container: str, encoder: str, *, kind: str) -> str:
    """Short text for a greyed-out option ("mov does not accept FLAC: ...")."""
    p = probe(container, encoder, kind=kind)
    if p.verdict == MuxVerdict.OK:
        return ""
    if p.verdict == MuxVerdict.OK_WITH_WARNING:
        return f"{container} accepts {encoder} with a warning: {p.note}"
    if p.verdict == MuxVerdict.FAIL:
        if is_transient(p):
            return f"{container} + {encoder}: probe could not run ({p.note[len(TRANSIENT):]}); retried next time"
        tail = _short_note(p.note)
        return f"{container} does not accept {encoder}" + (f": {tail}" if tail else "")
    return f"{container} + {encoder} not probed yet"


def _short_note(note: str) -> str:
    """Strip ffmpeg's '[ctx @ 0x..] ' prefixes and keep the most telling fragment."""
    parts = [re.sub(r"^\[[^\]]*\]\s*", "", x.strip()) for x in note.split(" | ")]
    parts = [p for p in parts if p]
    for p in parts:
        if "Nothing was written" in p:
            return "muxer wrote nothing (codec not supported in this container)"
        if "reads back as" in p:
            return p
        if "private data stream" in p or "may not be recognized" in p:
            return "written as a private data stream that no player recognises"
    return parts[-1][:160] if parts else ""


def probe_all(containers: list[str], video: list[str], audio: list[str], ffmpeg: str = "ffmpeg") -> list[MuxProbe]:
    """Bulk probe (``qtgmc-6ui encoders --probe``); transient failures are returned but not cached."""
    t = _table(ffmpeg)
    jobs = [(c, e, "video") for c in containers for e in video] + [(c, e, "audio") for c in containers for e in audio]
    with _lock:
        todo = [j for j in jobs if t.get(_key(*j), MuxProbe("", "")).verdict == MuxVerdict.UNKNOWN]
    fresh: dict[tuple[str, str, str], MuxProbe] = {}
    if todo:
        with ThreadPoolExecutor(max_workers=min(4, max(1, os.cpu_count() or 1))) as ex:
            for j, p in zip(todo, ex.map(lambda j: run_probe(j[0], j[1], kind=j[2], ffmpeg=ffmpeg), todo), strict=True):
                fresh[j] = p
                if not is_transient(p):
                    with _lock:
                        t[_key(*j)] = p
        _save(ffmpeg)
    with _lock:
        return [t.get(_key(*j)) or fresh[j] for j in jobs]
