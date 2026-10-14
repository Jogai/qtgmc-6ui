"""Viewing-copy profiles (WP-C): TOML files in paths.profiles_dir(), built-ins in data/profiles/.

TOML layout == models.Profile (nested tables [video], [video.scale], [audio], [audio.sox]).
``builtin`` and ``path`` are never written.  A user file with the same id shadows a built-in.
Reading uses tomllib; writing uses the small emitter here (no third-party dependency).
Built-ins (rely only on Arch's ffmpeg):
  mp4-h264-aac   "MP4 – H.264 + AAC (compatible)"       libx264 crf 18 slow, aac 192k, scale square
  mkv-hevc10-opus "MKV – HEVC 10-bit + Opus (efficient)" libx265 crf 20 slow yuv420p10le, libopus 128k
  mkv-h264-flac  "MKV – H.264 + FLAC (high quality)"     libx264 crf 16 slower, flac, keep SAR
Built-ins for the optional tools (x264/x265/kvazaar CLI, sox) ship too but are hidden by
``list_profiles()`` while the tool is absent (``required_tools()`` / ``is_available()``).
"""

from __future__ import annotations

import os
import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import encoders, muxmatrix, osdep, paths
from .errors import ErrorCode, QtgmcError
from .models import (
    PROFILE_SCHEMA,
    MuxVerdict,
    Profile,
    RateControl,
    ScaleMode,
    VideoBackend,
    to_dict,
)

BUILTIN_DIR = Path(__file__).parent / "data" / "profiles"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# ffmpeg encoder whose mux verdict stands in for a CLI backend's elementary stream
_CLI_FFMPEG_EQUIV = {VideoBackend.X264_CLI: "libx264", VideoBackend.X265_CLI: "libx265", VideoBackend.KVAZAAR_CLI: "libx265"}
_CLI_TOOL = {VideoBackend.X264_CLI: "x264", VideoBackend.X265_CLI: "x265", VideoBackend.KVAZAAR_CLI: "kvazaar"}

# encoders whose constant-quality mode is "cq" (crf with -b:v 0) and the lossless switch
CQ_ENCODERS = {"libvpx", "libvpx-vp9", "libaom-av1"}
# libswscale flag names (ffmpeg -h filter=scale / sws_flags); anything else could inject filters
SCALE_ALGORITHMS = ("fast_bilinear", "bilinear", "bicubic", "experimental", "neighbor", "area", "bicublin", "gauss", "sinc", "lanczos", "spline")
SCALE_MODIFIERS = ("accurate_rnd", "full_chroma_int", "full_chroma_inp", "bitexact", "print_info")
# tokens in extra_args / sox effects that would write or read other files or change the output
_DANGEROUS_TOKENS = {"-i", "-y", "-n", "-f", "-o", "--output", "-passlogfile", "--dump-yuv", "--stats", "--analyse-stats", "-report", "noiseprof", "--recon", "--recon-y4m-exec", "--debug", "--stats-file-prefix", "--roi", "--cqmfile"}
_MAX_DIMENSION = 16384
_X265_PROFILE_DEPTH = {"main": 8, "main444-8": 8, "main10": 10, "main422-10": 10, "main444-10": 10, "main12": 12, "main422-12": 12, "main444-12": 12}
LOSSLESS_ARGS: dict[str, list[str]] = {
    "libx264": ["-qp", "0"],
    "libx264rgb": ["-qp", "0"],
    "libx265": ["-x265-params", "lossless=1"],
    "libvpx-vp9": ["-lossless", "1"],
    "libaom-av1": ["-aom-params", "lossless=1"],
    "ffv1": [],
    "utvideo": [],
    "huffyuv": [],
    "ffvhuff": [],
}


# --------------------------------------------------------------------------- TOML
def _without_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: _without_none(v) if isinstance(v, dict) else v for k, v in d.items() if v is not None}


