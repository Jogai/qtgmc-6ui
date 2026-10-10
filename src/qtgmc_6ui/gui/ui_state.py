"""UI state only (window geometry, last folders, panel values) in paths.config_dir()/gui.ini."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSettings

from .. import paths


def settings() -> QSettings:
    path = paths.config_dir() / "gui.ini"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return QSettings(str(path), QSettings.Format.IniFormat)


def get_str(key: str, default: str = "") -> str:
    v = settings().value(key, default)
    return v if isinstance(v, str) else default


def get_bool(key: str, default: bool = False) -> bool:
    v = settings().value(key, default)
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes")
    return bool(v)


def get_list(key: str) -> list[str]:
    v = settings().value(key, [])
    if isinstance(v, str):
        return [v] if v else []
    return [str(x) for x in v or []]


def put(key: str, value: Any) -> None:
    s = settings()
    s.setValue(key, value)
    s.sync()
