"""THE core guarantee (brief §11): with source match + lossless on, every output field line that
corresponds to an original field is bit-identical to the source.  The oracle (helpers.py) decodes
source and master independently with ffmpeg; nothing from the implementation is reused.

Negative control: with --no-lossless the same check must fail, otherwise the test proves nothing.
"""

from __future__ import annotations

import pytest

from fixtures_gen import NAMED, get_clip
from helpers import (
    count_field_mismatches,
    deint_master,
    expected_out_frames,
    media_info,
    read_frames,
)

np = pytest.importorskip("numpy")

# name -> (tff, rate)
REPRESENTATIVE = [
    ("pal_tff", "double"),
    ("ntsc_bff", "single"),
    ("fmt_411_bff", "double"),
    ("fmt_422p10_bff", "double"),
    ("dv_pal_bff", "double"),
]
INTERLACED = [n for n, s in NAMED.items() if s.kind in ("tff", "bff")]
FULL = [(n, r) for n in INTERLACED for r in ("double", "single")]
FULL_SLOW = [pytest.param(n, r, marks=pytest.mark.slow) for n, r in FULL
             if (n, r) not in REPRESENTATIVE]

pytestmark = pytest.mark.needs_sandbox


def _fields(name, rate, tmp_path, *extra, preset="draft"):
    spec = NAMED[name]
    clip = get_clip(name)
    tff = spec.kind == "tff"
    out = tmp_path / "master.mkv"
    deint_master(clip, out, *extra, field_order="tff" if tff else "bff", rate=rate, preset=preset)
    si, mi = media_info(clip), media_info(out)
    assert mi.pix_fmt == si.pix_fmt, "master must keep the source pixel format"
    assert (mi.width, mi.height) == (si.width, si.height)
    assert mi.frames == expected_out_frames(si.frames, rate)
    src = read_frames(clip, si.pix_fmt, si.width, si.height)
    outf = read_frames(out, mi.pix_fmt, mi.width, mi.height)
    return count_field_mismatches(src, outf, tff, rate)


@pytest.mark.parametrize("name,rate", REPRESENTATIVE + FULL_SLOW)
@pytest.mark.needs_qtgmc
def test_lossless_preserves_source_fields(name, rate, tmp_path, isolated_dirs):
    bad, total = _fields(name, rate, tmp_path)
    assert total > 0
    assert bad == 0, f"{bad}/{total} original field planes changed ({name}, {rate})"


@pytest.mark.needs_qtgmc
@pytest.mark.parametrize("name,rate", [("pal_tff", "double"), ("ntsc_bff", "single")])
def test_negative_control_no_lossless_fails_the_check(name, rate, tmp_path, isolated_dirs):
    bad, total = _fields(name, rate, tmp_path, "--no-lossless")
    assert bad > 0, "check cannot fail: --no-lossless left every original field untouched"


@pytest.mark.needs_qtgmc
def test_default_preset_and_no_source_match_keep_fields(tmp_path, isolated_dirs):
    # lossless alone (no source match) and the resolution-default preset must also hold
    bad, total = _fields("pal_bff", "double", tmp_path, "--no-source-match", preset="faster")
    assert bad == 0, f"{bad}/{total}"


@pytest.mark.slow
@pytest.mark.needs_qtgmc
@pytest.mark.parametrize("name", ["fmt_444_tff", "res_1440x1080_tff"])
def test_more_formats_double(name, tmp_path, isolated_dirs):
    bad, total = _fields(name, "double", tmp_path)
    assert bad == 0, f"{bad}/{total}"


# ---- oracle self-tests: run without the QTGMC stack (numpy + ffmpeg only)

@pytest.mark.needs_sandbox
@pytest.mark.parametrize("name", ["pal_tff", "ntsc_bff", "fmt_411_bff", "fmt_422p10_tff"])
def test_oracle_positive_and_negative_controls(name):
    clip = get_clip(name)
    mi = media_info(clip)
    src = read_frames(clip, mi.pix_fmt, mi.width, mi.height)
    tff = NAMED[name].kind == "tff"
    # each frame emitted twice carries both original fields intact -> 0 mismatches
    dup = [f for f in src for _ in (0, 1)]
    bad, total = count_field_mismatches(src, dup, tff, "double")
    assert bad == 0 and total == 6 * len(src)
    # single rate: frames themselves
    assert count_field_mismatches(src, src, tff, "single")[0] == 0
    # shifted by one frame -> must fail
    shifted = dup[1:] + dup[:1]
    assert count_field_mismatches(src, shifted, tff, "double")[0] > 0
    # one perturbed sample in the first field of frame 3 -> must fail
    broken = [[p.copy() for p in f] for f in dup]
    first = 0 if tff else 1
    broken[6][0][first, 0] ^= 1
    assert count_field_mismatches(src, broken, tff, "double") == (1, total)
    # line order reversed inside each frame (fields swap parity) -> must fail
    swapped = [[p[::-1].copy() for p in f] for f in dup]  # reverse line order inside each frame
    assert count_field_mismatches(src, swapped, tff, "double")[0] > 0


@pytest.mark.needs_sandbox
@pytest.mark.parametrize("name", ["pal_tff", "ntsc_bff", "fmt_422p10_bff"])
def test_oracle_against_ffmpeg_bwdif_bob(name, tmp_path):
    """bwdif send_field keeps the original field lines of each output frame: a real bob that the
    oracle must accept with the right order and reject with the wrong one."""
    spec = NAMED[name]
    tff = spec.kind == "tff"
    clip = get_clip(name)
    mi = media_info(clip)
    out = tmp_path / "bob.mkv"
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-vf",
                    f"bwdif=mode=send_field:parity={'tff' if tff else 'bff'}:deint=all",
                    "-an", "-c:v", "ffv1", str(out)], check=True)
    src = read_frames(clip, mi.pix_fmt, mi.width, mi.height)
    got = read_frames(out, mi.pix_fmt, mi.width, mi.height)
    assert len(got) == 2 * len(src)
    assert count_field_mismatches(src, got, tff, "double")[0] == 0
    assert count_field_mismatches(src, got, not tff, "double")[0] > 0
