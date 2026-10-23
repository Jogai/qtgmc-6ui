#!/usr/bin/env python3
"""Compare the pinned versions with the newest upstream releases: check-upstream.py [--report FILE].

Looks at the primary source of every plugin in plugins.json (newest stable git tag of its upstream
repository), the pinned Zig toolchain (ziglang.org) and the Python packages pinned in
pyproject.toml / the dfttest2 wheel (PyPI). A newer version that holds.json lists as already
looked at ("seen") is shown as held with its reason and does not count; a version newer than the
seen one counts again. Prints a markdown table, writes it to FILE and, in GitHub Actions, sets
the step output ``news`` to the number of unheld newer versions. Always exits 0 unless a lookup
fails for every item.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PRERELEASE = re.compile(r"(?i)(rc|alpha|beta|pre|dev|test|nightly|snapshot)")


def key(tag: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", tag))


def stable_newest(tags: list[str]) -> str | None:
    tags = [t for t in tags if key(t) and not PRERELEASE.search(t)]
    return max(tags, key=key, default=None)


def git_tags(upstream: str) -> list[str]:
    out = subprocess.run(["git", "ls-remote", "--tags", "--refs", upstream], check=True, capture_output=True,
                         text=True, timeout=120).stdout
    return [line.split("refs/tags/", 1)[1] for line in out.splitlines() if "refs/tags/" in line]


def get_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "qtgmc-6ui-upstream-check"}), timeout=60) as r:
        return json.load(r)


def pypi_newest(name: str) -> str | None:
    return stable_newest(list(get_json(f"https://pypi.org/pypi/{name}/json")["releases"]))


def items() -> list[tuple[str, str, str, Callable[[], str | None]]]:
    """(name, pinned, where, lookup)"""
    data = json.loads((HERE / "plugins.json").read_text(encoding="utf-8"))
    rows = []
    for c in data["components"]:
        rows.append((c["name"], c["tag"], c["upstream"], lambda u=c["upstream"]: stable_newest(git_tags(u + ".git"))))
        for s in c["sources"]:
            if s.get("toolchain"):
                rows.append(("zig", s["toolchain"], "ziglang.org",
                             lambda: stable_newest([k for k in get_json("https://ziglang.org/download/index.json") if k != "master"])))
        for w in c.get("python_wheels", []):
            m = re.match(r"([A-Za-z0-9_]+)-([0-9][^-]*)-", w["filename"])
            if m:
                rows.append((m[1], m[2], "PyPI", lambda n=m[1]: pypi_newest(n)))
    rows = list({(r[0], r[1]): r for r in rows}.values())  # Zig is pinned by two components
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    for d in deps:
        if m := re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9][^\s;]*)", d):
            rows.append((m[1], m[2], "PyPI", lambda n=m[1]: pypi_newest(n)))
    return rows


def main(argv: list[str]) -> int:
    report = Path(argv[argv.index("--report") + 1]) if "--report" in argv else None
    holds = json.loads((HERE / "holds.json").read_text(encoding="utf-8"))
    lines = ["| Component | Pinned | Newest stable | Status |", "|---|---|---|---|"]
    news = failed = 0
    rows = items()
    for name, pinned, where, lookup in rows:
        try:
            newest = lookup()
        except Exception as e:  # noqa: BLE001 - a lookup failure must not hide the other rows
            failed += 1
            lines.append(f"| {name} | {pinned} | ? | lookup failed ({where}): {e} |")
            continue
        if newest is None or key(newest) <= key(pinned):
            status = "current"
        elif (h := holds.get(name)) and key(newest) <= key(h["seen"]):
            status = f"held at {h['seen']}: {h['reason']}"
        else:
            status = f"**newer** ({where})"
            news += 1
        lines.append(f"| {name} | {pinned} | {newest or '?'} | {status} |")
    text = "\n".join(lines) + "\n"
    print(text)
    if report:
        report.write_text(text, encoding="utf-8")
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"news={news}\n")
    print(f"{news} newer, {failed} lookups failed, {len(rows)} checked", file=sys.stderr)
    return 1 if failed == len(rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
