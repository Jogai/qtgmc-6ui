"""Viewing-copy profiles (WP-C): TOML files in paths.profiles_dir(), built-ins in data/profiles/.

TOML layout == models.Profile (nested tables [video], [video.scale], [audio], [audio.sox]).
``builtin`` and ``path`` are never written.  A user file with the same id shadows a built-in.
Reading uses tomllib; writing uses the small emitter here (no third-party dependency).
Built-ins (rely only on Arch's ffmpeg):
  mp4-h264-aac   "MP4 – H.264 + AAC (compatible)"       libx264 crf 18 slow, aac 192k, scale square
  mkv-hevc10-opus "MKV – HEVC 10-bit + Opus (efficient)" libx265 crf 20 slow yuv420p10le, libopus 128k
  mkv-h264-flac  "MKV – H.264 + FLAC (high quality)"     libx264 crf 16 slower, flac, keep SAR
"""

from __future__ import annotations

from pathlib import Path

from .models import Profile


def builtin_profiles() -> list[Profile]:
    raise NotImplementedError


def user_profiles() -> list[Profile]:
    raise NotImplementedError


def list_profiles() -> list[Profile]:
    """Built-ins + user profiles, user shadows built-in by id, sorted by name."""
    raise NotImplementedError


def load(id_or_path: str) -> Profile:
    """Resolve by id (user, then built-in) or by .toml path; raise PROFILE_NOT_FOUND."""
    raise NotImplementedError


def validate(profile: Profile, *, check_tools: bool = True) -> list[str]:
    """Return problem lines (empty = valid): schema, id pattern, container/encoder exist,
    mux verdict not FAIL, backend tool available, pix_fmt supported by encoder."""
    raise NotImplementedError


def save(profile: Profile, path: Path | None = None) -> Path:
    raise NotImplementedError


def delete(id: str) -> None:
    """User profiles only; built-ins raise PROFILE_INVALID."""
    raise NotImplementedError


def to_toml(profile: Profile) -> str:
    raise NotImplementedError


def from_toml(text: str, *, path: str | None = None, builtin: bool = False) -> Profile:
    raise NotImplementedError
