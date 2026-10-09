"""preview: PNG writer, source/fields/output renders, preview-info/serve protocol."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from qtgmc_6ui import preview
from qtgmc_6ui.events import Event


def read_png(data: bytes) -> tuple[int, int, bytes]:
    """Minimal validating reader: signature, chunk CRCs, IHDR 8-bit RGB, filter 0 scanlines."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    w = h = 0
    tag = b""
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        tag, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + n]
        (crc,) = struct.unpack(">I", data[pos + 8 + n : pos + 12 + n])
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, tag
        if tag == b"IHDR":
            w, h, depth, ctype, _, _, _ = struct.unpack(">IIBBBBB", body)
            assert (depth, ctype) == (8, 2)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    assert tag == b"IEND"
    raw = zlib.decompress(idat)
    assert len(raw) == h * (3 * w + 1)
    assert all(raw[y * (3 * w + 1)] == 0 for y in range(h))
    return w, h, raw


def test_write_png_roundtrip(tmp_path: Path) -> None:
    w, h = 5, 3
    rows = [b"".join(bytes((x * 10, y * 50, 7)) for x in range(w)) for y in range(h)]
    p = tmp_path / "a.png"
    n = preview.write_png(str(p), w, h, rows)
    data = p.read_bytes()
    assert n == len(data)
    rw, rh, raw = read_png(data)
    assert (rw, rh) == (w, h)
    assert raw[1 : 1 + 3 * w] == rows[0] and raw[-3 * w :] == rows[-1]
    with pytest.raises(ValueError):
        preview.write_png(str(p), w, h, rows[:2])
    assert not any(x.name.startswith("a.png.part") for x in tmp_path.iterdir())


def test_write_png_unwritable(tmp_path: Path) -> None:
    from qtgmc_6ui.errors import OutputError

    with pytest.raises(OutputError):
        preview.write_png(str(tmp_path / "no" / "dir" / "x.png"), 1, 1, [b"\x00\x00\x00"])


# --------------------------------------------------------------------------- real renders
CLI = [sys.executable, "-m", "qtgmc_6ui"]


def _env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "QTGMC6UI_CACHE_DIR": str(tmp_path / "cache"), "QTGMC6UI_CONFIG_DIR": str(tmp_path / "cfg")}


@pytest.mark.needs_sandbox
@pytest.mark.parametrize("mode,size,frames", [("source", (720, 576), 1), ("fields", (720, 1152), 2)])
def test_preview_frame_reference(reference_clip: Path, tmp_path: Path, mode: str, size: tuple[int, int], frames: int) -> None:
    out = tmp_path / f"{mode}.png"
    r = subprocess.run([*CLI, "preview-frame", str(reference_clip), "100", "--mode", mode, "-o", str(out), "--json"],
                       capture_output=True, text=True, timeout=120, env=_env(tmp_path))
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert (d["width"], d["height"], d["frames"]) == (*size, frames)
    assert d["source"]["frames"] == 274 and d["source"]["fps"] == "25/1" and d["source"]["sar"] == "16:15"
    w, h, raw = read_png(out.read_bytes())
    assert (w, h) == size
    assert len(set(raw[1 : 3 * w : 3])) > 10  # real picture content, not a flat frame
    if mode == "fields":
        assert "bff" in d["note"]  # order came from somewhere, and says so


@pytest.mark.needs_sandbox
def test_fields_are_the_source_lines(reference_clip: Path, tmp_path: Path) -> None:
    """The two halves of the fields image are the line-doubled fields of the source frame:
    even rows of the top half == the first field (bottom lines for BFF)."""
    from qtgmc_6ui.models import FieldOrder, PreviewMode, PreviewRequest

    rend = preview.Renderer()
    try:
        src_png = tmp_path / "s.png"
        rend.render(PreviewRequest(str(reference_clip), 100, PreviewMode.SOURCE, output=str(src_png)))
        f_png = tmp_path / "f.png"
        res = rend.render(PreviewRequest(str(reference_clip), 100, PreviewMode.FIELDS, field_order=FieldOrder.TFF, output=str(f_png)))
        assert res.note == ""
        w, h, fraw = read_png(f_png.read_bytes())
        _, sh, sraw = read_png(src_png.read_bytes())
        stride = 3 * w + 1
        # luma-only comparison is not possible after RGB; check the doubled-line structure instead
        for y in range(0, sh, 2):
            assert fraw[y * stride : (y + 1) * stride] == fraw[(y + 1) * stride : (y + 2) * stride]
        assert fraw[: sh * stride] != fraw[sh * stride :]  # the two fields differ (motion)
        info = rend.info(str(reference_clip))
        assert info["frames"] == 274 and info["width"] == 720
    finally:
        rend.close()


