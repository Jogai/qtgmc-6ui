"""profiles: built-ins, TOML round trip, user storage (XDG), validation, tool hiding."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from qtgmc_6ui import encoders, paths, profiles
from qtgmc_6ui.errors import ErrorCode, QtgmcError
from qtgmc_6ui.models import Profile, RateControl, ScaleMode, VideoBackend, ViewRateMode

BRIEF_BUILTINS = {"mp4-h264-aac", "mkv-hevc10-opus", "mkv-h264-flac"}


def test_builtin_profiles_from_brief() -> None:
    ps = {p.id: p for p in profiles.builtin_profiles()}
    assert BRIEF_BUILTINS <= set(ps)
    for pid in BRIEF_BUILTINS:
        p = ps[pid]
        assert p.builtin and p.path and p.path.endswith(f"{pid}.toml")
        assert p.video.backend == VideoBackend.FFMPEG and not p.audio.sox.enabled  # ffmpeg only
        assert profiles.required_tools(p) == []
        assert profiles.validate(p, check_tools=False, check_ffmpeg=False) == []
    assert ps["mp4-h264-aac"].container == "mp4" and ps["mp4-h264-aac"].video.encoder == "libx264" and ps["mp4-h264-aac"].audio.encoder == "aac"
    assert ps["mkv-hevc10-opus"].video.pix_fmt == "yuv420p10le" and ps["mkv-hevc10-opus"].audio.encoder == "libopus"
    assert ps["mkv-h264-flac"].audio.encoder == "flac" and ps["mkv-h264-flac"].video.scale.mode == ScaleMode.KEEP
    assert ps["mp4-h264-aac"].video.scale.mode == ScaleMode.SQUARE
    assert ps["mp4-h264-aac"].name == "MP4 – H.264 + AAC (compatible)"


def test_optional_tool_builtins_declare_their_tool() -> None:
    ps = {p.id: p for p in profiles.builtin_profiles()}
    assert profiles.required_tools(ps["mp4-x264cli-aac"]) == ["x264"]
    assert profiles.required_tools(ps["mkv-x265cli10-opus"]) == ["x265"]
    assert profiles.required_tools(ps["mkv-kvazaar-opus"]) == ["kvazaar"]
    assert profiles.required_tools(ps["mp4-h264-aac-sox"]) == ["sox"]
    assert ps["mp4-h264-aac-sox"].video.rate == ViewRateMode.SINGLE
    for p in ps.values():
        assert profiles.validate(p, check_tools=False, check_ffmpeg=False) == [], p.id


def test_toml_round_trip_all_builtins() -> None:
    for p in profiles.builtin_profiles():
        text = profiles.to_toml(p)
        assert "builtin" not in text and "path" not in text.splitlines()[0]
        back = profiles.from_toml(text, path=p.path, builtin=True)
        assert back == p
        assert text.startswith("schema = 1\n")


def test_toml_escaping_and_optional_fields() -> None:
    p = Profile(id="odd-1", name='Quote " and \\ backslash', description="line1\nline2\ttab")
    p.video.extra_args = ["-x264-params", 'ref=4:me="umh"']
    p.audio.quality = 5.5
    p.audio.sox.enabled = True
    p.audio.sox.effects = ["rate", "-v", "48000"]
    back = profiles.from_toml(profiles.to_toml(p))
    assert back == p
    p.audio.quality = None
    text = profiles.to_toml(p)
    assert "quality" not in text.split("[audio]")[1].split("[audio.sox]")[0]
    assert profiles.from_toml(text).audio.quality is None


def test_control_characters_round_trip() -> None:
    p = Profile(id="ctrl", name="win\r\nline", description="bell\x07 del\x7f esc\x1b tab\t")
    text = profiles.to_toml(p)
    assert "\r" not in text and "\x07" not in text and "\\u0007" in text and "\\u007f" in text.lower()
    back = profiles.from_toml(text)
    assert back.name == p.name and back.description == p.description


def test_hostile_values_become_problems_not_crashes() -> None:
    base = 'schema = 1\nid = "h"\nname = "h"\n'
    for body in ('[video]\nquality = "abc"\n', "description = 5\n", '[video]\nextra_args = "-tune film"\n', "[audio.sox]\neffects = [1, 2]\n", "[video]\npreset = true\n", "[video.scale]\nwidth = 1.5\n"):
        with pytest.raises(QtgmcError) as e:
            profiles.from_toml(base + body)
        assert e.value.code == ErrorCode.PROFILE_INVALID and "wrong type" in e.value.message, body
    p = profiles.from_toml(base + "[video]\nbitrate_kbps = 99999999999999999\nquality = 1e9\n[audio]\nsample_rate = 999999999\n")
    problems = profiles.validate(p, check_tools=False, check_ffmpeg=False)
    assert any("bitrate_kbps" in x for x in problems) and any("quality" in x for x in problems) and any("out of range" in x for x in problems)


def test_scale_algorithm_and_dangerous_tokens_rejected() -> None:
    p = Profile(id="inj", name="inj")
    p.video.scale.mode = ScaleMode.SQUARE
    p.video.scale.algorithm = "lanczos,drawbox=c=red"
    assert any("not a scale flag" in x for x in profiles.validate(p, check_tools=False, check_ffmpeg=False))
    p.video.scale.algorithm = "lanczos+accurate_rnd"
    assert profiles.validate(p, check_tools=False, check_ffmpeg=False) == []
    p.video.extra_args = ["-i", "/etc/passwd"]
    p.audio.sox.enabled, p.audio.sox.effects = True, ["noiseprof", "/home/x/prof"]
    problems = profiles.validate(p, check_tools=False, check_ffmpeg=False)
    assert any("video.extra_args" in x and "-i" in x for x in problems) and any("sox.effects" in x and "noiseprof" in x for x in problems)
    p.video.extra_args, p.audio.sox.effects = ["--dump-yuv", "x"], ["gain", "-3"]
    assert any("--dump-yuv" in x for x in profiles.validate(p, check_tools=False, check_ffmpeg=False))


def test_filename_and_id_may_differ(isolated_dirs: dict[str, Path]) -> None:
    d = paths.profiles_dir()
    d.mkdir(parents=True)
    (d / "copy of mine.toml").write_text(profiles.to_toml(Profile(id="mine", name="Mine")), "utf-8")
    assert "mine" in {p.id for p in profiles.list_profiles()}
    assert profiles.load("mine").name == "Mine" and profiles.load("mine").path == str(d / "copy of mine.toml")
    profiles.delete("mine")
    assert not (d / "copy of mine.toml").exists()


def test_delete_and_load_reject_unsafe_ids(isolated_dirs: dict[str, Path]) -> None:
    victim = isolated_dirs["config"] / "victim.toml"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("x", "utf-8")
    for bad in ("../victim", "/etc/passwd", "Mine", "a b"):
        with pytest.raises(QtgmcError) as e:
            profiles.delete(bad)
        assert e.value.code == ErrorCode.PROFILE_NOT_FOUND
        with pytest.raises(QtgmcError):
            profiles.load(bad)
    assert victim.exists()
    # a profile file that lives outside the profiles dir (symlink) is listed but never deleted through the link
    d = paths.profiles_dir()
    d.mkdir(parents=True, exist_ok=True)
    outside = isolated_dirs["config"] / "outside.toml"
    outside.write_text(profiles.to_toml(Profile(id="outside", name="o")), "utf-8")
    (d / "link.toml").symlink_to(outside)
    with pytest.raises(QtgmcError):
        profiles.delete("outside")
    assert outside.exists()


def test_from_toml_rejects_bad_input() -> None:
    with pytest.raises(QtgmcError) as e:
        profiles.from_toml("this is not = toml [")
    assert e.value.code == ErrorCode.PROFILE_INVALID
    with pytest.raises(QtgmcError) as e:
        profiles.from_toml('schema = 1\nid = "x"\n[video]\nbackend = "nope"\n')
    assert e.value.code == ErrorCode.PROFILE_INVALID
    with pytest.raises(QtgmcError) as e:
        profiles.from_toml('schema = 1\nid = "x"\ncontainerr = "mp4"\n')
    assert "unknown key" in e.value.message


def test_user_profile_storage_round_trip(isolated_dirs: dict[str, Path]) -> None:
    assert profiles.user_profiles() == []
    p = profiles.load("mp4-h264-aac")
    p.id, p.name = "my-copy", "My copy"
    p.video.quality = 21.0
    p.video.rate_control = RateControl.CRF
    path = profiles.save(p)
    assert path == paths.profiles_dir() / "my-copy.toml" and path.is_file()
    assert str(path).startswith(str(isolated_dirs["config"]))
    assert not p.builtin and p.path == str(path)
    got = profiles.load("my-copy")
    assert got.video.quality == 21.0 and got.name == "My copy" and not got.builtin
    assert got.path == str(path)
    assert profiles.load(str(path)) == got
    assert any(x.id == "my-copy" for x in profiles.list_profiles())
    profiles.delete("my-copy")
    assert not path.exists()
    with pytest.raises(QtgmcError) as e:
        profiles.load("my-copy")
    assert e.value.code == ErrorCode.PROFILE_NOT_FOUND
    with pytest.raises(QtgmcError) as e:
        profiles.delete("my-copy")
    assert e.value.code == ErrorCode.PROFILE_NOT_FOUND


def test_xdg_config_home_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QTGMC6UI_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert paths.profiles_dir() == tmp_path / "xdg" / "qtgmc-6ui" / "profiles"
    p = Profile(id="xdg-test", name="x")
    path = profiles.save(p)
    assert path == tmp_path / "xdg" / "qtgmc-6ui" / "profiles" / "xdg-test.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert paths.profiles_dir() == tmp_path / "home" / ".config" / "qtgmc-6ui" / "profiles"


def test_user_shadows_builtin_and_builtin_delete_refused(isolated_dirs: dict[str, Path]) -> None:
    p = profiles.load("mkv-h264-flac")
    p.name = "shadowed"
    profiles.save(p)
    assert profiles.load("mkv-h264-flac").name == "shadowed"
    listed = [x for x in profiles.list_profiles(include_unavailable=True) if x.id == "mkv-h264-flac"]
    assert len(listed) == 1 and listed[0].name == "shadowed" and not listed[0].builtin
    profiles.delete("mkv-h264-flac")  # removes the user copy only
    assert profiles.load("mkv-h264-flac").builtin
    with pytest.raises(QtgmcError) as e:
        profiles.delete("mkv-h264-flac")
    assert e.value.code == ErrorCode.PROFILE_INVALID


def test_save_rejects_structurally_invalid(isolated_dirs: dict[str, Path]) -> None:
    for bad in (Profile(id="Bad_ID", name="x"), Profile(id="ok", name=""), Profile(id="ok", name="x", extension=".mp4")):
        with pytest.raises(QtgmcError) as e:
            profiles.save(bad)
        assert e.value.code == ErrorCode.PROFILE_INVALID
    assert not list(paths.profiles_dir().glob("*.toml")) if paths.profiles_dir().exists() else True


def test_structural_validation_messages() -> None:
    p = Profile(id="t", name="t")
    p.video.rate_control = RateControl.BITRATE
    p.video.scale.mode = ScaleMode.CUSTOM
    p.audio.sox.enabled = True
    problems = profiles.validate(p, check_tools=False, check_ffmpeg=False)
    assert any("bitrate_kbps" in x for x in problems)
    assert any("custom" in x for x in problems)
    assert any("sox.effects" in x for x in problems)
    k = Profile(id="k", name="k", video=p.video.__class__(backend=VideoBackend.KVAZAAR_CLI, encoder="kvazaar", rate_control=RateControl.CRF, tune="grain", level="4.1"))
    problems = profiles.validate(k, check_tools=False, check_ffmpeg=False)
    assert any("kvazaar supports qp" in x for x in problems) and any("no tune" in x for x in problems)
    assert not any("level" in x for x in problems)  # kvazaar has --level
    x5 = Profile(id="x5", name="x5", video=p.video.__class__(backend=VideoBackend.X265_CLI, encoder="x265", profile="main", pix_fmt="yuv420p10le"))
    assert any("does not allow 10-bit" in m for m in profiles.validate(x5, check_tools=False, check_ffmpeg=False))
    x = Profile(id="x", name="x", video=p.video.__class__(backend=VideoBackend.X264_CLI, encoder="x264", preset="nope"))
    assert any("preset 'nope'" in m for m in profiles.validate(x, check_tools=False, check_ffmpeg=False))


def test_tool_profiles_hidden_without_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for t in ("ffmpeg", "ffprobe"):
        real = shutil.which(t)
        if real:
            os.symlink(real, bindir / t)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("QTGMC6UI_CONFIG_DIR", str(tmp_path / "cfg"))
    assert not encoders.tool_available("x264") and not encoders.tool_available("sox")
    visible = {p.id for p in profiles.list_profiles()}
    assert BRIEF_BUILTINS <= visible
    assert not ({"mp4-x264cli-aac", "mkv-x265cli10-opus", "mkv-kvazaar-opus", "mp4-h264-aac-sox"} & visible)
    everything = {p.id for p in profiles.list_profiles(include_unavailable=True)}
    assert "mp4-x264cli-aac" in everything
    p = profiles.load("mp4-x264cli-aac")  # loading by id still works, validation names the tool
    assert not profiles.is_available(p)
    problems = profiles.validate(p, check_tools=True, check_ffmpeg=False)
    assert any("'x264' is not installed" in x for x in problems)
    assert profiles.validate(p, check_tools=False, check_ffmpeg=False) == []


@pytest.mark.needs_sandbox
def test_validate_against_ffmpeg(isolated_dirs: dict[str, Path]) -> None:
    for p in profiles.builtin_profiles():
        if profiles.is_available(p):
            assert profiles.validate(p) == [], p.id
    bad = profiles.load("mkv-h264-flac")
    bad.id, bad.container, bad.extension = "mov-flac", "mov", "mov"
    problems = profiles.validate(bad)
    assert len(problems) == 1 and problems[0].startswith("mov does not accept flac")
    bad.audio.encoder = "copy"
    assert any("master's FLAC" in x for x in profiles.validate(bad))
    bad.audio.encoder = "aac"
    assert profiles.validate(bad) == []
    bad.video.pix_fmt = "rgb24"
    assert any("pix_fmt 'rgb24'" in x for x in profiles.validate(bad))
    bad.video.pix_fmt = ""
    bad.video.encoder = "nonexistent"
    assert any("not provided by this ffmpeg" in x for x in profiles.validate(bad))
    bad.video.encoder = "libx264"
    bad.container = "webm"
    assert any("webm does not accept" in x for x in profiles.validate(bad))
