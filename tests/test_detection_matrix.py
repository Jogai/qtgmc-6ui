"""Classification of every synthetic clip == expected, and the CLI blocking rules:
telecined / mixed blocked with the IVTC explanation, progressive not deinterlaced by default,
--force overrides."""

from __future__ import annotations

import pytest

from fixtures_gen import NAMED, get_clip
from helpers import cli_json, deint_master, media_info

pytestmark = pytest.mark.needs_sandbox

# one clip per distinct (expected class) is enough for the slow tier; the rest are fast
CLASSIFIED = list(NAMED)
BIG = {"res_1440x1080_tff", "res_1920x1080_tff", "res_1920x1080_bff", "prog_hd"}


@pytest.mark.parametrize("name", [pytest.param(n, marks=pytest.mark.slow) if n in BIG else n
                                  for n in CLASSIFIED])
def test_classification_matches_expected(name, isolated_dirs):
    spec = NAMED[name]
    rc, doc, err = cli_json("analyze", str(get_clip(name)), "--no-cache")
    assert rc == 0, err
    got = doc["classification"]["content"]
    assert got == spec.expected, (
        f"{name}: got {got} expected {spec.expected}; reasons={doc['classification'].get('reasons')}")
    if spec.kind in ("tff", "bff"):
        assert doc["classification"]["field_order"] == spec.kind
        assert doc["recommendation"]["action"] == "deinterlace"


@pytest.mark.parametrize("name,code", [("ntsc_tele", "blocked_telecined"), ("ntsc_mixed", "blocked_mixed")])
def test_telecined_and_mixed_are_blocked_with_ivtc_text(name, code, tmp_path, isolated_dirs):
    out = tmp_path / "x.mkv"
    rc, doc, err = cli_json("deint", str(get_clip(name)), "-o", str(out))
    assert rc == 3, (rc, doc, err)
    e = doc["error"]
    assert e["code"] == code
    text = (e["message"] + " " + (e.get("hint") or "")).lower()
    assert "ivtc" in text or "inverse telecine" in text
    assert "--force" in text
    assert not out.exists(), "a blocked job must not leave an output file"


def test_progressive_not_deinterlaced_by_default(tmp_path, isolated_dirs):
    out = tmp_path / "x.mkv"
    rc, doc, err = cli_json("deint", str(get_clip("prog_pal")), "-o", str(out))
    assert rc == 3, (rc, doc, err)
    assert doc["error"]["code"] == "blocked_progressive"
    assert not out.exists()


def test_undetermined_needs_explicit_field_order(tmp_path, isolated_dirs):
    out = tmp_path / "x.mkv"
    rc, doc, err = cli_json("deint", str(get_clip("prog_detail")), "-o", str(out))
    assert rc == 3, (rc, doc, err)
    assert doc["error"]["code"] in ("blocked_undetermined", "blocked_progressive")
    assert not out.exists()


@pytest.mark.needs_qtgmc
@pytest.mark.parametrize("name", ["ntsc_tele", "prog_pal"])
def test_force_overrides_block(name, tmp_path, isolated_dirs):
    out = tmp_path / "forced.mkv"
    deint_master(get_clip(name), out, "--force", field_order="tff")
    src, mi = media_info(get_clip(name)), media_info(out)
    assert mi.frames == 2 * src.frames


@pytest.mark.needs_qtgmc
def test_explicit_field_order_unblocks_undetermined(tmp_path, isolated_dirs):
    out = tmp_path / "x.mkv"
    deint_master(get_clip("prog_detail"), out, field_order="tff")
    assert media_info(out).frames == 2 * media_info(get_clip("prog_detail")).frames