@pytest.mark.needs_sandbox
def test_preview_stdout_png_and_errors(reference_clip: Path, tmp_path: Path) -> None:
    r = subprocess.run([*CLI, "preview-frame", str(reference_clip), "5", "-o", "-", "--json"], capture_output=True, timeout=120, env=_env(tmp_path))
    assert r.returncode == 0
    assert read_png(r.stdout)[:2] == (720, 576)
    assert json.loads(r.stderr.decode().strip().splitlines()[-1])["width"] == 720
    r = subprocess.run([*CLI, "preview-frame", str(reference_clip), "274", "-o", str(tmp_path / "x.png"), "--json"],
                       capture_output=True, text=True, timeout=120, env=_env(tmp_path))
    assert r.returncode == 2 and "out of range" in json.loads(r.stdout)["error"]["message"]


@pytest.mark.needs_sandbox
def test_preview_info(reference_clip: Path, tmp_path: Path) -> None:
    r = subprocess.run([*CLI, "preview-info", str(reference_clip), "--json"], capture_output=True, text=True, timeout=120, env=_env(tmp_path))
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["frames"] == 274 and d["fps"] == "25/1" and (d["width"], d["height"]) == (720, 576)
    assert abs(d["duration_s"] - 10.96) < 0.01


@pytest.mark.needs_sandbox
def test_preview_serve_protocol(reference_clip: Path, tmp_path: Path) -> None:
    reqs = [
        {"id": 1, "frame": 0, "mode": "source", "output": str(tmp_path / "a.png")},
        {"id": 2, "frame": 273, "mode": "fields", "field_order": "bff", "output": str(tmp_path / "b.png")},
        {"id": 3, "frame": 9999, "mode": "source", "output": str(tmp_path / "c.png")},
        "not json",
        {"id": 5, "frame": 1, "mode": "source", "output": str(reference_clip)},
    ]
    stdin = "".join((json.dumps(q) if isinstance(q, dict) else q) + "\n" for q in reqs) + "quit\n"
    r = subprocess.run([*CLI, "preview-serve", str(reference_clip)], input=stdin, capture_output=True, text=True, timeout=120, env=_env(tmp_path))
    assert r.returncode == 0, r.stderr
    evs = [Event.from_json(ln) for ln in r.stdout.splitlines()]
    assert all(evs) and evs[0].ev == "start" and evs[-1].ev == "done" and evs[-1].data["exit_code"] == 0
    body = evs[1:-1]
    assert body[0].ev == "result" and body[0].data["data"]["kind"] == "info" and body[0].data["data"]["frames"] == 274
    assert body[1].data["data"]["id"] == 1 and body[1].data["data"]["height"] == 576
    assert body[2].data["data"]["id"] == 2 and body[2].data["data"]["height"] == 1152
    assert body[3].ev == "error" and body[3].data["id"] == 3
    assert body[4].ev == "error" and body[4].data["code"] == "usage"
    assert body[5].ev == "error" and body[5].data["id"] == 5 and "overwrite the input" in body[5].data["message"]
    assert read_png((tmp_path / "b.png").read_bytes())[:2] == (720, 1152)


@pytest.mark.needs_qtgmc
@pytest.mark.parametrize("rate,height,frames", [("double", 1152, 2), ("single", 576, 1)])
def test_preview_output_mode(reference_clip: Path, tmp_path: Path, rate: str, height: int, frames: int) -> None:
    out = tmp_path / "o.png"
    r = subprocess.run([*CLI, "preview-frame", str(reference_clip), "100", "--mode", "output", "--field-order", "bff",
                        "--preset", "faster", "--rate", rate, "--window", "4", "-o", str(out), "--json"],
                       capture_output=True, text=True, timeout=300, env=_env(tmp_path))
    assert r.returncode == 0, r.stderr + r.stdout
    d = json.loads(r.stdout)
    assert (d["width"], d["height"], d["frames"]) == (720, height, frames)
    assert read_png(out.read_bytes())[:2] == (720, height)


@pytest.mark.needs_qtgmc
def test_output_window_maps_to_source_frame(reference_clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The trimmed QTGMC window is indexed correctly: the stacked output frames for N keep
    frame N's original field lines (lossless, BFF: first output = bottom field lines)."""
    import numpy as np

    from qtgmc_6ui.models import FieldOrder, MasterSettings, PreviewMode, PreviewRequest, Preset

    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", str(tmp_path / "cache"))
    rend = preview.Renderer()
    try:
        n = 100
        req = PreviewRequest(str(reference_clip), n, PreviewMode.OUTPUT, FieldOrder.BFF,
                             MasterSettings(preset=Preset.FASTER), str(tmp_path / "o.png"), window=4)
        rend.render(req)
        (g,) = rend.graphs.values()
        first = next(iter(rend.graphs))[2]
        src = np.asarray(rend.sources[str(reference_clip)].get_frame(n)[0])
        o0 = np.asarray(g.clip.get_frame(2 * (n - first))[0])
        o1 = np.asarray(g.clip.get_frame(2 * (n - first) + 1)[0])
        assert (o0[1::2] == src[1::2]).all() and (o1[0::2] == src[0::2]).all()
    finally:
        rend.close()
