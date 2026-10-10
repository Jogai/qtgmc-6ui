"""Guard rail: OS-specific constructs live in osdep*.py only (the seam for a Windows port).

AST scan of src/qtgmc_6ui (docstrings and comments are NOT scanned, so prose may name
POSIX things freely).  Flags, outside osdep*.py:
  * forbidden modules/attributes, resolving ``import os as o`` and ``from os import killpg``,
    and ``getattr(os, "killpg")`` with a constant name;
  * Popen keywords for process groups / preexec, also inside ``**{...}`` literals;
  * ``sys.platform`` / ``os.name`` checks;
  * string literals hard-coding /proc, /dev/, /tmp/, /usr/, XDG_* or .so names.
Fix a failure by adding (or using) a function in qtgmc_6ui.osdep; only a reviewed exception
belongs in ALLOWLIST.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "qtgmc_6ui"
SEAM = {"osdep.py", "osdep_base.py", "osdep_windows.py"}

FORBIDDEN_MODULES = {"fcntl", "termios", "ctypes", "pwd", "grp", "resource", "signal", "winreg", "msvcrt", "_winapi"}
FORBIDDEN_ATTRS = {
    "os": {"killpg", "getpgid", "setpgid", "setsid", "fork", "getppid", "kill", "symlink", "readlink", "getuid",
           "geteuid", "sched_getaffinity", "X_OK", "name", "setpgrp"},
    "sys": {"platform"},
    "subprocess": {"CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW", "STARTUPINFO"},
}
FORBIDDEN_KEYWORDS = {"preexec_fn", "start_new_session", "creationflags", "process_group", "startupinfo"}
FORBIDDEN_STRINGS = [
    (re.compile(r"(^|[^\w.])/proc(/|\b)"), "/proc"),
    (re.compile(r"(^|[^\w.])/dev/"), "/dev/"),
    (re.compile(r"(^|[^\w.])/tmp/"), "/tmp/"),
    (re.compile(r"(^|[^\w.])/usr/"), "/usr/"),
    (re.compile(r"\bXDG_[A-Z_]+"), "XDG_*"),
    (re.compile(r"\w\.so(\.\d+)*$|\w\.so\b(?![\w-])"), ".so name"),
]

# (file relative to src/qtgmc_6ui, violation text substring): reason.  Keep this short.
ALLOWLIST: dict[tuple[str, str], str] = {}

MESSAGE = (
    "OS-specific code outside qtgmc_6ui/osdep*.py.  Use (or add) a function in qtgmc_6ui.osdep "
    "so the Windows port has one seam; see the module docstring of tests/test_osdep_boundary.py."
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _violations(path: Path, root: Path = SRC) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = str(path.relative_to(root))
    docs = _docstring_nodes(tree)
    aliases: dict[str, str] = {}  # local name -> module
    out: list[str] = []

    def add(node: ast.AST, what: str) -> None:
        out.append(f"{rel}:{getattr(node, 'lineno', '?')}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                aliases[a.asname or top] = top
                if top in FORBIDDEN_MODULES:
                    add(node, f"import {a.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            if top in FORBIDDEN_MODULES:
                add(node, f"from {node.module} import ...")
            for a in node.names:
                if a.name in FORBIDDEN_ATTRS.get(top, set()):
                    add(node, f"from {top} import {a.name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            mod = aliases.get(node.value.id)
            if mod and node.attr in FORBIDDEN_ATTRS.get(mod, set()):
                add(node, f"{mod}.{node.attr}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
            obj, name = node.args[0], node.args[1]
            if isinstance(obj, ast.Name) and isinstance(name, ast.Constant) and isinstance(name.value, str):
                mod = aliases.get(obj.id)
                if mod and (name.value in FORBIDDEN_ATTRS.get(mod, set()) or mod in FORBIDDEN_MODULES):
                    add(node, f"getattr({mod}, {name.value!r})")
        elif isinstance(node, ast.keyword):
            if node.arg in FORBIDDEN_KEYWORDS:
                add(node, f"{node.arg}=")
            elif node.arg is None and isinstance(node.value, ast.Dict):
                for k in node.value.keys:
                    if isinstance(k, ast.Constant) and k.value in FORBIDDEN_KEYWORDS:
                        add(node, f"**{{{k.value!r}: ...}}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            for rx, what in FORBIDDEN_STRINGS:
                if rx.search(node.value):
                    add(node, f"string with {what}: {node.value[:60]!r}")
    return [v for v in out if not any(f == rel and s in v for (f, s) in ALLOWLIST)]


def _files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name not in SEAM)


BAD = '''"""Docstrings may mention /usr/lib, XDG_CONFIG_HOME, libfoo.so and /proc freely."""
import os, signal, fcntl
import os as o
from os import killpg
import sys
os.killpg(1, signal.SIGKILL)
o.setsid()
getattr(os, "killpg")(1, 9)
subprocess.Popen(["x"], preexec_fn=None, start_new_session=True)
subprocess.Popen(["x"], **{"start_new_session": True})
os.symlink("a", "b")
if sys.platform == "win32" or os.name == "nt":
    pass
P = "/usr/lib/x/libfoo.so"
Q = f"/proc/{1}/stat"
R = "XDG_CONFIG_HOME"
S = open("/dev/null")
T = "/tmp/x"
def f():
    """Function docstrings too: /dev/null, /tmp, pacman."""
'''

GOOD = '''import os
import subprocess
x = os.path.join("a", "b")
y = os.devnull
z = "setsar=16/15,scale=768:576"
w = "ratio 4/3 and a/b paths, video.mp4, mkv-h264-aac.mkv, profile.so-called"
v = subprocess.DEVNULL
'''


def test_scanner_catches_the_known_tricks(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(BAD)
    text = "\n".join(_violations(bad, tmp_path))
    for needle in ("import signal", "import fcntl", "from os import killpg", "os.killpg", "os.setsid",
                   "getattr(os, 'killpg')", "preexec_fn=", "start_new_session=", "**{'start_new_session'",
                   "os.symlink", "sys.platform", "os.name", "string with /usr/", "string with .so name",
                   "string with /proc", "string with XDG_*", "string with /dev/", "string with /tmp/"):
        assert needle in text, needle
    assert ":1:" not in text and ":20:" not in text  # docstrings are not scanned


def test_scanner_ignores_ordinary_code(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(GOOD)
    assert _violations(good, tmp_path) == []


@pytest.mark.parametrize("path", _files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_os_specific_code_outside_osdep(path: Path) -> None:
    v = _violations(path)
    assert not v, MESSAGE + "\n" + "\n".join(v)
