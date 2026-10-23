"""Audio modes of the master: catalogue, planning/validation, CLI contract, JobSpec round trip."""

from __future__ import annotations

import json

import pytest

from qtgmc_6ui import audiomodes, cli, jobs
from qtgmc_6ui.errors import ErrorCode, ExitCode, QtgmcError
from qtgmc_6ui.models import AudioMode, AudioProbe, JobSpec, MasterSettings, MuxProbe, MuxVerdict

FORBIDDEN = ("best", "worst", "default")


def _a(codec="pcm_s16le", fmt="s16", bits=16, ch=2, layout="stereo", rate=48000, index=1) -> AudioProbe:
    return AudioProbe(index=index, codec=codec, sample_rate=rate, channels=ch, channel_layout=layout, bit_depth=bits, sample_fmt=fmt)


def test_catalogue_order_labels_and_wording() -> None:
    ids = [m.id for m in audiomodes.MODES]
    assert ids == [AudioMode.COPY, AudioMode.PCM, AudioMode.FLAC, AudioMode.ALAC, AudioMode.AAC, AudioMode.OPUS]
    assert audiomodes.DEFAULT == AudioMode.COPY and MasterSettings().audio == AudioMode.COPY
    labels = [m.label for m in audiomodes.MODES]
    assert labels == ["Copy source audio", "PCM (uncompressed)", "FLAC (lossless)", "ALAC (lossless)",
                      "AAC 256 kbps (lossy)", "Opus 192 kbps (lossy)"]
    for m in audiomodes.MODES:
        text = (m.label + " " + m.tooltip).lower()
        assert not any(w in text for w in FORBIDDEN), (m.id, text)
        assert m.group in ("lossless", "lossy") and m.lossy == (m.group == "lossy")
    d = audiomodes.describe()
    assert [x["id"] for x in d] == [i.value for i in ids]
    assert set(d[0]) == {"id", "label", "tooltip", "group", "lossy"}
    json.dumps(d)


def test_plan_copy_pcm_flac_alac() -> None:
    s16 = _a()
    p = audiomodes.plan("copy", [s16], probe_mux=None)
    assert p.lossless and p.tracks[0].codec == "copy" and p.tracks[0].expect == "pcm_s16le" and p.warnings == []
    assert p.ffmpeg_args() == ["-c:a:0", "copy"]
    # pcm: codec follows the sample format, never narrowed
    cases = [(_a(), "pcm_s16le"), (_a("pcm_s24le", "s32", 24), "pcm_s24le"), (_a("pcm_s32le", "s32", 32), "pcm_s32le"),
             (_a("pcm_f32le", "flt", 32), "pcm_f32le"), (_a("pcm_f64le", "dbl", 64), "pcm_f64le"), (_a("pcm_u8", "u8", 8), "pcm_u8"),
             (_a("aac", "fltp", None), "pcm_f32le"), (_a("mp3", "fltp", None), "pcm_f32le"), (_a("flac", "s32", 24), "pcm_s24le"),
             (_a("pcm_s16le", "", 16), "pcm_s16le"), (_a("aac", "", None), "pcm_f32le")]
    for a, want in cases:
        t = audiomodes.plan(AudioMode.PCM, [a], probe_mux=None).tracks[0]
        assert (t.codec, t.expect, t.lossless) == (want, want, True), (a, t)
    # flac: level 8, 32-bit needs -strict experimental, float warns
    p = audiomodes.plan("flac", [_a(), _a("pcm_s32le", "s32", 32, index=2)], probe_mux=None)
    assert p.ffmpeg_args() == ["-c:a:0", "flac", "-compression_level:a:0", "8", "-c:a:1", "flac", "-compression_level:a:1", "8", "-strict", "experimental"]
    p = audiomodes.plan("flac", [_a("pcm_f32le", "flt", 32)], probe_mux=None)
    assert p.warnings and p.warnings[0][0] == "audio" and "floating point" in p.warnings[0][1]
    assert not p.tracks[0].lossless and not p.lossless  # 24-bit integer FLAC of float samples is not bit-exact
    # alac: s16p / s32p, float refused, odd layouts refused
    assert audiomodes.plan("alac", [_a()], probe_mux=None).ffmpeg_args() == ["-c:a:0", "alac", "-sample_fmt:a:0", "s16p"]
    assert audiomodes.plan("alac", [_a("pcm_s24le", "s32", 24, 6, "5.1")], probe_mux=None).ffmpeg_args()[-1] == "s32p"
    with pytest.raises(QtgmcError) as ei:
        audiomodes.plan("alac", [_a("pcm_f32le", "flt", 32)], probe_mux=None)
    assert ei.value.code == ErrorCode.AUDIO_MODE_UNSUPPORTED and ei.value.exit_code == ExitCode.USAGE and "flac" in ei.value.hint
    with pytest.raises(QtgmcError):
        audiomodes.plan("alac", [_a(ch=2, layout="2.1")], probe_mux=None)


