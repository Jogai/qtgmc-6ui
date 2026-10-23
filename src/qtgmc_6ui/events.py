"""JSON-lines event protocol (``--events``) between the CLI and the GUI.

One JSON object per line on **stdout**; every object has ``"ev"`` (type) and
``"t"`` (seconds since process start).  Human-readable output goes to stderr
in this mode, so stdout is machine-only.  Event types:

  start      {schema, command, input, pid, outputs: [planned paths]}
  stage      {name, index, total, label}          name in STAGES
  progress   {stage, done, total, fps, eta_s, elapsed_s}   frames (or percent for probe-only stages)
  log        {level, source, line}                 source: ffmpeg | vapoursynth | x264 | sox | app
  warning    {code, message, hint}
  result     {data}                                JobResult / AnalysisResult / DoctorReport dict
  error      {code, message, hint, details, exit_code}
  cancelled  {removed: [paths], kept: [paths]}
  done       {exit_code}                           always the last line

GUI cancel: send SIGTERM to the process, or write ``cancel\\n`` to its stdin.
Cancel timing (commands.CANCEL_GRACE_S, runtime):
  * the first cancel (stdin line, SIGTERM or SIGINT) emits ``log{source:"app",level:"warning",
    line:"cancelling ..."}`` at once; the running stage then stops feeding frames, SIGTERMs its
    encoder (SIGKILL after 5 s) and removes its ``.partial`` output; the CLI waits up to 15 s
    for that, then kills the children itself and removes what the run created; finally
    ``cancelled{removed,kept}`` and ``done{130}``.  Typically well under 1 s.
  * repeated SIGTERM / stdin cancels are the same cancel (they never cut the cleanup short);
    only SIGINT twice (terminal Ctrl-C Ctrl-C) exits immediately, still after killing the
    children and removing partial outputs, and still with ``cancelled`` + ``done{130}``.
  * front ends should wait >= 20 s after ``cancel`` before SIGKILL.  Even SIGKILL is safe for
    outputs: children die with the CLI (osdep die-with-parent) and unfinished outputs only exist
    as ``<output>.partial``.
  * a closed stdout (the reader died) is treated as a cancel.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import IO, Any, Protocol, TextIO

EVENTS_SCHEMA = 1

STAGES: tuple[str, ...] = (
    "probe",  # ffprobe
    "index",  # bestsource index
    "idet",  # idet windows
    "fieldmatch",  # telecine confirmation pass
    "classify",
    "setup",  # vs core, plugins, graph
    "master",  # deinterlace + ffv1/flac encode
    "view",  # one per viewing copy; label carries the profile id
    "verify",  # ffprobe of outputs
)


@dataclass
class Event:
    ev: str
    t: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"ev": self.ev, "t": round(self.t, 3), **self.data}, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> Event | None:
        """Parse one stdout line; returns None for non-JSON lines (never raises)."""
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            d = json.loads(line)
        except ValueError:
            return None
        if not isinstance(d, dict) or "ev" not in d:
            return None
        ev = d.pop("ev")
        t = float(d.pop("t", 0.0))
        return cls(ev, t, d)


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class JsonLinesSink:
    """Writes events as JSON lines to ``stream`` (stdout by default), flushing each line."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self.stream = stream or sys.stdout
        self.t0 = time.monotonic()

    def emit(self, event: Event) -> None:
        if not event.t:
            event.t = time.monotonic() - self.t0
        self.stream.write(event.to_json() + "\n")
        self.stream.flush()


