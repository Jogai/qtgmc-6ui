#!/usr/bin/env python3
"""Download every pinned source and wheel of plugins.json and compare its sha256: verify-sources.py.

Network check for the weekly workflow (upstream-checks.yml): a deleted release, a re-tagged
archive or a moved URL shows up here and not when a user builds the package.
Prints one line per file; exits 1 if any download or checksum fails.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

MANIFEST = Path(__file__).resolve().with_name("plugins.json")
USER_AGENT = "qtgmc-6ui-upstream-check"


def sha256_of(url: str) -> str:
    h = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=120) as r:
        while chunk := r.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = 0
    seen: set[str] = set()
    for comp in data["components"]:
        for src in comp["sources"] + comp.get("python_wheels", []):
            if src["url"] in seen:
                continue
            seen.add(src["url"])
            try:
                got = sha256_of(src["url"])
            except OSError as e:
                got = f"download failed: {e}"
            ok = got == src["sha256"]
            bad += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {comp['name']:<11} {src['filename']}" + ("" if ok else f"  expected {src['sha256']} got {got}"))
    print(f"{len(seen)} files, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
