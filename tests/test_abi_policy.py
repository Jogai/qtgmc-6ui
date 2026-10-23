"""ABI policy: what blocks (python minor, API major, lower API minor) and what only warns
(a VapourSynth release change with the same API), in check_abi, doctor and the CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from qtgmc_6ui import buildinfo, doctor, vsenv
from qtgmc_6ui.errors import ComponentError, ErrorCode
from qtgmc_6ui.events import Event
from qtgmc_6ui.models import BuildInfo, Status

PY = list(sys.version_info[:2])


def _bi(core: int = 80, api=(4, 3), py=None) -> BuildInfo:
    return BuildInfo(python_minor=py or PY, vapoursynth_core=core, vapoursynth_api=list(api), python_version=".".join(map(str, PY)))


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch):
    """Pretend the running VapourSynth is (release, api) without touching the real module."""
    def set_(release: int, api=(4, 3), clear: bool = True) -> None:
        monkeypatch.setattr(vsenv, "_running_vs", lambda: (release, list(api)))
        if clear:
            vsenv._abi_notice_cache.clear()
    yield set_
    vsenv._abi_notice_cache.clear()


@pytest.mark.parametrize("built,run,blocks,warns", [
    ((80, (4, 3)), (80, (4, 3)), False, False),  # same release
    ((80, (4, 3)), (81, (4, 3)), False, True),   # R80 -> R81, API unchanged: warning only
    ((80, (4, 3)), (79, (4, 3)), False, True),   # downgrade with the same API: warning only
    ((80, (4, 3)), (82, (4, 4)), False, True),   # newer release, higher API minor: still loads
    ((80, (4, 3)), (79, (4, 2)), True, False),   # API minor lower than the build's: block
    ((80, (4, 3)), (90, (5, 0)), True, False),   # API major differs: block
])
def test_check_abi_table(running, built, run, blocks, warns) -> None:
    running(run[0], run[1])
    bi = _bi(built[0], built[1])
    problems = vsenv.check_abi(bi)
    notices = vsenv.abi_warnings(bi)
    assert bool(problems) == blocks and bool(notices) == warns, (problems, notices)
    if blocks:
        assert "Arch updated vapoursynth" in problems[0] and "reinstall/rebuild" in problems[0]
    if warns:
        assert f"VapourSynth is R{run[0]}" in notices[0] and "built against R80" in notices[0]
        assert "compatible" in notices[0] and "rebuild is not required" in notices[0] and "reinstall" not in notices[0]


def test_check_abi_python_minor_blocks(running) -> None:
    running(80)
    problems = vsenv.check_abi(_bi(py=[3, 9]))
    assert len(problems) == 1 and "Arch updated python" in problems[0] and "3.9" in problems[0]
    assert vsenv.abi_warnings(_bi(py=[3, 9])) == []  # a blocked build gets no notice
    assert vsenv.check_abi(BuildInfo()) == [] and vsenv.abi_warnings(BuildInfo()) == []  # unknown fields are not compared


def test_abi_notice_memoised(running) -> None:
    running(81)
    n = vsenv.abi_notice(_bi(80))
    assert n is not None and "R81" in n[0] and "reinstall" not in n[0] + n[1]
    running(80, clear=False)  # memoised for the process: the first answer stays
    assert vsenv.abi_notice(_bi(80)) == n
    vsenv._abi_notice_cache.clear()
    assert vsenv.abi_notice(_bi(80)) is None


@pytest.mark.needs_sandbox
def test_doctor_release_change_is_a_warning(running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "bi.json"
    p.write_text(json.dumps(_bi(80).to_dict()))
    monkeypatch.setenv("QTGMC6UI_BUILD_INFO", str(p))
    monkeypatch.setenv("QTGMC6UI_PLUGIN_DIR", str(tmp_path))
    running(81)
    rep = doctor.run(load_plugins=False, smoke=False)
    c = next(c for c in rep.checks if c.id == "vapoursynth_abi")
    assert c.status == Status.WARN and "VapourSynth is R81" in c.detail and "a rebuild is not required" in c.detail and c.hint == ""
    assert rep.ok, rep.summary  # WARN never fails the report
    assert "1 warning(s)" in rep.summary
    text = doctor.render_text(rep)
    assert "[WARN] vapoursynth_abi" in text and "OK:" in text and "reinstall/rebuild qtgmc-6ui" not in text.split("vapoursynth_abi")[1].split("\n")[0]
    d = json.loads(json.dumps(rep.to_dict()))
    assert d["ok"] is True and next(x for x in d["checks"] if x["id"] == "vapoursynth_abi")["status"] == "warn"
    # the blocking cases stay FAIL
    running(79, (4, 2))
    rep = doctor.run(load_plugins=False, smoke=False)
    c = next(c for c in rep.checks if c.id == "vapoursynth_abi")
    assert c.status == Status.FAIL and "reinstall/rebuild" in c.detail and not rep.ok


@pytest.mark.needs_sandbox
def test_open_core_proceeds_with_notice(running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QTGMC6UI_PLUGIN_DIR", str(tmp_path))
    running(81)
    vc = vsenv.open_core(build_info=_bi(80))
    try:
        assert vc.abi_warnings and "R81" in vc.abi_warnings[0]
    finally:
        vc.close()
    running(79, (4, 2))
    with pytest.raises(ComponentError) as ei:
        vsenv.open_core(build_info=_bi(80))
    assert ei.value.code == ErrorCode.VS_VERSION_MISMATCH and "reinstall/rebuild" in ei.value.message
    assert vsenv.current() is None


@pytest.mark.needs_qtgmc
def test_deint_emits_one_vapoursynth_updated_warning(running, reference_clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    from qtgmc_6ui import cli, commands, paths

    bi = buildinfo.dev_build_info(paths.plugin_dir())  # the real plugin table of this checkout ...
    bi.vapoursynth_core = 80  # ... recorded as built against R80
    p = tmp_path / "bi.json"
    p.write_text(json.dumps(bi.to_dict()))
    monkeypatch.setenv("QTGMC6UI_BUILD_INFO", str(p))
    monkeypatch.setenv("QTGMC6UI_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("QTGMC6UI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(commands._NoticeEmitter, "_notified", False)
    running(81)  # the running core pretends to be R81
    out = tmp_path / "ref.deint.mkv"
    rc = cli.main(["deint", str(reference_clip), "-o", str(out), "--preset", "draft", "--field-order", "bff", "--events"])
    lines = capsys.readouterr().out.splitlines()
    events = [e for e in (Event.from_json(ln) for ln in lines) if e is not None]
    assert rc == 0 and events[-1].ev == "done" and events[-1].data["exit_code"] == 0
    assert out.is_file()
    updated = [e for e in events if e.ev == "warning" and e.data["code"] == "vapoursynth_updated"]
    assert len(updated) == 1 and "R81" in updated[0].data["message"] and "reinstall" not in updated[0].data["message"]
    assert events.index(updated[0]) == 1 and events[0].ev == "start"  # right after start, before any stage
    assert not [e for e in events if e.ev == "error"]
