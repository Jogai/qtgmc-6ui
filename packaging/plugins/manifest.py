#!/usr/bin/env python3
"""Query plugins.json (the pin manifest for the bundled VapourSynth plugins).

  manifest.py names [SET...]        component names of the sets/components given
                                    (default: set 'all' = what the package ships;
                                    'everything' = every component incl. extras)
  manifest.py sources [SET...]      TSV lines: component filename url sha256
  manifest.py pkgbuild [SET...]     bash lines for a PKGBUILD: source+=(...) sha256sums+=(...)
                                    noextract+=(...)  (python wheels included)
  manifest.py depends [SET...]      runtime Arch packages, one per line (sorted, unique)
  manifest.py makedepends [SET...]  build-time Arch packages, one per line (sorted, unique)
  manifest.py get COMPONENT KEY     one field (lists joined by spaces)
  manifest.py zighashes COMPONENT   TSV lines: filename zig_hash
  manifest.py toolchain COMPONENT   TSV lines: filename version (pinned build toolchains)
  manifest.py table [SET...]        JSON list for build-info (name namespace identifier version
                                    file required origin), in load order

Run it with `python3 -I` so nothing from the current directory is imported.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().with_name("plugins.json")


def load() -> dict:
    with MANIFEST.open(encoding="utf-8") as f:
        return json.load(f)


def select(data: dict, args: list[str]) -> list[dict]:
    by_name = {c["name"]: c for c in data["components"]}
    wanted: list[str] = []
    for a in args or ["all"]:
        if a == "everything":
            names = list(by_name)
        elif a in data["sets"]:
            names = data["sets"][a]
        elif a in by_name:
            names = [a]
        else:
            raise SystemExit(f"manifest.py: unknown component or set: {a}")
        wanted += [n for n in names if n not in wanted]
    order = list(by_name)
    return [by_name[n] for n in sorted(wanted, key=order.index)]


def all_sources(comps: list[dict]):
    for c in comps:
        for s in c["sources"]:
            yield c, s, False
        for s in c.get("python_wheels", []):
            yield c, s, True


def q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    data = load()
    if cmd == "names":
        print("\n".join(c["name"] for c in select(data, args)))
    elif cmd == "sources":
        for c, s, _ in all_sources(select(data, args)):
            print(f"{c['name']}\t{s['filename']}\t{s['url']}\t{s['sha256']}")
    elif cmd == "pkgbuild":
        comps = select(data, args)
        for c, s, _wheel in all_sources(comps):
            print(f"source+=({q(s['filename'] + '::' + s['url'])})  # {c['name']}")
            print(f"sha256sums+=({q(s['sha256'])})")
            print(f"noextract+=({q(s['filename'])})")
    elif cmd in ("depends", "makedepends"):
        out: set[str] = set(data["common_makedepends"]) if cmd == "makedepends" else set()
        for c in select(data, args):
            out.update(c[cmd])
        print("\n".join(sorted(out)))
    elif cmd == "get":
        if len(args) != 2:
            raise SystemExit("usage: manifest.py get COMPONENT KEY")
        (c,) = select(data, args[:1])
        v = c.get(args[1], "")
        print(" ".join(map(str, v)) if isinstance(v, list) else v)
    elif cmd == "zighashes":
        (c,) = select(data, args[:1])
        for s in c["sources"]:
            if "zig_hash" in s:
                print(f"{s['filename']}\t{s['zig_hash']}")
    elif cmd == "toolchain":
        (c,) = select(data, args[:1])
        for s in c["sources"]:
            if "toolchain" in s:
                print(f"{s['filename']}\t{s['toolchain']}")
    elif cmd == "table":
        rows = [
            {
                "name": c["name"],
                "namespace": c["namespace"],
                "identifier": c["identifier"],
                "version": c["tag"],
                "file": c["files"][0],
                "required": c["required"],
                "origin": "bundled",
            }
            for c in select(data, args)
        ]
        print(json.dumps(rows, indent=2))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
