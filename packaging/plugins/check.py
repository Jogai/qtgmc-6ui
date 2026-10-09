#!/usr/bin/env python3
"""Check an installed plugin dir: check.py DESTDIR COMPONENT...

Loads each component's plugin with a fresh VapourSynth core that has autoloading
disabled (in manifest order), compares namespace/identifier with plugins.json and
scans every installed file for references to build trees (namcap-style).
Prints a JSON table on stdout; exits 1 on any problem.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().with_name("plugins.json")
# Substrings that must never appear in an installed file.
BAD_REFS = [b"/qtgmc6ui-plugins-build", b"/build/", b"/startdir/", b"/pkg/qtgmc-6ui"]


def no_autoload_core():
    import vapoursynth as vs

    class Policy(vs.EnvironmentPolicy):
        def on_policy_registered(self, api):
            self.env = api.create_environment(vs.DISABLE_AUTO_LOADING)
            self.cur = None

        def on_policy_cleared(self):
            pass

        def get_current_environment(self):
            return self.cur

        def set_environment(self, env):
            old, self.cur = self.cur, env
            return old

    policy = Policy()
    vs.register_policy(policy)
    policy.cur = policy.env
    core = vs.core.core
    assert core.flags & vs.DISABLE_AUTO_LOADING, "autoload still enabled"
    return core


def main(argv: list[str]) -> int:
    dest = Path(argv[0]).resolve()
    wanted = argv[1:]
    comps = {c["name"]: c for c in json.loads(MANIFEST.read_text())["components"]}
    extra_refs = [os.fsencode(p) for p in filter(None, [os.environ.get("SRCDIR"), os.environ.get("QTGMC6UI_BUILDDIR")])]
    core = no_autoload_core()
    base = {p.namespace for p in core.plugins()}
    problems: list[str] = []
    rows = []
    for name in wanted:
        c = comps[name]
        so = dest / c["files"][0]
        row = {"name": name, "file": so.name, "version": c["tag"], "required": c["required"], "origin": "bundled"}
        for f in c["files"]:
            if not (dest / f).is_file():
                problems.append(f"{name}: {f} missing")
            else:
                data = (dest / f).read_bytes()
                for ref in BAD_REFS + extra_refs:
                    if ref in data:
                        problems.append(f"{name}: {f} contains build path {ref.decode()!r}")
        try:
            before = {p.identifier for p in core.plugins()}
            core.std.LoadPlugin(path=str(so))
            new = [p for p in core.plugins() if p.identifier not in before]
            if len(new) != 1:
                raise RuntimeError(f"expected one new plugin, got {[p.identifier for p in new]}")
            p = new[0]
            row.update(namespace=p.namespace, identifier=p.identifier, loaded=True)
            if (p.namespace, p.identifier) != (c["namespace"], c["identifier"]):
                problems.append(
                    f"{name}: loaded {p.namespace}/{p.identifier}, manifest says {c['namespace']}/{c['identifier']}"
                )
        except Exception as e:  # noqa: BLE001
            row.update(loaded=False, error=str(e))
            problems.append(f"{name}: LoadPlugin failed: {e}")
        rows.append(row)
    loaded = sorted({p.namespace for p in core.plugins()} - base)
    print(json.dumps({"plugin_dir": str(dest), "plugins": rows, "namespaces_added": loaded, "problems": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
