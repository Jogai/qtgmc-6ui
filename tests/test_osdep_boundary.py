"""Guard rail: POSIX-only constructs live in osdep*.py only (the Windows-port seam).

AST scan of src/qtgmc_6ui: forbidden calls/attributes/imports/keywords, and string literals
(including docstrings) that hard-code POSIX paths, XDG variables or .so names."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "qtgmc_6ui"
ALLOWED = {"osdep.py", "osdep_windows.py"}

FORBIDDEN_ATTRS = {
    ("os", "killpg"), ("os", "getpgid"), ("os", "setsid"), ("os", "fork"), ("os", "getppid"),
    ("signal", "SIGKILL"), ("signal", "SIGTERM"), ("signal", "SIGINT"), ("signal", "SIGBREAK"),
    ("signal", "CTRL_BREAK_EVENT"), ("signal", "signal"), ("os", "kill"),
}
FORBIDDEN_IMPORTS = {"fcntl", "termios", "ctypes", "pwd", "grp", "resource", "signal"}
FORBIDDEN_KEYWORDS = {"preexec_fn", "start_new_session", "creationflags", "process_group"}
FORBIDDEN_STRINGS = [
    (re.compile(r"/proc\b"), "/proc"),
    (re.compile(r"XDG_"), "XDG_*"),
    (re.compile(r"(^|[\s\"'(=])/usr/"), "hard-coded /usr/"),
    (re.compile(r"\.so\b(?!\w)"), ".so name"),
    (re.compile(r"\bprctl\b"), "prctl"),
    (re.compile(r"PR_SET_PDEATHSIG"), "PR_SET_PDEATHSIG"),
]


def _files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name not in ALLOWED)


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        where = f"{path.relative_to(SRC)}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if (node.value.id, node.attr) in FORBIDDEN_ATTRS:
                out.append(f"{where}: {node.value.id}.{node.attr}")
        elif isinstance(node, ast.Import):
            out += [f"{where}: import {a.name}" for a in node.names if a.name.split(".")[0] in FORBIDDEN_IMPORTS]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
            out.append(f"{where}: from {node.module} import ...")
        elif isinstance(node, ast.keyword) and node.arg in FORBIDDEN_KEYWORDS:
            out.append(f"{where}: {node.arg}=")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for rx, what in FORBIDDEN_STRINGS:
                if rx.search(node.value):
                    out.append(f"{where}: string with {what}: {node.value[:60]!r}")
    return out


def test_scanner_finds_violations(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import os, signal, fcntl\n"
        "os.killpg(1, signal.SIGKILL)\n"
        "subprocess.Popen(['x'], preexec_fn=None, start_new_session=True)\n"
        "P = '/usr/lib/x/libfoo.so'\n"
        "Q = f'/proc/{1}/stat'\n"
        "R = 'XDG_CONFIG_HOME'\n"
    )
    global SRC
    old, SRC = SRC, tmp_path
    try:
        v = _violations(bad)
    finally:
        SRC = old
    text = "\n".join(v)
    for needle in ("import signal", "import fcntl", "os.killpg", "signal.SIGKILL", "preexec_fn=", "start_new_session=",
                   "/usr/", ".so name", "/proc", "XDG_"):
        assert needle in text, needle


@pytest.mark.parametrize("path", _files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_posix_only_constructs_outside_osdep(path: Path) -> None:
    assert _violations(path) == []
