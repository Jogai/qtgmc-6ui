"""Error codes, exit codes and the exception hierarchy shared by CLI, core and GUI.

Every error that reaches the user carries a stable ``code`` (for the GUI and
for bug reports), a one-line ``message`` and an actionable ``hint``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1  # unexpected / generic failure
    USAGE = 2  # bad arguments (argparse)
    BLOCKED = 3  # analysis blocked the job (telecined/mixed/progressive) and --force not given
    COMPONENT = 4  # environment problem: ABI mismatch, plugin failed to load, tool missing
    INPUT = 5  # input unreadable or unsupported format
    ENCODE = 6  # ffmpeg / external encoder failed
    OUTPUT = 7  # output exists, not writable, disk full
    CANCELLED = 130  # SIGINT/SIGTERM/stdin "cancel": partial outputs removed


class ErrorCode(StrEnum):
    # generic
    INTERNAL = "internal"
    USAGE = "usage"
    # input / analysis
    INPUT_NOT_FOUND = "input_not_found"
    INPUT_UNREADABLE = "input_unreadable"
    INPUT_NO_VIDEO = "input_no_video"
    INPUT_FORMAT_UNSUPPORTED = "input_format_unsupported"  # RGB/float/alpha/410/440: no y4m path
    ANALYSIS_FAILED = "analysis_failed"
    # blocking decisions
    BLOCKED_TELECINED = "blocked_telecined"
    BLOCKED_MIXED = "blocked_mixed"
    BLOCKED_PROGRESSIVE = "blocked_progressive"
    BLOCKED_UNDETERMINED = "blocked_undetermined"  # field order unknown and none given
    # environment / components
    VS_IMPORT_FAILED = "vs_import_failed"
    VS_VERSION_MISMATCH = "vs_version_mismatch"  # build-info vs running core/api
    PYTHON_VERSION_MISMATCH = "python_version_mismatch"
    BUILD_INFO_MISSING = "build_info_missing"
    PLUGIN_DIR_MISSING = "plugin_dir_missing"
    PLUGIN_LOAD_FAILED = "plugin_load_failed"
    PLUGIN_CONFLICT = "plugin_conflict"  # same identifier already autoloaded from elsewhere
    PYPKG_MISSING = "pypkg_missing"  # vsjetpack / jetpytools not importable
    PYPKG_VERSION_MISMATCH = "pypkg_version_mismatch"
    TOOL_MISSING = "tool_missing"  # ffmpeg/ffprobe (fatal) or x264/x265/kvazaar/sox (per profile)
    DENOISE_UNAVAILABLE = "denoise_unavailable"  # dfttest2 plugin not bundled/loaded
    # processing
    ENCODE_FAILED = "encode_failed"
    PIPE_BROKEN = "pipe_broken"
    PROFILE_INVALID = "profile_invalid"
    PROFILE_NOT_FOUND = "profile_not_found"
    PROFILE_COMBINATION_INVALID = "profile_combination_invalid"  # muxer rejects codec
    OUTPUT_EXISTS = "output_exists"
    OUTPUT_NOT_WRITABLE = "output_not_writable"
    CANCELLED = "cancelled"


_EXIT_FOR_CODE: dict[ErrorCode, ExitCode] = {
    ErrorCode.USAGE: ExitCode.USAGE,
    ErrorCode.INPUT_NOT_FOUND: ExitCode.INPUT,
    ErrorCode.INPUT_UNREADABLE: ExitCode.INPUT,
    ErrorCode.INPUT_NO_VIDEO: ExitCode.INPUT,
    ErrorCode.INPUT_FORMAT_UNSUPPORTED: ExitCode.INPUT,
    ErrorCode.ANALYSIS_FAILED: ExitCode.INPUT,
    ErrorCode.BLOCKED_TELECINED: ExitCode.BLOCKED,
    ErrorCode.BLOCKED_MIXED: ExitCode.BLOCKED,
    ErrorCode.BLOCKED_PROGRESSIVE: ExitCode.BLOCKED,
    ErrorCode.BLOCKED_UNDETERMINED: ExitCode.BLOCKED,
    ErrorCode.VS_IMPORT_FAILED: ExitCode.COMPONENT,
    ErrorCode.VS_VERSION_MISMATCH: ExitCode.COMPONENT,
    ErrorCode.PYTHON_VERSION_MISMATCH: ExitCode.COMPONENT,
    ErrorCode.BUILD_INFO_MISSING: ExitCode.COMPONENT,
    ErrorCode.PLUGIN_DIR_MISSING: ExitCode.COMPONENT,
    ErrorCode.PLUGIN_LOAD_FAILED: ExitCode.COMPONENT,
    ErrorCode.PLUGIN_CONFLICT: ExitCode.COMPONENT,
    ErrorCode.PYPKG_MISSING: ExitCode.COMPONENT,
    ErrorCode.PYPKG_VERSION_MISMATCH: ExitCode.COMPONENT,
    ErrorCode.TOOL_MISSING: ExitCode.COMPONENT,
    ErrorCode.DENOISE_UNAVAILABLE: ExitCode.COMPONENT,
    ErrorCode.ENCODE_FAILED: ExitCode.ENCODE,
    ErrorCode.PIPE_BROKEN: ExitCode.ENCODE,
    ErrorCode.PROFILE_INVALID: ExitCode.USAGE,
    ErrorCode.PROFILE_NOT_FOUND: ExitCode.USAGE,
    ErrorCode.PROFILE_COMBINATION_INVALID: ExitCode.USAGE,
    ErrorCode.OUTPUT_EXISTS: ExitCode.OUTPUT,
    ErrorCode.OUTPUT_NOT_WRITABLE: ExitCode.OUTPUT,
    ErrorCode.CANCELLED: ExitCode.CANCELLED,
}


@dataclass
class QtgmcError(Exception):
    """Base error. ``details`` is free-form JSON-able context (paths, stderr tail, ...)."""

    code: ErrorCode
    message: str
    hint: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}" + (f"\n  hint: {self.hint}" if self.hint else "")

    @property
    def exit_code(self) -> ExitCode:
        return _EXIT_FOR_CODE.get(self.code, ExitCode.ERROR)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "message": self.message,
            "hint": self.hint,
            "details": dict(self.details),
            "exit_code": int(self.exit_code),
        }


class UsageError(QtgmcError):
    def __init__(self, message: str, hint: str = "", **details: Any) -> None:
        super().__init__(ErrorCode.USAGE, message, hint, details)


class InputError(QtgmcError):
    pass


class BlockedError(QtgmcError):
    """Analysis decided not to process; ``--force`` overrides (see Recommendation)."""


class ComponentError(QtgmcError):
    """Environment problem that ``qtgmc-6ui doctor`` would also report."""


class EncodeError(QtgmcError):
    pass


class OutputError(QtgmcError):
    pass


class CancelledError(QtgmcError):
    def __init__(self, removed: list[str] | None = None, kept: list[str] | None = None) -> None:
        super().__init__(
            ErrorCode.CANCELLED,
            "Cancelled",
            "",
            {"removed": removed or [], "kept": kept or []},
        )
