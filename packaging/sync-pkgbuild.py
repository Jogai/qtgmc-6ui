#!/usr/bin/env python3
"""Regenerate (or --check) the generated parts of packaging/aur/PKGBUILD and .SRCINFO.

  python3 -I packaging/sync-pkgbuild.py           rewrite PKGBUILD (+ .SRCINFO if makepkg exists)
  python3 -I packaging/sync-pkgbuild.py --check   exit 1 if anything is out of date
  python3 -I packaging/sync-pkgbuild.py --project-sha256 auto|SHA|SKIP
                                                  set the checksum of the project tarball
                                                  (auto: download $url/archive/v$pkgver.tar.gz)

Release: tag v$pkgver, push, then `--project-sha256 auto`.  Do NOT use updpkgsums: it merges all
sums into one array.  (If it was run anyway, this script folds its result back: the first sum
becomes _project_sha256 and the rest is regenerated.)

Sources of truth:
  pkgver                     src/qtgmc_6ui/__init__.py (__version__)
  "plugin-deps",             packaging/plugins/plugins.json via plugins/manifest.py (WP-F):
  "plugin-sources" blocks    plugin set, runtime/build deps, native plugin tarballs and the
                             python wheels of plugins (dfttest2)
  "python" block             uv.lock: the pinned py3-none-any wheels of the bundled Python
                             packages (vsjetpack, jetpytools); hashes are uv.lock's sha256
  "licenses" block           license=() from pyproject.toml (the app, first) + plugins.json
                             licences + EMBEDDED/PY_LICENSES below; _extra_licenses = licence
                             texts of sub-sources, extracted from the tarballs in package()

A block is the text between "# BEGIN generated: NAME" and "# END generated: NAME".
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent
PKGBUILD = PKG / "aur" / "PKGBUILD"
SRCINFO = PKG / "aur" / ".SRCINFO"
MANIFEST_PY = PKG / "plugins" / "manifest.py"
PLUGIN_SET = "all"
PROJECT_SUMS = 'sha256sums=("$_project_sha256")'

# Licences of code compiled into a plugin that plugins.json does not list as a source of its
# own (verified in the pinned tarballs: file headers / licence files).
EMBEDDED = {
    "akarin": ["Apache-2.0",  # expr2/reactor: SwiftShader Reactor (Apache-2.0 file headers)
               "MIT"],        # text/fmt: {fmt} (MIT with binary-redistribution exception)
    "dfttest2": ["Apache-2.0"],  # vs-dfttest2/cpu_source/vectorclass/LICENSE
}
# Bundled Python packages from uv.lock (checked against the wheels' licence files).
PY_LICENSES = {"vsjetpack": "MIT", "jetpytools": "MIT"}
# Plain names in plugins.json mean the version-only licence (no "or later" in the sources).
SPDX_FIX = {"LGPL-2.1": "LGPL-2.1-only", "GPL-2.0": "GPL-2.0-only", "LGPL-3.0": "LGPL-3.0-only",
            "GPL-3.0": "GPL-3.0-only"}
# Licence texts of sub-sources (source filename -> member below the tarball's top dir,
# installed as /usr/share/licenses/qtgmc-6ui/<dest>).  The main source of every component
# is handled by build.sh (plugins.json license_files).
EXTRA_LICENSE_FILES = {
    "zimg-1ad1895d5ff0bbe69c61243f9996aede713d1b5f.tar.gz": ("COPYING", "resize2/zimg-COPYING"),
    "graphengine-cb5b2ce13384ec2491f0c37256ea210034799f69.tar.gz": ("COPYING", "resize2/graphengine-COPYING"),
    "graphengine-7bb77a5c062c610e7f44df6a59155cc3ab3e452a.tar.gz": ("COPYING", "znedi3/graphengine-COPYING"),
    "vsxx-11eb026af5517d69e1d3d6258d1b2c2040c508e7.tar.gz": ("COPYING.LGPLv2.1", "znedi3/vsxx-COPYING.LGPLv2.1"),
    "vapoursynth-zig-344caabeae1f4168b69ea6975d6ef244583e7c63.tar.gz": ("LICENSE", "zsmooth/vapoursynth-zig-LICENSE"),
    "adworacz-fftw-579b516e6b39a2a96200d248fc3c96d1ad7d9841.tar.gz": ("LICENSE", "zsmooth/fftw-zig-LICENSE"),
    "fftw-3.3.11.tar.gz": ("COPYRIGHT", "zsmooth/fftw-COPYRIGHT"),
    "vapoursynth_dfttest2_cpu-11.1.0.tar.gz": ("vs-dfttest2/cpu_source/vectorclass/LICENSE",
                                               "dfttest2/vectorclass-LICENSE"),
}


def q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def project_version() -> str:
    text = (ROOT / "src" / "qtgmc_6ui" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise SystemExit("sync-pkgbuild: __version__ not found")
    return m.group(1)


def _pick_wheel(pkg: dict) -> dict | None:
    """Pure-python wheels only: a cpXY wheel would tie the package to one Python minor
    more tightly than the rest of the venv already is."""
    for w in pkg.get("wheels", []):
        fn = w["url"].rsplit("/", 1)[1]
        py, abi, plat = fn[:-4].split("-")[-3:]
        if plat == "any" and abi == "none" and py.startswith(("py3", "py2.py3")):
            return w
    return None


def python_block() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    lines = []
    for p in sorted(lock["package"], key=lambda p: p["name"]):
        if "registry" not in p.get("source", {}):
            continue  # the project itself (editable)
        w = _pick_wheel(p)
        if w is None:
            raise SystemExit(f"sync-pkgbuild: no pure-python wheel for {p['name']} in uv.lock "
                             "(add an sdist build path or pick another version)")
        fn = w["url"].rsplit("/", 1)[1]
        algo, digest = w["hash"].split(":", 1)
        if algo != "sha256":
            raise SystemExit(f"sync-pkgbuild: {p['name']}: unexpected hash {algo}")
        lines += [f"source+=({q(fn + '::' + w['url'])})  # {p['name']} {p['version']}",
                  f"sha256sums+=({q(digest)})",
                  f"noextract+=({q(fn)})"]
    return "\n".join(lines)


def _manifest(*args: str) -> str:
    out = subprocess.run([sys.executable, "-I", str(MANIFEST_PY), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


# gcc-libs is a meta package on current Arch; namcap asks for the libraries actually linked.
DEP_ALIASES = {"gcc-libs": ["libgcc", "libstdc++"]}


def static_depends(text: str) -> set[str]:
    """Names in the hand-written depends=( ... ) array of the PKGBUILD."""
    m = re.search(r"^depends=\((.*?)^\)", text, re.S | re.M)
    return {w.strip("'\"") for w in m.group(1).split() if not w.startswith('"$')} if m else set()


def plugin_deps_block(text: str) -> str:
    deps: list[str] = []
    for d in _manifest("depends", PLUGIN_SET).split():
        deps += [x for x in DEP_ALIASES.get(d, [d]) if x not in deps]
    # makedepends that are runtime depends anyway would only draw a namcap warning
    runtime = set(deps) | static_depends(text)
    mk = [d for d in _manifest("makedepends", PLUGIN_SET).split() if d not in runtime]
    return "\n".join([
        f"_plugin_set={PLUGIN_SET}",
        "_plugin_depends=(" + " ".join(deps) + ")",
        "_plugin_makedepends=(" + " ".join(mk) + ")",
    ])


def plugin_sources_block() -> str:
    return _manifest("pkgbuild", PLUGIN_SET)


def _spdx(expr: str) -> list[str]:
    return [SPDX_FIX.get(x.strip(), x.strip()) for x in expr.split(" AND ") if x.strip()]


def licenses_block() -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    app = pyproject["project"]["license"]
    data = json.loads((PKG / "plugins" / "plugins.json").read_text(encoding="utf-8"))
    names = set(_manifest("names", PLUGIN_SET).split())
    found: set[str] = set()
    extra: list[str] = []
    for c in data["components"]:
        if c["name"] not in names:
            continue
        found.update(_spdx(c["license"]))
        for src in c["sources"] + c.get("python_wheels", []):
            found.update(_spdx(src.get("license", "")))
            if src["filename"] in EXTRA_LICENSE_FILES:
                member, dest = EXTRA_LICENSE_FILES[src["filename"]]
                extra.append(q(f"{src['filename']}|{member}|{dest}"))
        found.update(EMBEDDED.get(c["name"], []))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    for p in lock["package"]:
        if "registry" in p.get("source", {}):
            if p["name"] not in PY_LICENSES:
                raise SystemExit(f"sync-pkgbuild: licence of {p['name']} unknown: add it to PY_LICENSES")
            found.add(PY_LICENSES[p["name"]])
    found.discard(app)
    return "\n".join([
        "license=(" + " ".join(q(x) for x in [app, *sorted(found)]) + ")",
        "_extra_licenses=(" + "\n                 ".join(extra) + ")",
    ])


def fold_project_sums(text: str, sha: str | None) -> str:
    """Restore `sha256sums=("$_project_sha256")` (e.g. after updpkgsums merged every sum into
    one array) and set _project_sha256 (explicit value > first merged sum > unchanged)."""
    body = re.sub(r"^# BEGIN generated: .*?^# END generated: [^\n]*$", "", text, flags=re.S | re.M)
    m = re.search(r"^sha256sums=\((.*?)\)[^\n]*$", body, re.S | re.M)
    if m is None:
        raise SystemExit("sync-pkgbuild: no top-level sha256sums=( ) in PKGBUILD")
    if m.group(0).split("  #")[0].strip() != PROJECT_SUMS:
        first = re.findall(r"['\"]?([0-9a-fA-F]{64}|SKIP)['\"]?", m.group(1))
        if sha is None and first:
            sha = first[0]
        text = text.replace(m.group(0), PROJECT_SUMS, 1)
    if sha is not None:
        text, n = re.subn(r"^_project_sha256=.*$", f"_project_sha256={q(sha)}", text, count=1, flags=re.M)
        if n != 1:
            raise SystemExit("sync-pkgbuild: no _project_sha256= line")
    return text


def fetch_project_sha256(text: str) -> str:
    url = re.search(r'^url="([^"]+)"', text, re.M).group(1)
    if "<" in url:
        raise SystemExit("sync-pkgbuild: set url= in the PKGBUILD first (it still has a placeholder)")
    tarball = f"{url}/archive/v{project_version()}.tar.gz"
    h = hashlib.sha256()
    with urllib.request.urlopen(tarball, timeout=60) as r:
        while chunk := r.read(1 << 16):
            h.update(chunk)
    print(f"sync-pkgbuild: {tarball}: {h.hexdigest()}", file=sys.stderr)
    return h.hexdigest()


def render(text: str, project_sha: str | None = None) -> str:
    text = fold_project_sums(text, project_sha)
    text, n = re.subn(r"^pkgver=.*$", f"pkgver={project_version()}", text, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("sync-pkgbuild: no pkgver= line")
    if not MANIFEST_PY.is_file():
        raise SystemExit(f"sync-pkgbuild: {MANIFEST_PY} missing (WP-F plugin manifest)")
    blocks = (("plugin-deps", plugin_deps_block(text)), ("plugin-sources", plugin_sources_block()),
              ("python", python_block()), ("licenses", licenses_block()))
    for name, body in blocks:
        pat = re.compile(rf"(^# BEGIN generated: {re.escape(name)} [^\n]*\n).*?(^# END generated: {re.escape(name)}$)",
                         re.S | re.M)
        if not pat.search(text):
            raise SystemExit(f"sync-pkgbuild: block '{name}' missing in PKGBUILD")
        text = pat.sub(lambda m, body=body: m.group(1) + body + "\n" + m.group(2), text)
    return text


def srcinfo() -> str | None:
    if shutil.which("makepkg") is None:
        return None
    out = subprocess.run(["makepkg", "--printsrcinfo"], cwd=PKGBUILD.parent, capture_output=True,
                         text=True, check=True, env={"PATH": "/usr/bin", "LC_ALL": "C"})
    return out.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="sync packaging/aur/PKGBUILD with its sources of truth")
    ap.add_argument("--check", action="store_true", help="only report; exit 1 when out of date")
    ap.add_argument("--project-sha256", metavar="auto|SHA|SKIP",
                    help="checksum of the project tarball (auto: download the tagged release)")
    a = ap.parse_args(argv)
    old = PKGBUILD.read_text(encoding="utf-8")
    sha = a.project_sha256
    if sha == "auto":
        sha = fetch_project_sha256(old)
    elif sha is not None and sha != "SKIP" and not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise SystemExit("sync-pkgbuild: --project-sha256 needs auto, SKIP or 64 hex digits")
    if a.check and sha is not None:
        raise SystemExit("sync-pkgbuild: --check and --project-sha256 are exclusive")
    new = render(old, sha)
    stale = []
    if new != old:
        stale.append("PKGBUILD")
        if a.check:
            sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                       "PKGBUILD", "PKGBUILD (expected)"))
        else:
            PKGBUILD.write_text(new, encoding="utf-8")
    if not a.check or not stale:
        info = srcinfo()
        if info is None:
            print("sync-pkgbuild: makepkg not found, .SRCINFO not checked/regenerated", file=sys.stderr)
        elif not SRCINFO.is_file() or SRCINFO.read_text(encoding="utf-8") != info:
            stale.append(".SRCINFO")
            if not a.check:
                SRCINFO.write_text(info, encoding="utf-8")
    if a.check:
        print("sync-pkgbuild: out of date: " + ", ".join(stale) if stale else "sync-pkgbuild: up to date",
              file=sys.stderr)
        return 1 if stale else 0
    print("sync-pkgbuild: updated " + ", ".join(stale) if stale else "sync-pkgbuild: nothing to do",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