class TextSink:
    """Human-readable rendering for interactive terminals (progress on one line to stderr)."""

    def __init__(self, stream: TextIO | None = None, verbose: bool = False) -> None:
        self.stream = stream or sys.stderr
        self.verbose = verbose
        self.t0 = time.monotonic()

    def emit(self, event: Event) -> None:
        d = event.data
        if event.ev == "progress":
            tot = d.get("total") or 0
            pct = f"{100 * d.get('done', 0) / tot:5.1f}%" if tot else "  ?  "
            eta = d.get("eta_s")
            eta_s = f" eta {int(eta // 60)}:{int(eta % 60):02d}" if isinstance(eta, (int, float)) else ""
            self.stream.write(
                f"\r{d.get('stage', ''):<8} {pct} {d.get('done', 0)}/{tot} {d.get('fps', 0):.1f} fps{eta_s}   "
            )
        elif event.ev == "stage":
            self.stream.write(f"\n== {d.get('label') or d.get('name')}\n")
        elif event.ev == "log":
            if self.verbose or d.get("level") in ("warning", "error"):
                self.stream.write(f"\n[{d.get('source')}] {d.get('line')}\n")
        elif event.ev == "warning":
            self.stream.write(f"\nwarning [{d.get('code')}]: {d.get('message')}\n")
        elif event.ev == "error":
            self.stream.write(f"\nerror [{d.get('code')}]: {d.get('message')}\n")
            if d.get("hint"):
                self.stream.write(f"  hint: {d['hint']}\n")
        elif event.ev == "cancelled":
            self.stream.write(f"\ncancelled; removed {len(d.get('removed', []))} partial file(s)\n")
        elif event.ev == "done":
            self.stream.write("\n")
        self.stream.flush()


class NullSink:
    def emit(self, event: Event) -> None:
        pass


class Emitter:
    """Convenience wrapper used by core modules: ``em.progress(...)`` etc."""

    def __init__(self, sink: EventSink | None = None) -> None:
        self.sink: EventSink = sink or NullSink()
        self.t0 = time.monotonic()

    def _emit(self, ev: str, **data: Any) -> None:
        self.sink.emit(Event(ev, time.monotonic() - self.t0, data))

    def start(self, command: str, input: str, outputs: list[str]) -> None:
        import os

        self._emit("start", schema=EVENTS_SCHEMA, command=command, input=input, pid=os.getpid(), outputs=outputs)

    def stage(self, name: str, index: int, total: int, label: str = "") -> None:
        self._emit("stage", name=name, index=index, total=total, label=label or name)

    def progress(self, stage: str, done: int, total: int | None, fps: float, eta_s: float | None, elapsed_s: float) -> None:
        self._emit("progress", stage=stage, done=done, total=total, fps=round(fps, 2), eta_s=eta_s, elapsed_s=round(elapsed_s, 2))

    def log(self, source: str, line: str, level: str = "info") -> None:
        self._emit("log", level=level, source=source, line=line)

    def warning(self, code: str, message: str, hint: str = "") -> None:
        self._emit("warning", code=code, message=message, hint=hint)

    def result(self, data: dict[str, Any]) -> None:
        self._emit("result", data=data)

    def error(self, err: dict[str, Any]) -> None:
        self._emit("error", **err)

    def cancelled(self, removed: list[str], kept: list[str]) -> None:
        self._emit("cancelled", removed=removed, kept=kept)

    def done(self, exit_code: int) -> None:
        self._emit("done", exit_code=int(exit_code))


class ProgressTracker:
    """Frames-done -> fps/ETA with a sliding window; call ``update(done)`` per frame."""

    def __init__(self, total: int | None, window_s: float = 5.0) -> None:
        self.total = total
        self.window_s = window_s
        self.t0 = time.monotonic()
        self.samples: list[tuple[float, int]] = [(self.t0, 0)]

    def update(self, done: int) -> tuple[float, float | None, float]:
        """Returns (fps, eta_s or None, elapsed_s)."""
        now = time.monotonic()
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window_s:
            self.samples.pop(0)
        t_a, d_a = self.samples[0]
        fps = (done - d_a) / (now - t_a) if now > t_a else 0.0
        eta = (self.total - done) / fps if (self.total and fps > 0) else None
        return fps, eta, now - self.t0
