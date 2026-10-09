"""Bundled native plugins (WP-F): manifest consistency, and loading/QTGMC with a built plugin dir.

Build the plugins first (inside tools/archrun --user):
    bash packaging/plugins/build.sh fetch ~/plugin-src
    bash packaging/plugins/build.sh all ~/plugin-src /workspace/qtgmc/sandbox/plugins
then run with QTGMC6UI_PLUGIN_DIR=/workspace/qtgmc/sandbox/plugins.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[1] / "packaging" / "plugins"
MANIFEST = json.loads((PLUGINS / "plugins.json").read_text(encoding="utf-8"))
COMPONENTS = {c["name"]: c for c in MANIFEST["components"]}


def _manifest(*args: str) -> str:
    return subprocess.run(
        [sys.executable, "-I", str(PLUGINS / "manifest.py"), *args], check=True, capture_output=True, text=True
    ).stdout


def test_manifest_consistent() -> None:
    assert set(MANIFEST["sets"]["required"]) == {"akarin", "resize2", "mvutensils", "znedi3", "zsmooth"}
    filenames: dict[str, str] = {}
    for c in MANIFEST["components"]:
        assert (PLUGINS / "recipes" / f"{c['name']}.sh").is_file(), c["name"]
        for key in ("version", "tag", "license", "license_files", "namespace", "identifier", "files", "depends", "makedepends"):
            assert c[key], (c["name"], key)
        assert c["files"][0].endswith(".so")
        for s in c["sources"] + c.get("python_wheels", []):
            assert re.fullmatch(r"[0-9a-f]{64}", s["sha256"]), s["filename"]
            assert s["url"].startswith("https://"), s["url"]
            assert "-git" not in s["url"] and not s["url"].startswith("git+")
            # identical file names must mean identical content (shared makepkg $srcdir)
            assert filenames.setdefault(s["filename"], s["sha256"]) == s["sha256"], s["filename"]
    for names in MANIFEST["sets"].values():
        assert set(names) <= set(COMPONENTS)
    # mvutensils v10 breaks vsjetpack 2.2.6
    assert COMPONENTS["mvutensils"]["tag"] == "v9"


def test_manifest_cli() -> None:
    assert _manifest("names").split() == MANIFEST["sets"]["all"]
    lines = _manifest("pkgbuild", "required").splitlines()
    n_src = sum(len(COMPONENTS[n]["sources"]) for n in MANIFEST["sets"]["required"])
    assert sum(ln.startswith("source+=(") for ln in lines) == n_src
    assert sum(ln.startswith("sha256sums+=(") for ln in lines) == n_src
    assert "llvm22-libs" in _manifest("depends").split()
    table = json.loads(_manifest("table", "required"))
    assert [r["name"] for r in table] == MANIFEST["sets"]["required"]


def _built(names: list[str]) -> list[str]:
    pdir = Path(os.environ["QTGMC6UI_PLUGIN_DIR"])
    return [n for n in names if (pdir / COMPONENTS[n]["files"][0]).is_file()]


@pytest.mark.needs_qtgmc
def test_plugins_load_without_autoload() -> None:
    names = _built(list(COMPONENTS))
    assert set(MANIFEST["sets"]["required"]) <= set(names)
    r = subprocess.run(
        [sys.executable, "-I", str(PLUGINS / "check.py"), os.environ["QTGMC6UI_PLUGIN_DIR"], *names],
        capture_output=True, text=True,
    )
    report = json.loads(r.stdout)
    assert r.returncode == 0, report["problems"]
    assert all(p["loaded"] for p in report["plugins"])


SMOKE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from check import no_autoload_core
import numpy as np
core = no_autoload_core()
for f in sys.argv[4:]:
    core.std.LoadPlugin(f)
from vsdeinterlace import QTempGaussMC
clip = core.bs.VideoSource(sys.argv[2], cachepath=sys.argv[3])[100:104]
out = QTempGaussMC(source_match_mode=QTempGaussMC.SourceMatchMode.TWICE_REFINED,
                   lossless_mode=QTempGaussMC.LosslessMode.POSTSMOOTH).bob(clip, tff=False)
fields = core.std.SeparateFields(clip, tff=False)
bad = 0
for n in range(out.num_frames):
    fo, fs = out.get_frame(n), fields.get_frame(n)
    for p in range(fo.format.num_planes):
        a, b = np.asarray(fo[p]), np.asarray(fs[p])
        bad += not (np.array_equal(a[0::2], b) or np.array_equal(a[1::2], b))
print(json.dumps({"frames": out.num_frames, "fps": str(out.fps), "mismatched": bad}))
"""


@pytest.mark.needs_qtgmc
@pytest.mark.needs_sandbox
def test_qtgmc_bob_four_frames(reference_clip: Path, tmp_path: Path) -> None:
    import vapoursynth

    bs = Path(vapoursynth.__file__).resolve().parent / "plugins" / "libbestsource.so"
    if not bs.is_file():
        pytest.skip("vapoursynth-plugin-bestsource not installed")
    pdir = Path(os.environ["QTGMC6UI_PLUGIN_DIR"])
    sos = [str(pdir / COMPONENTS[n]["files"][0]) for n in MANIFEST["sets"]["required"]]
    r = subprocess.run(
        [sys.executable, "-c", SMOKE, str(PLUGINS), str(reference_clip), str(tmp_path / "bsidx"), str(bs), *sos],
        capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res == {"frames": 8, "fps": "50", "mismatched": 0}
