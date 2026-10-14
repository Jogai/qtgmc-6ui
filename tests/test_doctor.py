"""doctor: report content, JSON round trip, text rendering, startup subset."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from qtgmc_6ui import doctor, vsenv
from qtgmc_6ui.models import BuildInfo, DoctorReport, Status


def _check(rep: DoctorReport, cid: str):
    return next(c for c in rep.checks if c.id == cid)


@pytest.mark.needs_sandbox
def test_doctor_no_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QTGMC6UI_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setenv("QTGMC6UI_BUILD_INFO", str(tmp_path / "absent.json"))
    rep = doctor.run(load_plugins=False, smoke=False)
    assert _check(rep, "build_info").status == Status.WARN  # dev mode
    assert _check(rep, "python_abi").status == Status.OK and _check(rep, "vapoursynth_abi").status == Status.OK
    assert _check(rep, "vapoursynth_core").status == Status.SKIP
    assert _check(rep, "tool:ffmpeg").status == Status.OK and _check(rep, "tool:ffprobe").status == Status.OK
    assert _check(rep, "pypkg:vsjetpack").detail == "2.2.6"
    assert _check(rep, "locale").status == Status.OK
    assert rep.ok and rep.runtime["vapoursynth"] == "R80"
    assert rep.environment["filesystem_encoding"].lower().replace("-", "") == "utf8"
    text = doctor.render_text(rep)
    assert "qtgmc-6UI doctor" in text and "[ ok ] tool:ffmpeg" in text and "OK: all checks passed" in text
    d = json.loads(json.dumps(rep.to_dict()))
    assert DoctorReport.from_dict(d).checks == rep.checks and d["schema"] == 1


@pytest.mark.needs_sandbox
def test_doctor_abi_mismatch_and_missing_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bi = BuildInfo(pkgver="0.1.0", pkgrel="1", python_version="3.9.0", python_minor=[3, 9], vapoursynth_core=80,
                   vapoursynth_api=[4, 3], python_packages={"vsjetpack": "9.9.9"})
    bi.plugins = [{"name": "akarin", "namespace": "akarin", "identifier": "", "version": "v1.5.0", "file": "libakarin.so",
                   "required": True, "origin": "bundled"}]  # type: ignore[list-item]
    p = tmp_path / "bi.json"
    p.write_text(json.dumps(bi.to_dict()))
    monkeypatch.setenv("QTGMC6UI_BUILD_INFO", str(p))
    monkeypatch.setenv("QTGMC6UI_PLUGIN_DIR", str(tmp_path))
    rep = doctor.run(load_plugins=True, smoke=True)
    assert not rep.ok
    assert _check(rep, "python_abi").status == Status.FAIL and "Arch updated python" in _check(rep, "python_abi").detail
    assert _check(rep, "vapoursynth_core").status == Status.FAIL and "akarin" in _check(rep, "vapoursynth_core").detail
    assert any(c.id == "pypkg" and "9.9.9" in c.detail for c in rep.checks)
    assert "python_abi" in rep.summary and "vapoursynth_core" in rep.summary
    text = doctor.render_text(rep)
    assert "[FAIL] python_abi" in text and "hint:" in text
    assert vsenv.current() is None
    problems = doctor.startup_check()
    assert any("Arch updated python" in s for s in problems) and any("akarin" in s for s in problems)


@pytest.mark.needs_sandbox
def test_startup_check_dev_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QTGMC6UI_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setenv("QTGMC6UI_BUILD_INFO", str(tmp_path / "absent.json"))
    assert doctor.startup_check() == []
    monkeypatch.delenv("QTGMC6UI_PLUGIN_DIR")
    assert any("build-info" in s for s in doctor.startup_check())


@pytest.mark.needs_qtgmc
def test_doctor_full_stack_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QTGMC6UI_BUILD_INFO", str(tmp_path / "absent.json"))
    rep = doctor.run(load_plugins=True, smoke=True)
    text = doctor.render_text(rep)
    assert rep.ok, text
    for name in ("bestsource", "akarin", "resize2", "mvutensils", "znedi3", "zsmooth"):
        c = _check(rep, f"plugin:{name}")
        assert c.status == Status.OK, (name, c.detail)
    loaded = {p["name"]: p for p in rep.plugins}
    assert loaded["mvutensils"]["namespace"] == "mvu" and loaded["mvutensils"]["identifier"] == "com.vapoursynth.mvutensils"
    assert loaded["akarin"]["identifier"] == "info.akarin.vsplugin" and loaded["bestsource"]["origin"] == "repo"
    assert _check(rep, "qtgmc_smoke").status == Status.OK and "bobbed" in _check(rep, "qtgmc_smoke").detail
    assert "plugin_extra" not in {c.id for c in rep.checks}
    assert vsenv.current() is None
    d = json.loads(json.dumps(rep.to_dict()))
    assert d["ok"] is True and d["runtime"]["python"] == ".".join(map(str, sys.version_info[:3]))
    assert os.environ.get("QTGMC6UI_PLUGIN_DIR", "") in text


def test_doctor_reports_a_broken_encoders_module(monkeypatch) -> None:
    """No fallback tool detection: a failing encoders import becomes one FAIL check."""
    import builtins

    real_import = builtins.__import__

    def broken(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "encoders" and level == 1 or name == "qtgmc_6ui.encoders":
            raise ImportError("simulated: encoders is broken")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", broken)
    rep = doctor.run(load_plugins=False, smoke=False)
    tools = [c for c in rep.checks if c.id == "tools" or c.id.startswith("tool:")]
    assert [c.id for c in tools] == ["tools"] and tools[0].status.value == "fail"
    assert "simulated: encoders is broken" in tools[0].detail and "reinstall" in tools[0].hint
    assert rep.ok is False and rep.tools == []
