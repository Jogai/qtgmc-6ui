"""jobs: JobSpec from flags / JSON, output planning, path validation, blocking rules."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from qtgmc_6ui import cli, jobs
from qtgmc_6ui.errors import BlockedError, ErrorCode, ExitCode, QtgmcError, UsageError
from qtgmc_6ui.models import (
    AnalysisResult,
    Classification,
    ContentClass,
    FieldOrder,
    FieldOrderChoice,
    JobSpec,
    MasterSettings,
    Preset,
    Profile,
    RateMode,
    Recommendation,
    RecommendedAction,
    ViewCopySpec,
)


def _args(*argv: str):
    return cli.build_parser().parse_args(["deint", *argv])


def test_job_from_args_defaults(tmp_path: Path) -> None:
    inp = tmp_path / "clip.avi"
    j = jobs.job_from_args(_args(str(inp)))
    assert j.input == str(inp) and j.output_dir == str(tmp_path)
    assert j.master == MasterSettings()
    assert j.views == [] and not j.force and not j.overwrite and j.master_output is None


def test_job_from_args_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    j = jobs.job_from_args(
        _args("in.avi", "-o", "out/m.mkv", "--field-order", "tff", "--rate", "single", "--preset", "fast",
              "--no-lossless", "--no-source-match", "--denoise", "2.5", "--chroma", "422", "--view", "a",
              "--view", "b.toml", "--force", "--overwrite", "--threads", "4", "--cache-mb", "1000")
    )
    assert j.input == str(tmp_path / "in.avi")
    assert j.master_output == str(tmp_path / "out" / "m.mkv") and j.output_dir == str(tmp_path / "out")
    m = j.master
    assert (m.field_order, m.rate, m.preset) == (FieldOrderChoice.TFF, RateMode.SINGLE, Preset.FAST)
    assert not m.lossless and not m.source_match and m.denoise_sigma == 2.5 and m.chroma.value == "422"
    assert (m.threads, m.cache_mb) == (4, 1000)
    assert [v.profile for v in j.views] == ["a", "b.toml"] and j.force and j.overwrite
    with pytest.raises(UsageError):
        jobs.job_from_args(_args("in.avi", "--denoise", "-1"))


def test_dump_load_roundtrip(tmp_path: Path) -> None:
    j = JobSpec(input=str(tmp_path / "a.avi"), output_dir=str(tmp_path), views=[ViewCopySpec("mp4-h264-aac")])
    j.master.preset = Preset.SLOW
    p = tmp_path / "job.json"
    p.write_text(jobs.dump_job(j))
    assert jobs.load_job(str(p)) == j


def test_load_job_relative_paths_and_errors(tmp_path: Path) -> None:
    sub = tmp_path / "jobs"
    sub.mkdir()
    p = sub / "j.json"
    p.write_text(json.dumps({"schema": 1, "input": "../in.avi", "master_output": "~/x.mkv", "views": [{"profile": "my.toml"}]}))
    j = jobs.load_job(str(p))
    assert j.input == str(tmp_path / "in.avi") and j.output_dir == str(tmp_path)
    assert j.master_output == os.path.expanduser("~/x.mkv")
    assert j.views[0].profile == str(sub / "my.toml")
    for bad in ('{"schema": 99, "input": "x"}', '{"schema": 1}', "[1]", "{nope", '{"input": "x", "master": {"rate": "triple"}}'):
        p.write_text(bad)
        with pytest.raises(UsageError):
            jobs.load_job(str(p))
    with pytest.raises(UsageError):
        jobs.load_job(str(tmp_path / "missing.json"))


def test_read_json_document_leaves_rest_unread() -> None:
    s = io.StringIO('{"schema": 1,\n "input": "/x.avi"}\ncancel\n')
    assert jobs.read_json_document(s)["input"] == "/x.avi"
    assert s.readline() == "cancel\n"
    with pytest.raises(UsageError):
        jobs.read_json_document(io.StringIO(""))
    with pytest.raises(UsageError):
        jobs.read_json_document(io.StringIO("{broken\n"))


def test_load_job_from_stdin(tmp_path: Path) -> None:
    j = jobs.load_job("-", io.StringIO(json.dumps({"input": str(tmp_path / "a.avi")}) + "\n"))
    assert j.input == str(tmp_path / "a.avi")


def test_plan_outputs_naming(tmp_path: Path) -> None:
    j = JobSpec(input=str(tmp_path / "my clip.avi"), output_dir=str(tmp_path / "o"), views=[ViewCopySpec("p1"), ViewCopySpec("p2", "/z/v.mp4")])
    profs = [Profile(id="p1", extension="mp4"), Profile(id="p2", extension="mkv")]
    outs = jobs.plan_outputs(j, profs)
    assert outs == [str(tmp_path / "o" / "my clip.deint.mkv"), str(tmp_path / "o" / "my clip.deint.p1.mp4"), "/z/v.mp4"]
    assert j.master_output == outs[0] and j.views[0].output == outs[1]
    j2 = JobSpec(input="/a/b.avi", output_dir="/o", name_template="{stem}_x")
    assert jobs.plan_outputs(j2, []) == ["/o/b_x.mkv"]
    for bad in ("{nope}", "a/{stem}", "{}"):
        with pytest.raises(UsageError):
            jobs.master_path(JobSpec(input="/a/b.avi", output_dir="/o", name_template=bad))


def test_path_problems(tmp_path: Path) -> None:
    inp = tmp_path / "in.avi"
    inp.write_bytes(b"orig")
    j = JobSpec(input=str(inp), output_dir=str(tmp_path))
    assert jobs.job_problems(j, [str(tmp_path / "m.mkv")]) == []
    # output == input (also via another spelling / symlink), even with --overwrite
    link = tmp_path / "link.avi"
    link.symlink_to(inp)
    for out in (str(inp), str(tmp_path / "." / "in.avi"), str(link)):
        j.overwrite = True
        probs = jobs.job_problems(j, [out])
        assert probs and probs[0].code == ErrorCode.USAGE and "overwrite the input" in probs[0].message
    j.overwrite = False
    existing = tmp_path / "m.mkv"
    existing.write_bytes(b"x")
    probs = jobs.job_problems(j, [str(existing)])
    assert probs[0].code == ErrorCode.OUTPUT_EXISTS and probs[0].exit_code == ExitCode.OUTPUT
    j.overwrite = True
    assert jobs.job_problems(j, [str(existing)]) == []
    # duplicate outputs, missing input, unwritable dir
    probs = jobs.job_problems(j, [str(tmp_path / "a.mkv"), str(tmp_path / "a.mkv")])
    assert any("share one path" in p.message for p in probs)
    j.input = str(tmp_path / "missing.avi")
    assert jobs.job_problems(j, [str(tmp_path / "q.mkv")])[0].code == ErrorCode.INPUT_NOT_FOUND
    j.input = str(inp)
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        if not os.access(ro, os.W_OK):
            assert jobs.job_problems(j, [str(ro / "x.mkv")])[0].code == ErrorCode.OUTPUT_NOT_WRITABLE
    finally:
        ro.chmod(0o700)
    # creatable nested output dir is fine
    assert jobs.job_problems(j, [str(tmp_path / "new" / "deep" / "x.mkv")]) == []


def test_check_job_raises_first_with_all_problems(tmp_path: Path) -> None:
    j = JobSpec(input=str(tmp_path / "missing.avi"), output_dir=str(tmp_path))
    (tmp_path / "m.mkv").write_bytes(b"")
    with pytest.raises(QtgmcError) as ei:
        jobs.check_job(j, [str(tmp_path / "m.mkv")])
    assert ei.value.code == ErrorCode.INPUT_NOT_FOUND and len(ei.value.details["problems"]) == 2


def test_validate_job_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qtgmc_6ui import profiles

    def fake_load(pid: str) -> Profile:
        if pid == "nope":
            raise QtgmcError(ErrorCode.PROFILE_NOT_FOUND, "profile not found: nope")
        return Profile(id=pid, extension="mp4")

    monkeypatch.setattr(profiles, "load", fake_load)
    inp = tmp_path / "in.avi"
    inp.write_bytes(b"x")
    j = JobSpec(input=str(inp), output_dir=str(tmp_path), views=[ViewCopySpec("ok")])
    assert jobs.validate_job(j) == []
    (tmp_path / "in.deint.ok.mp4").write_bytes(b"")
    assert any("exists" in ln for ln in jobs.validate_job(j))
    j.views = [ViewCopySpec("nope")]
    assert jobs.validate_job(j) == ["profile not found: nope"]


def _analysis(content: ContentClass, fo: FieldOrder | None, action: RecommendedAction, force_allowed: bool = True) -> AnalysisResult:
    return AnalysisResult(
        classification=Classification(content, fo, 0.9),
        recommendation=Recommendation(action, f"{content} explanation", force_allowed),
    )


@pytest.mark.parametrize(
    "content,fo,action,code",
    [
        (ContentClass.TELECINED, FieldOrder.TFF, RecommendedAction.BLOCK_TELECINED, ErrorCode.BLOCKED_TELECINED),
        (ContentClass.MIXED, FieldOrder.TFF, RecommendedAction.BLOCK_MIXED, ErrorCode.BLOCKED_MIXED),
        (ContentClass.PROGRESSIVE, None, RecommendedAction.SKIP_PROGRESSIVE, ErrorCode.BLOCKED_PROGRESSIVE),
    ],
)
def test_blocked_unless_forced(content, fo, action, code) -> None:
    a = _analysis(content, fo, action)
    s = MasterSettings()
    with pytest.raises(BlockedError) as ei:
        jobs.check_blocked(a, s, force=False)
    assert ei.value.code == code and ei.value.exit_code == ExitCode.BLOCKED
    assert "--force" in ei.value.hint
    if code != ErrorCode.BLOCKED_PROGRESSIVE:
        assert "IVTC" in ei.value.hint
    assert ei.value.message == f"{content} explanation"
    if fo is None:
        # forced, but nothing tells us the field order: still refused
        with pytest.raises(BlockedError) as ei:
            jobs.check_blocked(a, s, force=True)
        assert ei.value.code == ErrorCode.BLOCKED_UNDETERMINED
        s = MasterSettings(field_order=FieldOrderChoice.BFF)
    warns = jobs.check_blocked(a, s, force=True)
    assert warns and warns[0].startswith("--force")


def test_force_not_allowed() -> None:
    a = _analysis(ContentClass.TELECINED, FieldOrder.TFF, RecommendedAction.BLOCK_TELECINED, force_allowed=False)
    with pytest.raises(BlockedError):
        jobs.check_blocked(a, MasterSettings(), force=True)


def test_undetermined_needs_explicit_field_order() -> None:
    a = _analysis(ContentClass.UNDETERMINED, None, RecommendedAction.CONFIRM_FIELD_ORDER)
    for force in (False, True):
        with pytest.raises(BlockedError) as ei:
            jobs.check_blocked(a, MasterSettings(), force=force)
        assert ei.value.code == ErrorCode.BLOCKED_UNDETERMINED and "--field-order" in ei.value.hint
    assert jobs.check_blocked(a, MasterSettings(field_order=FieldOrderChoice.TFF), force=False) == []


def test_interlaced_and_override_warning() -> None:
    a = _analysis(ContentClass.INTERLACED_BFF, FieldOrder.BFF, RecommendedAction.DEINTERLACE)
    assert jobs.check_blocked(a, MasterSettings(), force=False) == []
    w = jobs.check_blocked(a, MasterSettings(field_order=FieldOrderChoice.TFF), force=False)
    assert len(w) == 1 and "override" in w[0]
    assert jobs.effective_field_order(a, MasterSettings()) == FieldOrder.BFF


def test_no_master_rules_and_plan(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as ei:
        jobs.job_from_args(_args(str(tmp_path / "a.avi"), "--no-master"))
    assert ei.value.details["reason"] == "no_master_without_view"
    with pytest.raises(UsageError):
        jobs.job_from_args(_args(str(tmp_path / "a.avi"), "--no-master", "--view", "x", "-o", "m.mkv"))
    j = jobs.job_from_args(_args(str(tmp_path / "a.avi"), "--no-master", "--view", "x"))
    assert j.keep_master is False
    outs = jobs.plan_outputs(j, [Profile(id="x", extension="mp4")])
    assert outs == [str(tmp_path / "a.deint.x.mp4")] and j.master_output is None
    t = jobs.temp_master_path(j)
    assert t == str(tmp_path / f".a.deint.nomaster.{os.getpid()}.tmp.mkv")
    j2 = JobSpec(input=str(tmp_path / "a.avi"), output_dir=str(tmp_path), keep_master=False)
    assert any("viewing copy" in p.message for p in jobs.job_problems(j2, []))


def test_remove_stale_temp_masters(tmp_path: Path) -> None:
    import subprocess
    import sys

    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True).stdout.strip()
    stale = [tmp_path / f".x.deint.nomaster.{dead}.tmp.mkv", tmp_path / f"..x.deint.nomaster.{dead}.tmp.mkv.partial"]
    live = [tmp_path / f".x.deint.nomaster.{os.getpid()}.tmp.mkv", tmp_path / f".y.nomaster.{os.getppid()}.tmp.mkv"]
    other = [tmp_path / "x.deint.mkv", tmp_path / ".x.deint.mkv.partial"]
    for f in stale + live + other:
        f.write_bytes(b"x")
    assert sorted(jobs.remove_stale_temp_masters(str(tmp_path))) == sorted(map(str, stale))
    assert all(f.exists() for f in live + other)


def test_estimate_and_disk_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    a = AnalysisResult()
    a.probe.video.width, a.probe.video.height, a.probe.video.nb_frames, a.probe.video.pix_fmt = 720, 576, 274, "yuv420p"
    est = jobs.estimate_master_bytes(a, MasterSettings())
    assert est == int(548 * 720 * 576 * 1.5 * 0.5)  # reference master measures 121 MB
    j = JobSpec(input=str(tmp_path / "a.avi"), output_dir=str(tmp_path / "new"), keep_master=False)
    assert jobs.disk_space_warning(j, a) == ""
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(1, 1, 1000))
    w = jobs.disk_space_warning(j, a)
    assert "temporary master" in w and "GB" in w
