#!/usr/bin/env python3
"""Write /usr/lib/qtgmc-6ui/build-info.json at package() time (schema: models.BuildInfo).

Run it with the freshly built venv interpreter so the recorded Python is the one the app uses:

  "$venv/bin/python" -I packaging/write-build-info.py --out "$pkgdir/usr/lib/qtgmc-6ui/build-info.json" \
      --plugin-dir "$pkgdir/usr/lib/qtgmc-6ui/plugins" --manifest packaging/plugins/manifest.py \
      --site-packages "$venv/lib/python3.14/site-packages" --pkgver 0.1.0 --pkgrel 1 [--march x86-64]

Every bundled plugin is loaded (autoloading disabled, one fresh core each) to record its
namespace/identifier; a plugin that fails to load aborts the build.  Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

BUILD_INFO_SCHEMA = 1  # keep equal to qtgmc_6ui.models.BUILD_INFO_SCHEMA
REPO_PLUGINS = {"bestsource": ("vapoursynth-plugin-bestsource", "libbestsource.so")}


def pacman_version(pkg: str) -> str:
    try:
        out = subprocess.run(["pacman", "-Q", pkg], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""
    return out.split()[1] if len(out.split()) == 2 else ""


class _Loader:
    """One DISABLE_AUTO_LOADING environment per plugin, so the build host's autoloaded or
    duplicate plugins can never mask a broken bundled one."""

    def __init__(self) -> None:
        import vapoursynth as vs

        self.vs = vs

        class Policy(vs.EnvironmentPolicy):
            def on_policy_registered(self, api):
                self.api, self.cur = api, None

            def on_policy_cleared(self):
                pass

            def get_current_environment(self):
                return self.cur

            def set_environment(self, env):
                old, self.cur = self.cur, env
                return old

        self.policy = Policy()
        vs.register_policy(self.policy)

    def _fresh(self, fn):
        p = self.policy
        env = p.cur = p.api.create_environment(self.vs.DISABLE_AUTO_LOADING)
        try:
            return fn(self.vs.core.core)
        finally:
            p.cur = None
            p.api.destroy_environment(env)

    def load(self, path: Path) -> dict:
        def _load(core):
            before = {pl.identifier for pl in core.plugins()}
            core.std.LoadPlugin(path=str(path))
            new = [pl for pl in core.plugins() if pl.identifier not in before]
            if len(new) != 1:
                raise RuntimeError(f"{path.name}: expected one new plugin, got {len(new)}")
            return {"namespace": new[0].namespace, "identifier": new[0].identifier}

        return self._fresh(_load)

    def runtime(self) -> tuple[int, list[int]]:
        return self._fresh(lambda core: (core.core_version.release_major,
                                         [core.api_version.api_major, core.api_version.api_minor]))


def manifest_table(manifest_py: Path, plugin_set: str) -> list[dict]:
    """WP-F's `manifest.py table SET`: bundled plugins in load order with pinned versions."""
    out = subprocess.run([sys.executable, "-I", str(manifest_py), "table", plugin_set],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def plugin_table(plugin_dir: Path, rows: list[dict], loader: _Loader) -> list[dict]:
    """Verify each manifest row against the staged binary (namespace and identifier must
    match) and refuse stray .so files, so build-info always describes what is shipped."""
    table = []
    for row in rows:
        so = plugin_dir / row["file"]
        if not so.is_file():
            raise RuntimeError(f"{row['name']}: {so} was not built")
        ids = loader.load(so)
        for key in ("namespace", "identifier"):
            if row.get(key) and row[key] != ids[key]:
                raise RuntimeError(f"{so.name}: {key} {ids[key]!r} != manifest {row[key]!r}")
        table.append({**row, **ids, "origin": "bundled"})
    stray = sorted({p.name for p in plugin_dir.glob("*.so")} - {r["file"] for r in rows})
    if stray:
        raise RuntimeError(f"plugin files not in the manifest: {', '.join(stray)}")
    import vapoursynth

    for name, (pkg, fname) in REPO_PLUGINS.items():
        so = Path(vapoursynth.__file__).resolve().parent / "plugins" / fname
        if not so.is_file():
            raise RuntimeError(f"repo plugin {so} missing (install {pkg})")
        ids = loader.load(so)
        table.append({"name": name, **ids, "version": pacman_version(pkg), "file": str(so),
                      "required": True, "origin": "repo"})
    return table


def python_packages(site: Path) -> dict[str, str]:
    dists = importlib.metadata.distributions(path=[str(site)])
    return dict(sorted((d.metadata["Name"].lower(), d.version) for d in dists))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--plugin-dir", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True, help="packaging/plugins/manifest.py")
    ap.add_argument("--plugin-set", default="all")
    ap.add_argument("--site-packages", type=Path, required=True)
    ap.add_argument("--pkgver", required=True)
    ap.add_argument("--pkgrel", required=True)
    ap.add_argument("--march", default="x86-64")
    a = ap.parse_args(argv)

    loader = _Loader()
    core_major, api = loader.runtime()
    sde = int(os.environ.get("SOURCE_DATE_EPOCH", "0") or 0)
    major, minor = sys.version_info[:2]
    info = {
        "schema": BUILD_INFO_SCHEMA,
        "pkgver": a.pkgver,
        "pkgrel": a.pkgrel,
        "built_at": dt.datetime.fromtimestamp(sde, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "march": a.march,
        "python_version": platform.python_version(),
        "python_minor": [major, minor],
        "python_executable": f"/usr/bin/python{major}.{minor}",
        "vapoursynth_pkgver": pacman_version("vapoursynth"),
        "vapoursynth_core": core_major,
        "vapoursynth_api": api,
        "ffmpeg_pkgver": pacman_version("ffmpeg"),
        "pyside6_pkgver": pacman_version("pyside6"),
        "plugins": plugin_table(a.plugin_dir, manifest_table(a.manifest, a.plugin_set), loader),
        "python_packages": python_packages(a.site_packages),
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(f"build-info: python {info['python_version']}, VS R{core_major} API {api[0]}.{api[1]}, "
          f"{len(info['plugins'])} plugins, packages {info['python_packages']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
