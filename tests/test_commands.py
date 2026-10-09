"""commands: in-process wiring of profiles/encoders/doctor/preview/script, error mapping,
run_cancellable.  Lower layers are faked with monkeypatch."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from qtgmc_6ui import cli, commands, runtime
from qtgmc_6ui.errors import CancelledError, ErrorCode, QtgmcError
from qtgmc_6ui.models import (
    DoctorCheck,
    DoctorReport,
    EncoderInfo,
    MuxerInfo,
    MuxProbe,
    MuxVerdict,
    PreviewResult,
    Profile,
    Status,
    ToolInfo,
)


def _main(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    rc = cli.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


# --------------------------------------------------------------------------- helpers
def test_internal_error_mapping() -> None:
    try:
        raise NotImplementedError
    except NotImplementedError as e:
        q = commands.internal_error(e)
    assert q.code == ErrorCode.INTERNAL and "not implemented" in q.message and "test_commands.py" in q.message
    q = commands.internal_error(ValueError("bad"))
    assert "ValueError: bad" in q.message and "traceback" in q.details
    orig = QtgmcError(ErrorCode.ENCODE_FAILED, "x")
    assert commands.internal_error(orig) is orig


def test_run_cancellable_paths() -> None:
    tok = runtime.CancelToken()
    assert commands.run_cancellable(tok, lambda: 42) == 42
    with pytest.raises(ValueError):
        commands.run_cancellable(tok, lambda: (_ for _ in ()).throw(ValueError("x")))

    tok = runtime.CancelToken()
    stop = threading.Event()

    def cooperative() -> int:
        while not tok.cancelled:
            time.sleep(0.01)
        raise QtgmcError(ErrorCode.ENCODE_FAILED, "killed")  # child died because we killed it

    threading.Timer(0.1, tok.cancel).start()
    with pytest.raises(CancelledError):
        commands.run_cancellable(tok, cooperative)

    tok = runtime.CancelToken()
    threading.Timer(0.1, tok.cancel).start()
    t0 = time.monotonic()
    with pytest.raises(CancelledError):
        commands.run_cancellable(tok, lambda: stop.wait(30), grace_s=0.2)  # ignores the token
    assert time.monotonic() - t0 < 5
    stop.set()


# --------------------------------------------------------------------------- profiles
@pytest.fixture
def fake_profiles(monkeypatch: pytest.MonkeyPatch):
    from qtgmc_6ui import profiles

    a = Profile(id="mp4-a", name="MP4 A", container="mp4", extension="mp4", builtin=True)
    b = Profile(id="mine", name="Mine", container="matroska", extension="mkv", path="/x/mine.toml")
    by = {"mp4-a": a, "mine": b}

    def load(i: str) -> Profile:
        if i not in by:
            raise QtgmcError(ErrorCode.PROFILE_NOT_FOUND, f"profile not found: {i}", "see profiles list")
        return by[i]

    monkeypatch.setattr(profiles, "list_profiles", lambda include_unavailable=False: [a, b])
    monkeypatch.setattr(profiles, "is_available", lambda p: True)
    monkeypatch.setattr(profiles, "load", load)
    monkeypatch.setattr(profiles, "validate", lambda p, **kw: [] if p.builtin else ["encoder missing"])
    monkeypatch.setattr(profiles, "to_toml", lambda p: f'id = "{p.id}"\n')
    return by


def test_profiles(capsys, fake_profiles, tmp_path: Path, isolated_dirs) -> None:
    rc, out, _ = _main(capsys, "profiles", "list", "--json")
    assert rc == 0 and [p["id"] for p in json.loads(out)] == ["mp4-a", "mine"]
    rc, out, _ = _main(capsys, "profiles", "list")
    assert rc == 0 and "built-in" in out and "user" in out
    rc, out, _ = _main(capsys, "profiles", "show", "mine")
    assert rc == 0 and out == 'id = "mine"\n'
    rc, out, _ = _main(capsys, "profiles", "validate", "mp4-a", "--json")
    assert rc == 0 and json.loads(out)["valid"] is True
    rc, out, _ = _main(capsys, "profiles", "validate", "mine")
    assert rc == 2 and "encoder missing" in out
    rc, out, _ = _main(capsys, "profiles", "show", "nope", "--json")
    assert rc == 2 and json.loads(out)["error"]["code"] == "profile_not_found"
    rc, _, err = _main(capsys, "profiles", "show")
    assert rc == 2 and "needs a profile id" in err
    dest = tmp_path / "e.toml"
    rc, out, _ = _main(capsys, "profiles", "export", "mine", "-o", str(dest))
    assert rc == 0 and dest.read_text() == 'id = "mine"\n'
    rc, out, _ = _main(capsys, "profiles", "export", "mine", "-o", str(dest), "--json")
    assert rc == 7 and json.loads(out)["error"]["code"] == "output_exists"
    rc, out, _ = _main(capsys, "profiles", "path", "--json")
    assert rc == 0 and json.loads(out)["path"].endswith("profiles")


# --------------------------------------------------------------------------- encoders
@pytest.fixture
def fake_encoders(monkeypatch: pytest.MonkeyPatch):
    from qtgmc_6ui import encoders, muxmatrix

    monkeypatch.setattr(encoders, "detect_tools", lambda: [ToolInfo("ffmpeg", "/usr/bin/ffmpeg", "n9.0.2", True), ToolInfo("x264")])
    monkeypatch.setattr(encoders, "list_muxers", lambda: [MuxerInfo("mp4"), MuxerInfo("mov"), MuxerInfo("matroska")])
    monkeypatch.setattr(encoders, "list_encoders", lambda: [EncoderInfo("libx264", "video"), EncoderInfo("flac", "audio")])
    monkeypatch.setattr(encoders, "encoder_details", lambda n: EncoderInfo(n, "video", "H.264", pix_fmts=["yuv420p"]))
    exts = {"mp4": ["mp4", "m4a"], "mov": ["mov"], "matroska": ["mkv"]}
    monkeypatch.setattr(encoders, "muxer_details", lambda n: MuxerInfo(n, extensions=exts[n]))
    monkeypatch.setattr(encoders, "is_hw_encoder", lambda n: n.endswith("_vaapi"))
    calls = {}

    def probe(c, e, *, kind):
        calls["probe"] = (c, e, kind)
        return MuxProbe(c, e, MuxVerdict.FAIL if (c, e) == ("mov", "flac") else MuxVerdict.OK)

    def probe_all(cons, video, audio):
        calls["probe_all"] = (cons, video, audio)
        return [MuxProbe(c, e, MuxVerdict.OK) for c in cons for e in video + audio]

    monkeypatch.setattr(muxmatrix, "probe", probe)
    monkeypatch.setattr(muxmatrix, "probe_all", probe_all)
    monkeypatch.setattr(muxmatrix, "verdict", lambda c, e, *, kind: MuxVerdict.OK)

    def cached(c, e, *, kind):
        if (c, e) == ("mov", "flac"):
            return MuxProbe(c, e, MuxVerdict.FAIL, "Nothing was written")
        if e == "libx264":
            return MuxProbe(c, e, MuxVerdict.OK)
        return MuxProbe(c, e)

    def reason(c, e, *, kind):
        calls.setdefault("reason", []).append((c, e))
        return "mov does not accept flac" if (c, e) == ("mov", "flac") else ""

    monkeypatch.setattr(muxmatrix, "cached", cached)
    monkeypatch.setattr(muxmatrix, "reason", reason)
    return calls


def test_encoders(capsys, fake_encoders) -> None:
    rc, out, _ = _main(capsys, "encoders", "--json")
    d = json.loads(out)
    assert rc == 0 and d["tools"][0]["name"] == "ffmpeg" and len(d["muxers"]) == 3 and len(d["encoders"]) == 2
    rc, out, _ = _main(capsys, "encoders")
    assert rc == 0 and "x264" in out and "not installed" in out and "libx264" in out
    assert {m["name"]: m["extensions"] for m in d["muxers"]} == {"mp4": ["mp4", "m4a"], "mov": ["mov"], "matroska": ["mkv"]}
    rc, out, _ = _main(capsys, "encoders", "--container", "mov", "--encoder", "flac", "--probe", "--json")
    assert rc == 0 and json.loads(out)["probes"][0]["verdict"] == "fail"
    assert fake_encoders["probe"] == ("mov", "flac", "audio")
    # without --probe: cache only, with reasons, never probes
    fake_encoders.pop("probe")
    rc, out, _ = _main(capsys, "encoders", "--container", "mov", "--json")
    d = json.loads(out)
    by = {p["encoder"]: p for p in d["probes"]}
    assert "probe" not in fake_encoders and d["muxer"]["extensions"] == ["mov"]
    assert by["flac"]["verdict"] == "fail" and by["flac"]["reason"] == "mov does not accept flac"
    assert by["libx264"]["verdict"] == "ok" and by["libx264"]["reason"] == ""
    rc, out, _ = _main(capsys, "encoders", "--container", "mp4", "--kind", "audio", "--json")
    d = json.loads(out)
    assert [p["encoder"] for p in d["probes"]] == ["flac"] and d["probes"][0]["verdict"] == "unknown"
    assert d["unprobed"] == 1 and "not tested" in d["probes"][0]["reason"]
    rc, out, _ = _main(capsys, "encoders", "--probe", "--json")
    assert rc == 0 and fake_encoders["probe_all"] == (["matroska", "mp4", "mov"], ["libx264"], ["flac"])
    rc, out, _ = _main(capsys, "encoders", "--encoder", "libx264")
    assert rc == 0 and "pix_fmts: yuv420p" in out
    rc, out, _ = _main(capsys, "encoders", "--encoder", "nope", "--json")
    assert rc == 2


# --------------------------------------------------------------------------- doctor
def test_doctor(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui import doctor

    seen = {}

    def run(*, load_plugins: bool, smoke: bool) -> DoctorReport:
        seen.update(load=load_plugins, smoke=smoke)
        ok = load_plugins
        return DoctorReport(ok=ok, summary="s", checks=[DoctorCheck("x", "X", Status.OK if ok else Status.FAIL)])

    monkeypatch.setattr(doctor, "run", run)
    monkeypatch.setattr(doctor, "render_text", lambda r: f"doctor ok={r.ok}")
    rc, out, _ = _main(capsys, "doctor", "--json")
    assert rc == 0 and json.loads(out)["ok"] is True and seen == {"load": True, "smoke": True}
    rc, out, _ = _main(capsys, "doctor", "--no-load")
    assert rc == 4 and out == "doctor ok=False\n" and seen == {"load": False, "smoke": False}


# --------------------------------------------------------------------------- preview wiring
def test_preview_frame_wiring(capsys, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from qtgmc_6ui import preview

    clip = tmp_path / "c.avi"
    clip.write_bytes(b"x")
    got = {}

    def render(req):
        got["req"] = req
        return PreviewResult(output=req.output, width=720, height=1152, frames=2, took_s=0.05)

    monkeypatch.setattr(preview, "render", render)
    monkeypatch.setattr(preview, "source_info", lambda p: {"frames": 274, "fps": "25/1"})
    rc, out, err = _main(capsys, "preview-frame", str(clip), "7", "--mode", "fields", "--field-order", "bff", "-o", str(tmp_path / "p.png"), "--json")
    d = json.loads(out)
    assert rc == 0 and d["height"] == 1152 and d["source"]["frames"] == 274
    r = got["req"]
    assert r.frame == 7 and r.mode.value == "fields" and r.field_order.value == "bff" and r.output == str(tmp_path / "p.png")
    rc, out, err = _main(capsys, "preview-frame", str(clip), "7", "-o", "-", "--json")
    assert rc == 0 and out == "" and json.loads(err)["width"] == 720  # stdout is reserved for PNG bytes
    rc, out, _ = _main(capsys, "preview-frame", str(clip), "7", "-o", str(clip), "--json")
    assert rc == 2 and "overwrite the input" in json.loads(out)["error"]["message"]
    rc, out, _ = _main(capsys, "preview-frame", str(tmp_path / "nope.avi"), "1", "-o", "x.png", "--json")
    assert rc == 5


def test_not_implemented_component_is_reported(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui import doctor

    def boom(**kw):
        raise NotImplementedError

    monkeypatch.setattr(doctor, "run", boom)
    rc, out, err = _main(capsys, "doctor", "--json")
    assert rc == 1 and json.loads(out)["error"]["code"] == "internal"


def test_profiles_save_delete(capsys, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_dirs) -> None:
    import io

    from qtgmc_6ui import profiles

    monkeypatch.setattr(profiles, "builtin_profiles", lambda **kw: [Profile(id="mp4-h264-aac", builtin=True)])
    monkeypatch.setattr(profiles, "validate", lambda p, **kw: ["bad encoder"] if p.video.encoder == "nope" else [])
    good = Profile(id="my-copy", name="Mine", container="mp4", extension="mp4")
    toml = profiles.to_toml(good)
    monkeypatch.setattr("sys.stdin", io.StringIO(toml))
    rc, out, _ = _main(capsys, "profiles", "save", "-", "--json")
    d = json.loads(out)
    assert rc == 0 and d["valid"] and d["id"] == "my-copy" and Path(d["path"]).read_text() == toml
    assert d["profile"]["name"] == "Mine"
    # exists -> 7 unless --overwrite; JSON input; --id
    monkeypatch.setattr("sys.stdin", io.StringIO(toml))
    rc, out, _ = _main(capsys, "profiles", "save", "-", "--json")
    assert rc == 7 and json.loads(out)["error"]["code"] == "output_exists" and json.loads(out)["path"] is None
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(good.to_dict())))
    rc, out, _ = _main(capsys, "profiles", "save", "-", "--overwrite", "--json")
    assert rc == 0
    f = tmp_path / "p.toml"
    f.write_text(toml)
    rc, out, _ = _main(capsys, "profiles", "save", str(f), "--id", "other", "--json")
    assert rc == 0 and json.loads(out)["path"].endswith("other.toml")
    # built-in id, invalid, unknown keys, garbage
    rc, out, _ = _main(capsys, "profiles", "save", str(f), "--id", "mp4-h264-aac", "--json")
    assert rc == 2 and "built-in" in json.loads(out)["error"]["message"]
    bad = Profile(id="bad", name="B")
    bad.video.encoder = "nope"
    monkeypatch.setattr("sys.stdin", io.StringIO(profiles.to_toml(bad)))
    rc, out, _ = _main(capsys, "profiles", "save", "-", "--json")
    d = json.loads(out)
    assert rc == 2 and d["valid"] is False and d["problems"] == ["bad encoder"] and d["path"] is None
    monkeypatch.setattr("sys.stdin", io.StringIO('{"id": "x", "bogus": 1}'))
    rc, out, _ = _main(capsys, "profiles", "save", "-", "--json")
    assert rc == 2 and "bogus" in json.loads(out)["error"]["message"]
    monkeypatch.setattr("sys.stdin", io.StringIO("not = [toml"))
    rc, out, _ = _main(capsys, "profiles", "save", "-", "--json")
    assert rc == 2 and json.loads(out)["error"]["code"] == "profile_invalid"
    # delete: user ok, idempotent, built-in refused
    rc, out, _ = _main(capsys, "profiles", "delete", "other", "--json")
    assert rc == 0 and json.loads(out) == {"id": "other", "deleted": True}
    rc, out, _ = _main(capsys, "profiles", "delete", "other", "--json")
    assert rc == 0 and json.loads(out)["deleted"] is False
    rc, out, _ = _main(capsys, "profiles", "delete", "mp4-h264-aac", "--json")
    assert rc == 2 and json.loads(out)["error"]["code"] == "profile_invalid"


def test_usage_error_keeps_argparse_message(capsys) -> None:
    rc, out, err = _main(capsys, "deint", "--rate", "triple", "x.avi", "--json")
    e = json.loads(out)["error"]
    assert rc == 2 and e["code"] == "usage" and "invalid choice: 'triple'" in e["message"]
    rc, out, err = _main(capsys, "analyze", "--events")
    assert rc == 2 and "required: input" in json.loads(out.splitlines()[0])["message"]