def to_toml(profile: Profile) -> str:
    """TOML text of a profile (tomli_w; ``builtin``/``path`` and unset optional values omitted)."""
    import tomli_w

    d = to_dict(profile)
    d.pop("builtin", None)
    d.pop("path", None)
    return tomli_w.dumps(_without_none(d))


class _WrongType(TypeError):
    """A value of the wrong TOML type; args[0] is the problem text after the key."""


_strict: Any = None


def _strict_converter() -> Any:
    """cattrs converter for hand-edited/hostile TOML: unknown keys and wrong types are errors,
    no coercion (bool is not an int, a string is not a list), enums must be valid values."""
    global _strict
    if _strict is None:
        import cattrs

        c = cattrs.Converter(forbid_extra_keys=True)
        allowed = {str: (str,), int: (int,), float: (int, float), bool: (bool,)}

        def scalar(base: type) -> Any:
            def hook(v: Any, tp: Any) -> Any:
                if isinstance(v, dict):
                    raise _WrongType("must not be a table")
                if not isinstance(v, allowed[base]) or (base is not bool and isinstance(v, bool)):
                    raise _WrongType(f"has the wrong type ({type(v).__name__})")
                return tp(v) if tp is not base else v  # StrEnum: ValueError for an unknown value

            return hook

        def str_list(v: Any, tp: Any) -> list[str]:
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise _WrongType(f"has the wrong type ({type(v).__name__})")
            return v

        for base in allowed:
            c.register_structure_hook(base, scalar(base))
        c.register_structure_hook(StrEnum, scalar(str))
        c.register_structure_hook_func(lambda t: t == list[str], str_list)
        _strict = c
    return _strict


