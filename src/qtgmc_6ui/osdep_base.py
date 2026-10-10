"""Dependency-free definitions shared by osdep and osdep_windows (no import cycles)."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

APP_ID = "qtgmc-6ui"


class StopLevel(Enum):
    GRACEFUL = "graceful"  # ask the child (tree) to stop: SIGTERM (POSIX); see osdep_windows
    FORCE = "force"  # no negotiation: SIGKILL / TerminateJobObject


# "Termination" callback: (signal name for messages/reasons, interactive) where interactive is
# True for the terminal interrupt (Ctrl-C), whose repetition means "exit now".
TerminationCallback = Callable[[str, bool], None]


def is_utf8_locale(value: str) -> bool:
    v = value.lower().replace("-", "")
    return "utf8" in v
