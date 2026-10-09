"""Scaffold sanity: every module imports, contracts round-trip, CLI parses."""

from __future__ import annotations

import importlib
import json
from fractions import Fraction

import pytest

from qtgmc_6ui import cli, events, models
from qtgmc_6ui.errors import ErrorCode, ExitCode, QtgmcError

MODULES = [
    "qtgmc_6ui.paths", "qtgmc_6ui.errors", "qtgmc_6ui.models", "qtgmc_6ui.events",
    "qtgmc_6ui.ffprobe", "qtgmc_6ui.idet", "qtgmc_6ui.telecine", "qtgmc_6ui.classify",
    "qtgmc_6ui.colorimetry", "qtgmc_6ui.analysis", "qtgmc_6ui.vsenv", "qtgmc_6ui.buildinfo",
    "qtgmc_6ui.qtgmc", "qtgmc_6ui.graph", "qtgmc_6ui.master", "qtgmc_6ui.pipeline",
    "qtgmc_6ui.doctor", "qtgmc_6ui.runtime", "qtgmc_6ui.encoders", "qtgmc_6ui.muxmatrix",
    "qtgmc_6ui.profiles", "qtgmc_6ui.viewcopy", "qtgmc_6ui.jobs", "qtgmc_6ui.preview",
    "qtgmc_6ui.cli", "qtgmc_6ui.commands", "qtgmc_6ui.gui", "qtgmc_6ui.gui.app",
]


@pytest.mark.parametrize("name", MODULES)
def test_imports(name: str) -> None:
    importlib.import_module(name)


def test_analysis_roundtrip() -> None:
    a = models.AnalysisResult(input="x.avi")
    a.probe.video.fps = Fraction(30000, 1001)
    a.probe.video.sar = Fraction(16, 15)
    a.windows.append(models.IdetWindow(frames=300, multi=models.IdetCounts(bff=290, undetermined=10)))
    a.classification = models.Classification(models.ContentClass.INTERLACED_BFF, models.FieldOrder.BFF, 0.97)
    d = json.loads(json.dumps(a.to_dict()))
    assert d["probe"]["video"]["fps"] == "30000/1001"
    assert d["classification"]["content"] == "interlaced_bff"
    b = models.AnalysisResult.from_dict(d)
    assert b == a


def test_job_roundtrip() -> None:
    j = models.JobSpec(input="in.avi", views=[models.ViewCopySpec("mp4-h264-aac")])
    j.master.preset = models.Preset.FAST
    d = json.loads(json.dumps(j.to_dict()))
    assert models.JobSpec.from_dict(d) == j
    p = models.Profile(id="x", video=models.VideoSettings(pix_fmt="yuv420p10le"))
    assert models.Profile.from_dict(p.to_dict()) == p


def test_events_roundtrip() -> None:
    ev = events.Event("progress", 1.5, {"stage": "master", "done": 10, "total": 548})
    back = events.Event.from_json(ev.to_json())
    assert back == ev
    assert events.Event.from_json("not json") is None
    assert events.Event.from_json('{"x": 1}') is None


def test_error_exit_codes() -> None:
    e = QtgmcError(ErrorCode.BLOCKED_TELECINED, "telecined", "use --force")
    assert e.exit_code == ExitCode.BLOCKED
    assert e.to_dict()["exit_code"] == 3
    assert QtgmcError(ErrorCode.INTERNAL, "x").exit_code == ExitCode.ERROR


def test_cli_parses_contract() -> None:
    p = cli.build_parser()
    a = p.parse_args(["deint", "in.avi", "--rate", "single", "--view", "a", "--view", "b", "--events"])
    assert a.command == "deint" and a.view == ["a", "b"] and a.events
    a = p.parse_args(["preview-frame", "in.avi", "42", "--mode", "fields", "-o", "-"])
    assert a.frame == 42 and a.mode == "fields"
    assert cli.main(["version"]) == 0