def _strict_problems(d: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(key/type problems, invalid enum values) of a profile document, path-qualified."""
    import cattrs

    try:
        _strict_converter().structure(d, Profile)
        return [], []
    except cattrs.BaseValidationError as e:
        group = e
    problems: list[str] = []
    values: list[str] = []

    def walk(exc: BaseException, path: str) -> None:
        if isinstance(exc, cattrs.ForbiddenExtraKeysError):
            problems.extend(f"unknown key {path}{k}" for k in sorted(exc.extra_fields))
        elif isinstance(exc, BaseExceptionGroup):
            for sub in exc.exceptions:
                note = next((n for n in getattr(sub, "__notes__", []) if hasattr(n, "name")), None)
                walk(sub, f"{path}{note.name}." if note is not None else path)
        elif isinstance(exc, _WrongType):
            problems.append(f"{path[:-1]} {exc}")
        else:
            values.append(str(exc))

    walk(group, "")
    return problems, values


def from_toml(text: str, *, path: str | None = None, builtin: bool = False) -> Profile:
    try:
        d = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise QtgmcError(ErrorCode.PROFILE_INVALID, f"profile is not valid TOML: {e}", "", {"path": path or ""}) from e
    problems, values = _strict_problems(d)
    if problems:
        raise QtgmcError(ErrorCode.PROFILE_INVALID, "profile has invalid keys: " + ", ".join(problems), "", {"path": path or "", "problems": problems})
    if values:
        raise QtgmcError(ErrorCode.PROFILE_INVALID, f"profile has an invalid value: {values[0]}", "", {"path": path or ""})
    p = Profile.from_dict(d)
    p.builtin = builtin
    p.path = path
    if not p.id and path:
        p.id = Path(path).stem
    return p


# --------------------------------------------------------------------------- tool requirements
def required_tools(profile: Profile) -> list[str]:
    tools: list[str] = []
    if profile.video.backend in _CLI_TOOL:
        tools.append(_CLI_TOOL[profile.video.backend])
    if profile.audio.sox.enabled:
        tools.append("sox")
    return tools


def missing_tools(profile: Profile) -> list[str]:
    return [t for t in required_tools(profile) if not encoders.tool_available(t)]


def is_available(profile: Profile) -> bool:
    return not missing_tools(profile)


# --------------------------------------------------------------------------- loading
def _load_dir(d: Path, builtin: bool) -> list[Profile]:
    out: list[Profile] = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.toml")):
        try:
            out.append(from_toml(f.read_text("utf-8"), path=str(f), builtin=builtin))
        except (OSError, QtgmcError):
            if builtin:
                raise
            # a broken user file must not hide the others; `profiles validate PATH` reports it
            continue
    return out


def builtin_profiles(*, include_unavailable: bool = True) -> list[Profile]:
    ps = _load_dir(BUILTIN_DIR, True)
    return ps if include_unavailable else [p for p in ps if is_available(p)]


def user_profiles() -> list[Profile]:
    return _load_dir(paths.profiles_dir(), False)


def _user_index() -> dict[str, Profile]:
    """id -> profile for the user dir; the file name does not have to match the id (a copied
    or shared file keeps working).  With duplicate ids the first file (sorted) wins."""
    out: dict[str, Profile] = {}
    for p in user_profiles():
        out.setdefault(p.id, p)
    return out


def _check_id(id: str) -> None:
    if not ID_RE.match(id or ""):
        raise QtgmcError(ErrorCode.PROFILE_NOT_FOUND, f"invalid profile id '{id}'", "ids are lowercase letters, digits and hyphens", {"profile": id})


def _inside_profiles_dir(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(paths.profiles_dir().resolve())
    except OSError:
        return False


def list_profiles(*, include_unavailable: bool = False) -> list[Profile]:
    """Built-ins + user profiles, user shadows built-in by id, sorted by name.
    Profiles needing an absent optional tool are hidden unless ``include_unavailable``."""
    by_id: dict[str, Profile] = {p.id: p for p in builtin_profiles()}
    for p in user_profiles():
        by_id[p.id] = p
    ps = sorted(by_id.values(), key=lambda p: (p.name.lower(), p.id))
    if include_unavailable:
        return ps
    return [p for p in ps if is_available(p)]


def load(id_or_path: str) -> Profile:
    """Resolve by id (user, then built-in) or by .toml path; raise PROFILE_NOT_FOUND."""
    cand = Path(id_or_path)
    if id_or_path.endswith(".toml") or os.sep in id_or_path or cand.is_file():
        if not cand.is_file():
            raise QtgmcError(ErrorCode.PROFILE_NOT_FOUND, f"profile file not found: {id_or_path}", "", {"profile": id_or_path})
        return from_toml(cand.read_text("utf-8"), path=str(cand), builtin=False)
    _check_id(id_or_path)
    user = _user_index().get(id_or_path)
    if user is not None:
        return user
    for p in builtin_profiles():
        if p.id == id_or_path:
            return p
    raise QtgmcError(
        ErrorCode.PROFILE_NOT_FOUND,
        f"no profile with id '{id_or_path}'",
        "run `qtgmc-6ui profiles list` to see the available profiles",
        {"profile": id_or_path},
    )


# --------------------------------------------------------------------------- validation
def _bit_depth(pix_fmt: str) -> int:
    m = re.search(r"(\d+)(?:le|be)$", pix_fmt)
    return int(m.group(1)) if m else 8


def validate(profile: Profile, *, check_tools: bool = True, check_ffmpeg: bool = True) -> list[str]:
    """Return problem lines (empty = valid): schema, id pattern, container/encoder exist,
    mux verdict not FAIL, backend tool available, pix_fmt supported by encoder.
    ``check_ffmpeg=False`` skips every check that needs to run ffmpeg (structure only)."""
    problems: list[str] = []
    v, a = profile.video, profile.audio
    if profile.schema != PROFILE_SCHEMA:
        problems.append(f"schema {profile.schema} is not supported (expected {PROFILE_SCHEMA})")
    if not ID_RE.match(profile.id or ""):
        problems.append(f"id '{profile.id}' must match [a-z0-9-]+ (lowercase, digits, hyphens)")
    if not profile.name.strip():
        problems.append("name must not be empty")
    if not profile.container:
        problems.append("container must not be empty")
    if not profile.extension or "/" in profile.extension or profile.extension.startswith("."):
        problems.append(f"extension '{profile.extension}' must be a bare suffix like 'mp4'")
    if not 0 <= v.quality <= 100:
        problems.append("video.quality must be between 0 and 100")
    if not 0 <= v.bitrate_kbps <= 500_000:
        problems.append("video.bitrate_kbps must be between 0 and 500000")
    for part in (v.scale.algorithm or "lanczos").split("+"):
        if part not in SCALE_ALGORITHMS and part not in SCALE_MODIFIERS:
            problems.append(f"video.scale.algorithm '{v.scale.algorithm}' is not a scale flag ({' '.join(SCALE_ALGORITHMS)})")
            break
    if v.scale.width > _MAX_DIMENSION or v.scale.height > _MAX_DIMENSION:
        problems.append(f"video.scale width/height must be <= {_MAX_DIMENSION}")
    if a.bitrate_kbps > 10_000 or a.sample_rate > 384_000 or a.channels > 64:
        problems.append("audio bitrate_kbps/sample_rate/channels out of range")
    if a.quality is not None and not 0 <= a.quality <= 100:
        problems.append("audio.quality must be between 0 and 100")
    for label, toks in (("video.extra_args", v.extra_args), ("audio.extra_args", a.extra_args), ("audio.sox.effects", a.sox.effects)):
        if isinstance(toks, list):
            bad = [t for t in toks if isinstance(t, str) and (t in _DANGEROUS_TOKENS or t.startswith("--dump"))]
            if bad:
                problems.append(f"{label} contains tokens that are not allowed in a profile: {' '.join(bad)}")
    if v.rate_control == RateControl.BITRATE and v.bitrate_kbps <= 0:
        problems.append("video.bitrate_kbps must be > 0 for rate_control = bitrate")
    if v.scale.mode == ScaleMode.CUSTOM and v.scale.width <= 0 and v.scale.height <= 0:
        problems.append("video.scale width or height must be > 0 for mode = custom")
    if any(x < 0 for x in (v.scale.width, v.scale.height)):
        problems.append("video.scale width/height must not be negative")
    if a.bitrate_kbps < 0 or a.sample_rate < 0 or a.channels < 0:
        problems.append("audio bitrate_kbps/sample_rate/channels must not be negative")
    if a.sox.enabled and not a.sox.effects:
        problems.append("audio.sox.effects must not be empty when sox is enabled")
    if a.sox.enabled and a.encoder == "copy":
        problems.append("audio.sox cannot be combined with audio.encoder = copy (sox output must be re-encoded)")
    if not isinstance(v.extra_args, list) or not all(isinstance(x, str) for x in v.extra_args):
        problems.append("video.extra_args must be a list of strings")
    if not isinstance(a.extra_args, list) or not all(isinstance(x, str) for x in a.extra_args):
        problems.append("audio.extra_args must be a list of strings")
    if not isinstance(a.sox.effects, list) or not all(isinstance(x, str) for x in a.sox.effects):
        problems.append("audio.sox.effects must be a list of strings")

    # backend-specific syntax
    if v.backend == VideoBackend.X264_CLI or (v.backend == VideoBackend.FFMPEG and v.encoder in ("libx264", "libx264rgb")):
        if v.profile in ("baseline", "main", "high") and _bit_depth(v.pix_fmt) > 8:
            problems.append(f"x264 profile '{v.profile}' is 8-bit only (use high10/high422/high444 for {v.pix_fmt})")
    if v.backend == VideoBackend.X264_CLI:
        if v.preset and v.preset not in encoders.X264_PRESETS:
            problems.append(f"x264 preset '{v.preset}' unknown")
        if v.tune and v.tune not in encoders.X264_TUNES:
            problems.append(f"x264 tune '{v.tune}' unknown")
        if v.rate_control == RateControl.CQ:
            problems.append("x264 has no cq mode (use crf)")
        if v.pix_fmt and _bit_depth(v.pix_fmt) not in (8, 10):
            problems.append("x264 supports 8 or 10 bit output only")
    elif v.backend == VideoBackend.X265_CLI:
        if v.preset and v.preset not in encoders.X265_PRESETS:
            problems.append(f"x265 preset '{v.preset}' unknown")
        if v.tune and v.tune not in encoders.X265_TUNES:
            problems.append(f"x265 tune '{v.tune}' unknown")
        if v.rate_control == RateControl.CQ:
            problems.append("x265 has no cq mode (use crf)")
        if v.pix_fmt and _bit_depth(v.pix_fmt) not in (8, 10, 12):
            problems.append("x265 supports 8, 10 or 12 bit output only")
        if v.profile and _X265_PROFILE_DEPTH.get(v.profile, 12) < _bit_depth(v.pix_fmt):
            problems.append(f"x265 profile '{v.profile}' does not allow {_bit_depth(v.pix_fmt)}-bit output")
    elif v.backend == VideoBackend.KVAZAAR_CLI:
        if v.preset and v.preset not in encoders.KVAZAAR_PRESETS:
            problems.append(f"kvazaar preset '{v.preset}' unknown")
        if v.rate_control in (RateControl.CRF, RateControl.CQ):
            problems.append("kvazaar supports qp, bitrate or lossless rate control only")
        for fld in ("tune", "profile"):
            if getattr(v, fld):
                problems.append(f"kvazaar has no {fld} option")
        if v.pix_fmt and v.pix_fmt not in ("yuv420p", "gray"):
            problems.append("kvazaar accepts yuv420p (8-bit) input only")
    elif v.backend == VideoBackend.FFMPEG:
        if v.encoder in ("libx264", "libx264rgb"):
            if v.preset and v.preset not in encoders.X264_PRESETS:
                problems.append(f"libx264 preset '{v.preset}' unknown")
            if v.tune and v.tune not in encoders.X264_TUNES:
                problems.append(f"libx264 tune '{v.tune}' unknown")
        if v.encoder == "libx265":
            if v.preset and v.preset not in encoders.X265_PRESETS:
                problems.append(f"libx265 preset '{v.preset}' unknown")
            if v.tune and v.tune not in encoders.X265_TUNES:
                problems.append(f"libx265 tune '{v.tune}' unknown")
        if v.rate_control == RateControl.LOSSLESS and v.encoder not in LOSSLESS_ARGS:
            problems.append(f"no known lossless switch for encoder {v.encoder}")

    if check_tools:
        for t in missing_tools(profile):
            problems.append(f"optional tool '{t}' is not installed ({osdep.install_hint(t)})")

    if not check_ffmpeg:
        return problems
    try:
        muxers = encoders.muxer_names()
        vencs = encoders.encoder_names("video")
        aencs = encoders.encoder_names("audio")
    except QtgmcError as e:
        problems.append(f"ffmpeg not usable: {e.message}")
        return problems
    if profile.container and profile.container not in muxers:
        problems.append(f"container (muxer) '{profile.container}' is not provided by this ffmpeg")
        return problems
    # video: encoder existence, options, pix_fmt, mux verdict
    if v.backend == VideoBackend.FFMPEG:
        if v.encoder not in vencs:
            problems.append(f"video encoder '{v.encoder}' is not provided by this ffmpeg")
        else:
            info = encoders.encoder_details(v.encoder)
            names = {o.name for o in info.options}
            if v.pix_fmt and info.pix_fmts and v.pix_fmt not in info.pix_fmts:
                problems.append(f"pix_fmt '{v.pix_fmt}' is not supported by {v.encoder} (supported: {' '.join(info.pix_fmts)})")
            if v.rate_control == RateControl.CRF and "crf" not in names:
                problems.append(f"{v.encoder} has no crf option")
            if v.rate_control == RateControl.QP and not ({"qp", "q", "cqp"} & names) and v.encoder not in ("mpeg4", "mjpeg"):
                problems.append(f"{v.encoder} has no qp option")
            if v.rate_control == RateControl.CQ and v.encoder not in CQ_ENCODERS and "cq" not in names:
                problems.append(f"{v.encoder} has no cq mode")
            if v.preset and "preset" not in names:
                problems.append(f"{v.encoder} has no preset option")
            if v.tune and "tune" not in names:
                problems.append(f"{v.encoder} has no tune option")
            if v.profile and "profile" not in names and v.encoder not in ("aac",):
                problems.append(f"{v.encoder} has no profile option")
            mp = muxmatrix.probe(profile.container, v.encoder, kind="video")
            if mp.verdict == MuxVerdict.FAIL:
                problems.append(muxmatrix.reason(profile.container, v.encoder, kind="video"))
    else:
        equiv = _CLI_FFMPEG_EQUIV[v.backend]
        if equiv in vencs:
            mp = muxmatrix.probe(profile.container, equiv, kind="video")
            if mp.verdict == MuxVerdict.FAIL:
                problems.append(muxmatrix.reason(profile.container, equiv, kind="video").replace(equiv, v.backend.value))
    # audio
    if a.encoder == "copy":
        mp = muxmatrix.probe(profile.container, "flac", kind="audio")
        if mp.verdict == MuxVerdict.FAIL:
            problems.append("audio copy keeps the master's FLAC: " + muxmatrix.reason(profile.container, "flac", kind="audio"))
    elif a.encoder not in aencs:
        problems.append(f"audio encoder '{a.encoder}' is not provided by this ffmpeg")
    else:
        info = encoders.encoder_details(a.encoder)
        if a.sample_rate and info.sample_rates and a.sample_rate not in info.sample_rates:
            problems.append(f"sample_rate {a.sample_rate} is not supported by {a.encoder} (supported: {' '.join(map(str, info.sample_rates))})")
        mp = muxmatrix.probe(profile.container, a.encoder, kind="audio")
        if mp.verdict == MuxVerdict.FAIL:
            problems.append(muxmatrix.reason(profile.container, a.encoder, kind="audio"))
    return problems


# --------------------------------------------------------------------------- save / delete
def save(profile: Profile, path: Path | None = None) -> Path:
    """Write the profile as TOML (default: paths.profiles_dir()/<id>.toml).  Structural
    problems (schema, id, name, ...) are rejected; run ``validate()`` for the full check."""
    problems = validate(profile, check_tools=False, check_ffmpeg=False)
    if problems:
        raise QtgmcError(ErrorCode.PROFILE_INVALID, "profile is invalid: " + "; ".join(problems), "", {"problems": problems})
    target = Path(path) if path else paths.profiles_dir() / f"{profile.id}.toml"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        osdep.atomic_write_text(target, to_toml(profile))
    except OSError as e:
        raise QtgmcError(ErrorCode.OUTPUT_NOT_WRITABLE, f"cannot write profile {target}: {e}", "", {"path": str(target)}) from e
    profile.path = str(target)
    profile.builtin = False
    return target


def delete(id: str) -> None:
    """User profiles only; built-ins raise PROFILE_INVALID."""
    _check_id(id)
    user = _user_index().get(id)
    if user is not None and user.path:
        target = Path(user.path)
        if not _inside_profiles_dir(target):
            raise QtgmcError(ErrorCode.PROFILE_INVALID, f"refusing to delete {target}: outside the profiles directory", "", {"path": str(target)})
        try:
            target.unlink()
        except OSError as e:
            raise QtgmcError(ErrorCode.OUTPUT_NOT_WRITABLE, f"cannot delete {target}: {e}", "", {"path": str(target)}) from e
        return
    if any(p.id == id for p in builtin_profiles()):
        raise QtgmcError(ErrorCode.PROFILE_INVALID, f"'{id}' is a built-in profile and cannot be deleted", "copy it under a new id instead", {"profile": id})
    raise QtgmcError(ErrorCode.PROFILE_NOT_FOUND, f"no user profile with id '{id}'", "", {"profile": id})
