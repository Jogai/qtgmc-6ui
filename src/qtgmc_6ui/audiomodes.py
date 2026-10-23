"""Audio of the archival master: the single source of truth for the selectable modes.

``MODES`` is the ordered catalogue the CLI (``--audio``, ``audio-modes``) and the GUI show;
``plan()`` decides per source track what ffmpeg does and validates the choice up front
(dry-run included) with coded errors.  The ffmpeg command itself is built by master.py
from ``AudioPlan.ffmpeg_args()``.

Semantics (every audio track of the original, which is always mapped with ``-map 1:a?``):
  copy  -c:a copy; a codec Matroska cannot carry falls back to FLAC (warning audio_copy_unsupported)
  pcm   pcm_* matching the source sample format (s16 -> pcm_s16le, 24-bit -> pcm_s24le, s32 ->
        pcm_s32le, float -> pcm_f32le, double -> pcm_f64le, u8 -> pcm_u8; lossy sources decode to
        float -> pcm_f32le); the bit depth is never narrowed
  flac  compression level 8; 32-bit integer keeps 32 bits (-strict experimental), float is stored
        as 24-bit integer with a warning (audio)
  alac  s16p for 16-bit, s32p for 24/32-bit integer; float sources -> error audio_mode_unsupported
  aac   128 kbps per channel (256 kbps stereo), capped; warning audio_lossy
  opus  libopus 96 kbps per channel (192 kbps stereo), 48 kHz; warning audio_lossy
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ErrorCode, QtgmcError
from .models import AudioMode, AudioProbe, AudioTrackPlan

CONTAINER = "matroska"

# label/tooltip wording rule: no "best", "worst" or "default" (tests enforce it)
@dataclass(frozen=True)
class AudioModeInfo:
    id: AudioMode
    label: str
    tooltip: str
    group: str  # lossless | lossy
    lossy: bool

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id.value, "label": self.label, "tooltip": self.tooltip, "group": self.group, "lossy": self.lossy}


MODES: tuple[AudioModeInfo, ...] = (
    AudioModeInfo(AudioMode.COPY, "Copy source audio",
                  "Keeps every audio track of the original unchanged (bit-exact, no re-encoding). "
                  "A codec that Matroska cannot carry is stored as FLAC instead, with a warning.",
                  "lossless", False),
    AudioModeInfo(AudioMode.PCM, "PCM (uncompressed)",
                  "Uncompressed PCM at the sample format and bit depth of the source (lossy sources are decoded to "
                  "32-bit float). Largest files, readable everywhere.",
                  "lossless", False),
    AudioModeInfo(AudioMode.FLAC, "FLAC (lossless)",
                  "FLAC, compression level 8: decodes bit-exactly to the source samples at roughly half the size of "
                  "PCM. Floating-point sources are stored as 24-bit integer.",
                  "lossless", False),
    AudioModeInfo(AudioMode.ALAC, "ALAC (lossless)",
                  "Apple Lossless (16- or 24/32-bit integer): decodes bit-exactly, plays in Apple software. "
                  "Not available for floating-point sources.",
                  "lossless", False),
    AudioModeInfo(AudioMode.AAC, "AAC 256 kbps (lossy)",
                  "AAC at 256 kbps for stereo (128 kbps per channel). The master's audio is no longer the original.",
                  "lossy", True),
    AudioModeInfo(AudioMode.OPUS, "Opus 192 kbps (lossy)",
                  "Opus at 192 kbps for stereo (96 kbps per channel), resampled to 48 kHz. The master's audio is no "
                  "longer the original.",
                  "lossy", True),
)
DEFAULT: AudioMode = AudioMode.COPY

AAC_KBPS_PER_CHANNEL, AAC_MAX_KBPS = 128, 640
OPUS_KBPS_PER_CHANNEL, OPUS_MAX_KBPS = 96, 512
OPUS_RATES = {48000, 24000, 16000, 12000, 8000}
AAC_RATES = {96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350}
ALAC_LAYOUTS = {"mono", "stereo", "3.0", "4.0", "5.0", "5.1", "6.1(back)", "7.1(wide)"}
ALAC_CHANNELS = {1: "mono", 2: "stereo", 3: "3.0", 4: "4.0", 5: "5.0", 6: "5.1"}
MAX_CHANNELS = 8

# source codec -> ffmpeg encoder whose test mux answers "can Matroska carry this codec"
_COPY_ENCODER = {"mp3": "libmp3lame", "opus": "libopus", "vorbis": "libvorbis", "dts": "dca"}
_LOSSY_CODECS = {"aac", "mp3", "mp2", "opus", "vorbis", "ac3", "eac3", "dts", "wmav1", "wmav2", "wmapro", "atrac3",
                 "cook", "sipr", "nellymoser", "adpcm_ima_wav", "adpcm_ms", "adpcm_swf", "g726", "gsm", "amr_nb", "amr_wb"}


def info(mode: AudioMode | str) -> AudioModeInfo:
    m = AudioMode(mode)
    return next(i for i in MODES if i.id == m)


def describe() -> list[dict[str, Any]]:
    """The ordered catalogue as plain dicts (``qtgmc-6ui audio-modes --json``)."""
    return [m.to_dict() for m in MODES]


@dataclass
class AudioPlan:
    mode: AudioMode
    tracks: list[AudioTrackPlan] = field(default_factory=list)
    warnings: list[tuple[str, str, str]] = field(default_factory=list)  # (code, message, hint)
    strict_experimental: bool = False  # 32-bit FLAC needs it

    @property
    def lossless(self) -> bool:
        return all(t.lossless for t in self.tracks)

    def ffmpeg_args(self) -> list[str]:
        """Per-track output options (``-c:a:N`` etc.) for master.master_command."""
        args: list[str] = []
        for t in self.tracks:
            n = t.track
            if t.codec == "copy":
                args += [f"-c:a:{n}", "copy"]
            elif t.codec == "flac":
                args += [f"-c:a:{n}", "flac", f"-compression_level:a:{n}", "8"]
            elif t.codec == "alac":
                args += [f"-c:a:{n}", "alac", f"-sample_fmt:a:{n}", t.note.split("sample_fmt=")[1] if "sample_fmt=" in t.note else "s32p"]
            elif t.codec == "aac":
                args += [f"-c:a:{n}", "aac", f"-b:a:{n}", f"{t.bitrate_kbps}k"]
            elif t.codec == "libopus":
                args += [f"-c:a:{n}", "libopus", f"-b:a:{n}", f"{t.bitrate_kbps}k", f"-ar:a:{n}", "48000"]
            else:  # pcm_*
                args += [f"-c:a:{n}", t.codec]
        if self.strict_experimental:
            args += ["-strict", "experimental"]
        return args


def _sample_class(a: AudioProbe) -> tuple[str, int]:
    """('int' | 'float' | 'u8', bits) of the decoded samples of a source track."""
    fmt = (a.sample_fmt or "").rstrip("p")
    codec = (a.codec or "").lower()
    if fmt == "dbl":
        return "float", 64
    if fmt == "flt":
        return "float", 32
    if fmt == "u8":
        return "u8", 8
    if fmt in ("s16", "s32", "s64"):
        width = {"s16": 16, "s32": 32, "s64": 64}[fmt]
        return "int", min(a.bit_depth, width) if a.bit_depth else width  # 24-bit sources decode to s32
    # no sample_fmt known (recorded analyses): fall back to codec name / bit depth
    if codec.startswith("pcm_f64"):
        return "float", 64
    if codec.startswith("pcm_f32") or codec in _LOSSY_CODECS or not codec.startswith("pcm_") and not a.bit_depth:
        return "float", 32
    if codec == "pcm_u8":
        return "u8", 8
    return "int", a.bit_depth or 16


def _pcm_codec(a: AudioProbe) -> str:
    kind, bits = _sample_class(a)
    if kind == "float":
        return "pcm_f64le" if bits > 32 else "pcm_f32le"
    if kind == "u8":
        return "pcm_u8"
    return "pcm_s16le" if bits <= 16 else "pcm_s24le" if bits <= 24 else "pcm_s32le"


def _layout_ok_for_alac(a: AudioProbe) -> bool:
    if a.channel_layout and a.channel_layout not in ("unknown", "unspecified"):
        return a.channel_layout in ALAC_LAYOUTS
    return a.channels in ALAC_CHANNELS


def _unsupported(mode: AudioMode, a: AudioProbe, why: str, hint: str) -> QtgmcError:
    return QtgmcError(
        ErrorCode.AUDIO_MODE_UNSUPPORTED,
        f"audio mode {mode.value}: audio track #{a.index} ({a.codec}, {a.channels} ch, {a.sample_rate} Hz) {why}",
        hint,
        {"mode": mode.value, "stream_index": a.index, "codec": a.codec, "channels": a.channels, "sample_rate": a.sample_rate},
    )


def _copy_possible(a: AudioProbe, probe_mux: Any) -> tuple[bool, str]:
    """Can Matroska carry the source codec as-is?  ``probe_mux(container, encoder)`` returns a
    MuxProbe (muxmatrix.probe); None skips the check (recorded analyses in tests)."""
    codec = (a.codec or "").lower()
    if not codec:
        return False, "the source codec is unknown"
    if probe_mux is None:
        return True, ""
    enc = _COPY_ENCODER.get(codec, codec)
    try:
        res = probe_mux(CONTAINER, enc)
    except Exception as e:  # noqa: BLE001 - a failed probe must not break planning
        return False, f"the test mux for {codec} failed: {e}"
    verdict = getattr(res, "verdict", None)
    if verdict is not None and verdict.value == "fail":
        return False, getattr(res, "note", "") or f"Matroska cannot carry {codec}"
    return True, ""


def plan(mode: AudioMode | str, audio: list[AudioProbe], *, probe_mux: Any = "default") -> AudioPlan:
    """Decide the master audio per source track; raises QtgmcError(AUDIO_MODE_UNSUPPORTED) for
    combinations the mode cannot take.  ``probe_mux``: muxmatrix.probe-like callable for the copy
    check ("default" = the real one, None = assume Matroska takes everything)."""
    mode = AudioMode(mode)
    if probe_mux == "default":
        from . import muxmatrix

        def probe_mux(container: str, encoder: str) -> Any:  # noqa: F811
            return muxmatrix.probe(container, encoder, kind="audio")

    p = AudioPlan(mode=mode)
    lossy_tracks: list[int] = []
    for n, a in enumerate(audio):
        t = AudioTrackPlan(index=a.index, track=n, source_codec=a.codec)
        kind, bits = _sample_class(a)
        if mode == AudioMode.COPY:
            ok, why = _copy_possible(a, probe_mux)
            if ok:
                t.codec, t.expect, t.lossless = "copy", a.codec, True
            else:
                t.codec, t.expect, t.lossless = "flac", "flac", True
                t.note = f"copy not possible ({why}); stored as FLAC"
                p.warnings.append(("audio_copy_unsupported",
                                   f"audio track #{a.index}: Matroska cannot carry {a.codec} unchanged; it is stored as FLAC "
                                   "(lossless) instead",
                                   "choose --audio flac/pcm to silence this, or remux the source to a Matroska-friendly codec"))
                if kind == "float":
                    p.warnings.append(("audio", f"audio stream {a.index} ({a.codec}) is floating point; FLAC stores it as 24-bit integer", ""))
                if kind == "int" and bits > 24:
                    p.strict_experimental = True
        elif mode == AudioMode.PCM:
            t.codec = t.expect = _pcm_codec(a)
            t.lossless = True
        elif mode == AudioMode.FLAC:
            t.codec, t.expect, t.lossless = "flac", "flac", True
            if kind == "float":
                p.warnings.append(("audio", f"audio stream {a.index} ({a.codec}) is floating point; FLAC stores it as 24-bit integer", ""))
            if kind == "int" and bits > 24:
                p.strict_experimental = True
        elif mode == AudioMode.ALAC:
            if kind == "float":
                raise _unsupported(mode, a, "is floating point, which ALAC cannot store losslessly",
                                   "choose --audio flac, pcm or copy for this file")
            if not _layout_ok_for_alac(a):
                raise _unsupported(mode, a, f"has a channel layout ALAC does not support ({a.channel_layout or 'unknown'})",
                                   "choose --audio flac, pcm or copy for this file")
            fmt = "s16p" if bits <= 16 else "s32p"
            t.codec, t.expect, t.lossless, t.note = "alac", "alac", True, f"sample_fmt={fmt}"
        elif mode == AudioMode.AAC:
            if a.channels > MAX_CHANNELS or a.channels < 1:
                raise _unsupported(mode, a, f"has {a.channels} channels; AAC takes 1 to {MAX_CHANNELS}",
                                   "choose a lossless mode (copy, pcm, flac) for this file")
            if a.sample_rate and a.sample_rate not in AAC_RATES:
                raise _unsupported(mode, a, f"has a sample rate AAC does not support ({a.sample_rate} Hz)",
                                   "choose a lossless mode (copy, pcm, flac) or Opus (resamples to 48 kHz)")
            t.codec, t.expect, t.lossless = "aac", "aac", False
            t.bitrate_kbps = min(AAC_KBPS_PER_CHANNEL * max(a.channels, 1), AAC_MAX_KBPS)
            lossy_tracks.append(a.index)
        elif mode == AudioMode.OPUS:
            if a.channels > MAX_CHANNELS or a.channels < 1:
                raise _unsupported(mode, a, f"has {a.channels} channels; Opus takes 1 to {MAX_CHANNELS}",
                                   "choose a lossless mode (copy, pcm, flac) for this file")
            t.codec, t.expect, t.lossless = "libopus", "opus", False
            t.bitrate_kbps = min(OPUS_KBPS_PER_CHANNEL * max(a.channels, 1), OPUS_MAX_KBPS)
            if a.sample_rate and a.sample_rate not in OPUS_RATES:
                t.note = f"resampled {a.sample_rate} -> 48000 Hz"
            lossy_tracks.append(a.index)
        p.tracks.append(t)
    if lossy_tracks:
        label = info(mode).label
        p.warnings.append(("audio_lossy",
                           f"{label}: the master's audio is no longer the original (track(s) {', '.join('#' + str(i) for i in lossy_tracks)} are re-encoded lossily)",
                           "choose copy, pcm, flac or alac to keep the original samples"))
    return p
