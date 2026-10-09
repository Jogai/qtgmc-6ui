"""Application icon from package data, robust against a missing Qt SVG plugin.

Arch's ``pyside6`` does not depend on ``qt6-svg``, so the SVG may not be loadable; the PNG
renditions next to it (16-256 px, rendered from the SVG) always load (PNG support is in qt6-base).
On Wayland the compositor takes the icon from ``qtgmc-6ui.desktop`` via the desktop file name;
``setWindowIcon`` covers X11 and window switchers that honour it.
"""

from __future__ import annotations

import logging
from importlib import resources

from PySide6.QtCore import QBuffer, QByteArray, QSize
from PySide6.QtGui import QIcon, QImageReader, QPixmap

from .. import paths

log = logging.getLogger(__name__)

ICON_NAME = paths.APP_ID
PNG_SIZES = (16, 22, 24, 32, 48, 64, 128, 256)


def _resource(name: str) -> bytes | None:
    try:
        return resources.files("qtgmc_6ui.gui").joinpath("resources", name).read_bytes()
    except (OSError, ModuleNotFoundError):
        return None


def svg_supported() -> bool:
    return b"svg" in [bytes(f) for f in QImageReader.supportedImageFormats()]


def bundled_icon() -> QIcon:
    icon = QIcon()
    for size in PNG_SIZES:
        data = _resource(f"{ICON_NAME}-{size}.png")
        pm = QPixmap()
        if data and pm.loadFromData(QByteArray(data), "PNG"):
            icon.addPixmap(pm)
    svg = _resource(f"{ICON_NAME}.svg")
    if svg and svg_supported():
        for size in (512, 1024):  # crisp on HiDPI beyond the largest PNG
            buf = QBuffer()
            buf.setData(QByteArray(svg))
            reader = QImageReader(buf, QByteArray(b"svg"))
            reader.setScaledSize(QSize(size, size))
            img = reader.read()
            if not img.isNull():
                icon.addPixmap(QPixmap.fromImage(img))
    return icon


def has_opaque_pixels(icon: QIcon, size: int = 32) -> bool:
    img = icon.pixmap(size, size).toImage()
    if img.isNull():
        return False
    step = max(1, img.width() // 8)
    return any(img.pixelColor(x, y).alpha() > 0 for x in range(0, img.width(), step) for y in range(0, img.height(), step))


def app_icon() -> QIcon:
    """The installed theme icon when it renders, else the bundled one; a warning if neither does."""
    bundled = bundled_icon()
    themed = QIcon.fromTheme(ICON_NAME)
    if not themed.isNull() and has_opaque_pixels(themed):
        for size in PNG_SIZES:  # keep bundled sizes as fallbacks inside the themed icon
            if themed.pixmap(size, size).isNull():
                themed.addPixmap(bundled.pixmap(size, size))
        return themed
    if bundled.isNull() or not has_opaque_pixels(bundled):
        log.warning("qtgmc-6UI: the application icon could not be loaded (package resources missing?)")
    return bundled