def test_plan_lossy_bitrates_and_validation() -> None:
    p = audiomodes.plan("aac", [_a(), _a(ch=1, layout="mono", index=2), _a(ch=6, layout="5.1", index=3)], probe_mux=None)
    assert [t.bitrate_kbps for t in p.tracks] == [256, 128, 640]
    assert not p.lossless and all(t.expect == "aac" for t in p.tracks)
    assert p.warnings[-1][0] == "audio_lossy" and "no longer the original" in p.warnings[-1][1]
    p = audiomodes.plan("opus", [_a(rate=44100), _a(ch=6, layout="5.1", rate=32000, index=2)], probe_mux=None)
    assert [t.bitrate_kbps for t in p.tracks] == [192, 512]
    assert p.ffmpeg_args()[:6] == ["-c:a:0", "libopus", "-b:a:0", "192k", "-ar:a:0", "48000"]
    assert "48000" in p.tracks[0].note and p.tracks[0].expect == "opus"
    for mode in ("aac", "opus"):
        with pytest.raises(QtgmcError) as ei:
            audiomodes.plan(mode, [_a(ch=9, layout="")], probe_mux=None)
        assert ei.value.code == ErrorCode.AUDIO_MODE_UNSUPPORTED
    with pytest.raises(QtgmcError):
        audiomodes.plan("aac", [_a(rate=5000)], probe_mux=None)
    assert audiomodes.plan("opus", [], probe_mux=None).tracks == [] and audiomodes.plan("opus", [], probe_mux=None).warnings == []


def test_plan_copy_fallback_uses_mux_probe() -> None:
    seen = []

    def probe_mux(container: str, encoder: str) -> MuxProbe:
        seen.append((container, encoder))
        return MuxProbe(container, encoder, MuxVerdict.FAIL if encoder == "libmp3lame" else MuxVerdict.OK, "nope" if encoder == "libmp3lame" else "")

    p = audiomodes.plan("copy", [_a("mp3", "fltp", None), _a("pcm_s24le", "s32", 24, index=2)], probe_mux=probe_mux)
    assert seen == [("matroska", "libmp3lame"), ("matroska", "pcm_s24le")]
    assert p.tracks[0].codec == "flac" and p.tracks[0].expect == "flac" and "FLAC" in p.tracks[0].note
    assert p.tracks[1].codec == "copy"
    assert p.warnings[0][0] == "audio_copy_unsupported" and "mp3" in p.warnings[0][1]
    assert p.tracks[1].lossless and not p.tracks[0].lossless  # mp3 decodes to float: 24-bit FLAC is not bit-exact
    # a 32-bit source that falls back needs the strict flag, float a warning
    p = audiomodes.plan("copy", [_a("pcm_s32le", "s32", 32)], probe_mux=lambda c, e: MuxProbe(c, e, MuxVerdict.FAIL))
    assert p.strict_experimental and p.ffmpeg_args()[-2:] == ["-strict", "experimental"]
    p = audiomodes.plan("copy", [_a("", "", None)], probe_mux=None)
    assert p.tracks[0].codec == "flac"  # unknown codec is never copied blindly


def test_cli_flag_and_jobspec_roundtrip() -> None:
    parser = cli.build_parser()
    a = parser.parse_args(["deint", "in.avi", "--audio", "alac"])
    assert jobs.master_settings_from_args(a).audio == AudioMode.ALAC
    assert jobs.master_settings_from_args(parser.parse_args(["deint", "in.avi"])).audio == AudioMode.COPY
    with pytest.raises(SystemExit):
        parser.parse_args(["deint", "in.avi", "--audio", "mp3"])
    am = parser.parse_args(["audio-modes", "--json"])
    assert am.command == "audio-modes" and am.json
    j = JobSpec(input="x.avi", master=MasterSettings(audio=AudioMode.OPUS))
    d = json.loads(json.dumps(j.to_dict()))
    assert d["master"]["audio"] == "opus" and JobSpec.from_dict(d) == j
    old = JobSpec(input="x.avi").to_dict()
    del old["master"]["audio"]  # a job written before the field existed
    assert JobSpec.from_dict(old).master.audio == AudioMode.COPY


def test_cmd_audio_modes(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["audio-modes", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert isinstance(d, list) and [m["id"] for m in d] == ["copy", "pcm", "flac", "alac", "aac", "opus"]
    assert d[0]["label"] == "Copy source audio" and d[-1]["lossy"] is True and d[0]["group"] == "lossless"
    assert cli.main(["audio-modes"]) == 0
    out = capsys.readouterr().out
    assert "Copy source audio" in out and "opus" in out
    body = "\n".join(ln for ln in out.splitlines() if "initial value" not in ln).lower()
    assert not any(w in body for w in FORBIDDEN)
