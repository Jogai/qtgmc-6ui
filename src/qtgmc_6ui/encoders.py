"""FFmpeg encoder/muxer discovery and optional tool detection (WP-C).

All lists come from the installed ffmpeg at runtime (``-muxers``, ``-encoders``,
``-h encoder=NAME``, ``-h muxer=NAME``); results are cached per ffmpeg version
string in paths.mux_matrix_cache_dir()/encoders-<version>.json.
Parsing notes: research/arch-facts.md §6 (legend block before data, 6 flag chars,
``preset``/``tune``/``profile`` of libx264/libx265 are plain <string> options, so
their choices are hard-coded in X264_PRESETS/X264_TUNES/X265_PRESETS/X265_TUNES).
"""

from __future__ import annotations

from .models import EncoderInfo, MuxerInfo, ToolInfo

X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo")
X264_TUNES = ("", "film", "animation", "grain", "stillimage", "fastdecode", "zerolatency")
X265_PRESETS = X264_PRESETS
X265_TUNES = ("", "psnr", "ssim", "grain", "fastdecode", "zerolatency", "animation")

OPTIONAL_TOOLS = ("x264", "x265", "kvazaar", "sox")


def list_muxers(ffmpeg: str = "ffmpeg") -> list[MuxerInfo]:
    raise NotImplementedError


def list_encoders(ffmpeg: str = "ffmpeg") -> list[EncoderInfo]:
    """Video + audio encoders with the E flag; hardware encoders are kept (availability is
    only known by probing)."""
    raise NotImplementedError


def encoder_details(name: str, ffmpeg: str = "ffmpeg") -> EncoderInfo:
    """``-h encoder=NAME``: pix_fmts, sample formats/rates and AVOptions with enum choices.
    Unknown encoder prints 'Codec ... is not recognized' with rc 0: detect by text."""
    raise NotImplementedError


def muxer_details(name: str, ffmpeg: str = "ffmpeg") -> MuxerInfo:
    raise NotImplementedError


def detect_tools() -> list[ToolInfo]:
    """ffmpeg/ffprobe (required) and OPTIONAL_TOOLS via shutil.which + ``--version`` parsing."""
    raise NotImplementedError


def invalidate_cache() -> None:
    raise NotImplementedError
