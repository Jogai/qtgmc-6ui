"""Master audio modes end to end: every mode on the reference clip and on synthetic clips
(32 kHz mono, 44.1 kHz stereo, 5.1, two tracks, no audio, A/V start offset).

Lossless modes decode bit-identically to the source; copy keeps the codec and the packet
data; aac/opus keep duration and sync (cross-correlation within 1 ms) and warn audio_lossy;
invalid combinations are coded errors before anything is written.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from helpers_wpb import reference_analysis, synth_analysis
from qtgmc_6ui import audiomodes, pipeline, vsenv
from qtgmc_6ui.errors import ErrorCode, QtgmcError
from qtgmc_6ui.events import Emitter, Event
from qtgmc_6ui.models import (
    AudioMode,
    FieldOrder,
    FieldOrderChoice,
    JobSpec,
    MasterSettings,
    MuxProbe,
    MuxVerdict,
    Preset,
)
from qtgmc_6ui.runtime import CancelToken

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
LOSSLESS = [AudioMode.COPY, AudioMode.PCM, AudioMode.FLAC, AudioMode.ALAC]
LOSSY = [AudioMode.AAC, AudioMode.OPUS]


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, ev: Event) -> None:
        self.events.append(ev)

    def of(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.ev == kind]

    def warning_codes(self) -> list[str]:
        return [e.data["code"] for e in self.of("warning")]


def probe(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def audio_streams(path: str) -> list[dict]:
    return [s for s in probe(path)["streams"] if s["codec_type"] == "audio"]


def decoded_md5(path: str, track: int, fmt: str = "pcm_s32le") -> str:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-map", f"0:a:{track}", "-c:a", fmt, "-f", "md5", "-"],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def pcm(path: str, track: int = 0) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-map", f"0:a:{track}", "-f", "s16le", "-ac", "1", "-ar", "48000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<i2").astype(np.float64)


def lag_samples(a: np.ndarray, b: np.ndarray) -> int:
    """Lag of b relative to a (FFT cross-correlation of the first 10 s): a[i + k] ~ b[i]."""
    n = min(len(a), len(b), 48000 * 10)
    x, y = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    c = np.fft.irfft(np.fft.rfft(x, 2 * n) * np.conj(np.fft.rfft(y, 2 * n)))
    return int(np.argmax(np.concatenate([c[-n + 1:], c[:n]]))) - (n - 1)


def stream_duration(s: dict, info: dict) -> float:
    if s.get("duration"):
        return float(s["duration"])
    t = s.get("tags", {}).get("DURATION")
    if t:
        h, m, sec = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    return float(info["format"]["duration"])


def _gen(out: Path, *args: str, video: str = "mandelbrot=s=352x288:r=50", audio: list[str] | None = None,
         vf: str = "tinterlace=mode=interleave_top,setfield=tff,format=yuv420p", t: str = "2", video_offset: str | None = None,
         maps: list[str] | None = None) -> Path:
    cmd = FF + (["-itsoffset", video_offset] if video_offset else []) + ["-f", "lavfi", "-i", video]
    for a in audio or ["sine=r=48000:f=440"]:
        cmd += ["-f", "lavfi", "-i", a]
    cmd += ["-vf", vf, "-t", t]
    if maps:
        for m in maps:
            cmd += ["-map", m]
    cmd += [*args, str(out)]
    subprocess.run(cmd, check=True)
    return out


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    d = tmp_path_factory.mktemp("aclips")
    # "anoisesrc" gives a broadband signal whose cross-correlation has one sharp peak (a sine would not)
    noise = "anoisesrc=r={r}:c=pink:a=0.5:seed=7"
    return {
        "mono32": _gen(d / "mono32.mkv", "-c:v", "ffv1", "-c:a", "pcm_s16le", audio=[noise.format(r=32000)]),
        "st44": _gen(d / "st44.mkv", "-c:v", "ffv1", "-c:a", "pcm_s16le", "-ac", "2", audio=[noise.format(r=44100)]),
        "ch51": _gen(d / "ch51.mkv", "-c:v", "ffv1", "-c:a", "pcm_s24le", audio=[noise.format(r=48000) + ",aformat=channel_layouts=5.1"]),
        "two": _gen(d / "two.mkv", "-c:v", "ffv1", "-c:a:0", "pcm_s16le", "-c:a:1", "pcm_s24le",
                    audio=[noise.format(r=48000), "sine=r=48000:f=880"], maps=["0:v", "1:a", "2:a"]),
        "silent": _gen(d / "silent.mkv", "-c:v", "ffv1", "-an"),
        "offset": _gen(d / "offset.ts", "-c:v", "mpeg2video", "-q:v", "2", "-flags", "+ildct+ilme", "-c:a", "mp2", "-f", "mpegts",
                       audio=[noise.format(r=48000)], video_offset="0.5"),
        "float": _gen(d / "float.mkv", "-c:v", "ffv1", "-c:a", "pcm_f32le", audio=[noise.format(r=48000)]),
    }


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("QTGMC6UI_CONFIG_DIR", str(tmp_path / "config"))
    assert vsenv.current() is None
    return tmp_path


def _job(clip: Path, out: Path, mode: AudioMode, fo: FieldOrder = FieldOrder.TFF, analysis=None) -> JobSpec:
    return JobSpec(input=str(clip), master_output=str(out), analysis=analysis or synth_analysis(str(clip), fo),
                   master=MasterSettings(preset=Preset.DRAFT, field_order=FieldOrderChoice(fo.value), audio=mode))


def _run(job: JobSpec) -> tuple[object, Recorder]:
    rec = Recorder()
    res = pipeline.run_job(job, emitter=Emitter(rec), cancel=CancelToken())
    return res, rec


def _check_lossless(src: str, out: str, res, rec: Recorder, mode: AudioMode) -> None:
    sa, oa = audio_streams(src), audio_streams(out)
    assert len(oa) == len(sa) == len(res.audio_tracks)
    for n, (s, o, t) in enumerate(zip(sa, oa, res.audio_tracks, strict=True)):
        want = s["codec_name"] if mode == AudioMode.COPY else t.expect
        assert o["codec_name"] == want == t.expect, (mode, n, o["codec_name"], t)
        is_float = s.get("sample_fmt", "").startswith(("flt", "dbl"))
        fmt = "pcm_f64le" if is_float else "pcm_s32le"
        if mode == AudioMode.FLAC and is_float:  # 24-bit integer FLAC of float samples: warned, not bit-exact
            assert not t.lossless and "audio" in rec.warning_codes()
        else:
            assert t.lossless
            assert decoded_md5(src, n, fmt) == decoded_md5(out, n, fmt), (mode, n)
        assert int(o["sample_rate"]) == int(s["sample_rate"]) and o["channels"] == s["channels"]
        if mode == AudioMode.COPY and not src.endswith(".ts"):  # packet data untouched (TS re-frames PES packets)
            def pkts(p: str, n: int = n) -> str:
                return subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-vn", "-map", f"0:a:{n}", "-c", "copy", "-f", "framemd5", "-"],
                                      capture_output=True, text=True, check=True).stdout.split("#stream")[-1]
            assert [ln.split(",")[-1] for ln in pkts(src).splitlines() if ln and not ln.startswith("#")] == \
                   [ln.split(",")[-1] for ln in pkts(out).splitlines() if ln and not ln.startswith("#")]
    assert "audio_lossy" not in rec.warning_codes()
    if any(t.lossless for t in res.audio_tracks):  # at least one bit-exact track was verified by decoded MD5
        assert any("audio_verified" in e.data["line"] for e in rec.of("log")), [e.data.get("line") for e in rec.of("log")]


def _check_lossy(src: str, out: str, res, rec: Recorder, mode: AudioMode) -> None:
    sa, oa = audio_streams(src), audio_streams(out)
    info_src, info_out = probe(src), probe(out)
    assert len(oa) == len(sa)
    for n, (s, o, t) in enumerate(zip(sa, oa, res.audio_tracks, strict=True)):
        assert o["codec_name"] == t.expect == ("aac" if mode == AudioMode.AAC else "opus")
        assert o["channels"] == s["channels"]
        if mode == AudioMode.OPUS:
            assert int(o["sample_rate"]) == 48000
        d_src, d_out = stream_duration(s, info_src), stream_duration(o, info_out)
        assert abs(d_src - d_out) <= pipeline.AUDIO_DURATION_TOLERANCE_S, (mode, n, d_src, d_out)
        k = lag_samples(pcm(src, n), pcm(out, n))
        assert abs(k) <= 48, f"{mode} track {n}: audio shifted by {k} samples"
    if sa:
        assert "audio_lossy" in rec.warning_codes()
        assert any("no longer the original" in w for w in res.warnings)


@pytest.mark.needs_qtgmc
@pytest.mark.parametrize("mode", LOSSLESS + LOSSY)
def test_reference_clip_every_mode(env: Path, reference_clip: Path, mode: AudioMode) -> None:
    out = env / f"ref_{mode.value}.mkv"
    res, rec = _run(_job(reference_clip, out, mode, FieldOrder.BFF, reference_analysis(str(reference_clip))))
    assert res.outputs[0].frames == 548 and res.effective_master.audio == mode
    (_check_lossy if mode in LOSSY else _check_lossless)(str(reference_clip), str(out), res, rec, mode)
    if mode == AudioMode.PCM:
        assert audio_streams(str(out))[0]["codec_name"] == "pcm_s16le"
    if mode == AudioMode.ALAC:
        assert audio_streams(str(out))[0]["sample_fmt"] == "s16p"


@pytest.mark.needs_qtgmc
@pytest.mark.parametrize("clip", ["mono32", "st44", "ch51", "two", "silent", "offset"])
@pytest.mark.parametrize("mode", LOSSLESS + LOSSY)
def test_synthetic_clips_every_mode(env: Path, clips: dict[str, Path], clip: str, mode: AudioMode) -> None:
    src = clips[clip]
    out = env / f"{clip}_{mode.value}.mkv"
    if clip == "offset" and mode == AudioMode.ALAC:  # mp2 decodes to float: ALAC is refused up front
        with pytest.raises(QtgmcError) as ei:
            _run(_job(src, out, mode))
        assert ei.value.code == ErrorCode.AUDIO_MODE_UNSUPPORTED and not out.exists()
        return
    res, rec = _run(_job(src, out, mode))
    assert out.is_file()
    (_check_lossy if mode in LOSSY else _check_lossless)(str(src), str(out), res, rec, mode)
    if clip == "silent":
        assert res.audio_tracks == [] and audio_streams(str(out)) == []
    if clip == "offset":  # the A/V start offset survives every mode
        src_av = pipeline.source_timing(probe(str(src)))["av_offset_s"]
        out_av = pipeline.source_timing(probe(str(out)))["av_offset_s"]
        assert abs(src_av - out_av) <= 0.03, (mode, src_av, out_av)
    if clip == "ch51":
        assert all(o["channels"] == 6 for o in audio_streams(str(out)))
        if mode == AudioMode.ALAC:
            assert audio_streams(str(out))[0]["sample_fmt"] == "s32p" and int(audio_streams(str(out))[0]["bits_per_raw_sample"]) == 24
        if mode == AudioMode.PCM:
            assert audio_streams(str(out))[0]["codec_name"] == "pcm_s24le"
        if mode == AudioMode.AAC:
            assert res.audio_tracks[0].bitrate_kbps == 640
    if clip == "two":
        assert len(audio_streams(str(out))) == 2
        if mode == AudioMode.PCM:
            assert [o["codec_name"] for o in audio_streams(str(out))] == ["pcm_s16le", "pcm_s24le"]
    if clip == "mono32" and mode == AudioMode.OPUS:
        assert "32000 -> 48000" in res.audio_tracks[0].note


@pytest.mark.needs_qtgmc
def test_copy_fallback_to_flac(env: Path, clips: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui import muxmatrix

    monkeypatch.setattr(muxmatrix, "probe", lambda c, e, *, kind, ffmpeg="ffmpeg": MuxProbe(c, e, MuxVerdict.FAIL, "test: cannot mux"))
    src = clips["st44"]
    out = env / "fallback.mkv"
    res, rec = _run(_job(src, out, AudioMode.COPY))
    assert audio_streams(str(out))[0]["codec_name"] == "flac"
    assert res.audio_tracks[0].codec == "flac" and "FLAC" in res.audio_tracks[0].note
    assert "audio_copy_unsupported" in rec.warning_codes() and any("stored as FLAC" in w for w in res.warnings)
    assert decoded_md5(str(src), 0) == decoded_md5(str(out), 0)


@pytest.mark.needs_qtgmc
def test_alac_float_is_refused_before_writing(env: Path, clips: dict[str, Path]) -> None:
    out = env / "float_alac.mkv"
    with pytest.raises(QtgmcError) as ei:
        _run(_job(clips["float"], out, AudioMode.ALAC))
    assert ei.value.code == ErrorCode.AUDIO_MODE_UNSUPPORTED and "floating point" in ei.value.message
    assert not out.exists() and not list(env.glob(".*.partial"))
    # ... while pcm keeps float as float and flac stores it as 24-bit with a warning
    res, rec = _run(_job(clips["float"], env / "float_pcm.mkv", AudioMode.PCM))
    assert audio_streams(str(env / "float_pcm.mkv"))[0]["codec_name"] == "pcm_f32le"
    assert decoded_md5(str(clips["float"]), 0, "pcm_f64le") == decoded_md5(str(env / "float_pcm.mkv"), 0, "pcm_f64le")
    res, rec = _run(_job(clips["float"], env / "float_flac.mkv", AudioMode.FLAC))
    assert "audio" in rec.warning_codes() and audio_streams(str(env / "float_flac.mkv"))[0]["codec_name"] == "flac"


@pytest.mark.needs_sandbox
def test_dry_run_reports_audio_plan_and_errors(env: Path, clips: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    from qtgmc_6ui import cli

    rc = cli.main(["deint", str(clips["two"]), "-o", str(env / "x.mkv"), "--dry-run", "--json", "--audio", "pcm",
                   "--field-order", "tff", "--force", "--preset", "draft"])
    out = capsys.readouterr().out
    assert rc == 0, out
    plan = json.loads(out)
    assert [t["expect"] for t in plan["audio_tracks"]] == ["pcm_s16le", "pcm_s24le"]
    assert "-c:a:1" in plan["master_command"] and plan["master_command"][plan["master_command"].index("-c:a:1") + 1] == "pcm_s24le"
    assert not (env / "x.mkv").exists()
    rc = cli.main(["deint", str(clips["float"]), "-o", str(env / "y.mkv"), "--dry-run", "--json", "--audio", "alac",
                   "--field-order", "tff", "--force", "--preset", "draft"])
    err = json.loads(capsys.readouterr().out)["error"]
    assert rc == 2 and err["code"] == "audio_mode_unsupported" and "flac" in err["hint"]
    rc = cli.main(["deint", str(clips["two"]), "-o", str(env / "z.mkv"), "--dry-run", "--audio", "opus",
                   "--field-order", "tff", "--force", "--preset", "draft"])
    text = capsys.readouterr().out
    assert rc == 0 and "audio #1: pcm_s16le -> opus 96 kbps" in text and "no longer the original" in text, text  # mono tracks


def test_plan_matches_probe_of_real_reference() -> None:
    a = reference_analysis()
    p = audiomodes.plan(AudioMode.COPY, a.probe.audio, probe_mux=None)
    assert p.tracks[0].expect == "pcm_s16le" and p.lossless
