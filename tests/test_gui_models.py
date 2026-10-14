"""GUI non-widget logic: event stream -> RunState, line splitting, queue model, CLI result parsing."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6")

from qtgmc_6ui import models as m  # noqa: E402
from qtgmc_6ui.events import Emitter, Event  # noqa: E402
from qtgmc_6ui.gui.backend import LineBuffer, _parse_cli_result, process_environment  # noqa: E402
from qtgmc_6ui.gui.queue_model import JobItem, JobStatus, QueueModel  # noqa: E402
from qtgmc_6ui.gui.run_model import RunState, format_duration  # noqa: E402


class ListSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        # round-trip through the wire format, exactly as the GUI receives it
        self.events.append(Event.from_json(event.to_json()))


def job_events(cancel: bool = False) -> list[Event]:
    sink = ListSink()
    em = Emitter(sink)
    em.start("run-job", "/v/clip ü.avi", ["/v/clip ü.deint.mkv"])
    em.stage("setup", 1, 3)
    em.stage("master", 2, 3)
    em.progress("master", 274, 548, 50.0, 5.48, 5.5)
    em.log("ffmpeg", "frame=274 fps=50")
    em.warning("denoise_on", "denoise alters the image", "turn it off")
    if cancel:
        em.cancelled(["/v/clip ü.deint.mkv"], [])
        em.done(130)
    else:
        em.stage("view", 3, 3, "mp4-h264-aac")
        em.progress("view", 548, 548, 120.0, 0.0, 10.0)
        em.result(m.JobResult(input="/v/clip ü.avi", outputs=[m.OutputFile("master", "/v/clip ü.deint.mkv")]).to_dict())
        em.done(0)
    return sink.events


def test_runstate_success_stream() -> None:
    st = RunState()
    lines = [st.apply(ev) for ev in job_events()]
    assert st.finished and st.exit_code == 0 and not st.cancelled
    assert st.planned_outputs == ["/v/clip ü.deint.mkv"]
    assert st.stage == "view" and st.stage_text().startswith("Viewing copy: mp4-h264-aac")
    assert st.result and st.result["outputs"][0]["kind"] == "master"
    assert st.warnings[0]["code"] == "denoise_on"
    assert any(line and line.startswith("[ffmpeg]") for line in lines)
    assert any(line and "WARNING [denoise_on]" in line for line in lines)


def test_runstate_progress_fractions() -> None:
    st = RunState()
    for ev in job_events()[:4]:  # start, setup, master, progress 274/548
        st.apply(ev)
    assert st.fraction == pytest.approx(0.5)
    assert st.overall_fraction == pytest.approx((1 + 0.5) / 3)
    assert st.frames_text() == "274 / 548 frames"
    assert st.fps == 50.0 and st.eta_s == pytest.approx(5.48)


def test_runstate_cancel_and_error() -> None:
    st = RunState()
    for ev in job_events(cancel=True):
        st.apply(ev)
    assert st.cancelled and st.exit_code == 130 and st.removed == ["/v/clip ü.deint.mkv"]

    st = RunState()
    st.apply(Event("error", 0, {"code": "encode_failed", "message": "ffmpeg failed", "hint": "see log", "exit_code": 6}))
    st.apply(Event("done", 0, {"exit_code": 6}))
    assert st.error["code"] == "encode_failed" and st.exit_code == 6 and not st.cancelled


def test_runstate_crash_without_done() -> None:
    st = RunState()
    st.apply(Event("stage", 0, {"name": "master", "index": 1, "total": 1}))
    st.finish_without_done(1, "", ["Traceback (most recent call last):", "ImportError: vapoursynth"])
    assert st.error and st.error["code"] == "backend_failed"
    assert "ImportError" in st.error["details"]["stderr"]


def test_line_buffer_splits_utf8_across_chunks() -> None:
    lb = LineBuffer()
    data = (json.dumps({"ev": "log", "line": "tést ビデオ"}, ensure_ascii=False) + "\n").encode()
    cut = data.index("ビ".encode()) + 1  # split inside a multi-byte character
    assert lb.feed(data[:cut]) == []
    out = lb.feed(data[cut:] + b'{"ev": "done"')
    assert len(out) == 1 and Event.from_json(out[0]).data["line"] == "tést ビデオ"
    assert lb.flush() == ['{"ev": "done"']


def test_parse_cli_result() -> None:
    ok = _parse_cli_result(["doctor"], 0, False, '{"ok": true, "checks": []}', "")
    assert ok.ok and ok.data["ok"] is True
    err = _parse_cli_result(["view"], 2, False, '{"error": {"code": "usage", "message": "bad"}}', "")
    assert not err.ok and err.error["code"] == "usage"
    crash = _parse_cli_result(["doctor"], 1, False, "", "Traceback\nImportError: x")
    assert crash.error["code"] == "backend_failed" and "ImportError" in crash.error["details"]["stderr"]


def test_child_environment_forces_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LANG", "C")
    env = process_environment()
    assert env.value("PYTHONUTF8") == "1"
    assert env.value("LC_CTYPE") == "C.UTF-8"


def test_format_duration() -> None:
    assert format_duration(None) == "–"
    assert format_duration(65) == "1:05"
    assert format_duration(3725) == "1:02:05"


def _analysis(content: m.ContentClass, action: m.RecommendedAction, order: m.FieldOrder | None) -> m.AnalysisResult:
    a = m.AnalysisResult(input="x.avi")
    a.classification = m.Classification(content, order, 0.9)
    a.recommendation = m.Recommendation(action, "msg", True)
    return a


def test_job_decisions() -> None:
    it = JobItem("/v/a.avi", status=JobStatus.READY)
    it.analysis = _analysis(m.ContentClass.TELECINED, m.RecommendedAction.BLOCK_TELECINED, None)
    it.refresh_status()
    assert it.status == JobStatus.NEEDS_DECISION and "IVTC" in it.decision_needed()
    it.force = True
    assert "field order" in it.decision_needed()
    it.field_order_override = "tff"
    it.refresh_status()
    assert it.status == JobStatus.READY and it.effective_field_order() == "tff"
    assert it.content_text() == "TFF (override)"

    und = JobItem("/v/b.avi", status=JobStatus.READY)
    und.analysis = _analysis(m.ContentClass.UNDETERMINED, m.RecommendedAction.CONFIRM_FIELD_ORDER, None)
    und.refresh_status()
    assert und.status == JobStatus.NEEDS_DECISION
    und.field_order_override = "bff"
    und.refresh_status()
    assert und.status == JobStatus.READY


@pytest.mark.gui
def test_queue_model_add_move_remove() -> None:
    q = QueueModel()
    q.add_paths(["/a.avi", "/b.avi", "/c.avi", "/a.avi"])
    assert [i.name for i in q.items] == ["a.avi", "b.avi", "c.avi"]
    assert q.move(0, +1) == 1
    assert [i.name for i in q.items] == ["b.avi", "a.avi", "c.avi"]
    assert q.move(2, -1) == 1
    assert [i.name for i in q.items] == ["b.avi", "c.avi", "a.avi"]
    assert q.move(0, -1) == 0
    q.remove_rows([0, 2])
    assert [i.name for i in q.items] == ["c.avi"]


def test_runstate_cancel_needs_confirmation() -> None:
    st = RunState(cancelling=True)
    assert "Cancelling" in st.stage_text_or_cancelling()
    st.apply(Event("done", 0, {"exit_code": 130}))  # no ``cancelled`` event: cleanup unconfirmed
    assert not st.cancelled and st.cancel_unconfirmed

    st = RunState(cancelling=True)
    st.finish_without_done(-9)  # killed
    assert st.cancel_unconfirmed and not st.cancelled


def test_expand_paths_natural_order(tmp_path) -> None:
    from qtgmc_6ui.gui.queue_model import expand_paths

    for n in ("clip10.avi", "clip2.avi", "clip1.avi", "notes.txt"):
        (tmp_path / n).write_bytes(b"")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x.avi").write_bytes(b"")
    names = [p.removeprefix(str(tmp_path) + "/") for p in expand_paths([str(tmp_path)])]
    assert names == ["b/x.avi", "clip1.avi", "clip2.avi", "clip10.avi"]
