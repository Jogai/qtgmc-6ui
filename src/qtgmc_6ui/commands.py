"""Subcommand implementations (WP-D).  ``cli.dispatch`` calls ``cmd_<name>(args)``.

Each handler: pick the sink (JsonLinesSink for --events, TextSink otherwise), build an
Emitter, install signal handlers + stdin cancel watcher for long commands, run, emit
``result`` + ``done``, return the exit code.

Long stages (analysis, pipeline.run_job, viewcopy.run_view) run on a worker thread while
the main thread waits on the CancelToken, so SIGTERM/SIGINT/"cancel" are seen at once even
while VapourSynth or a child process blocks the worker.  On cancel the worker gets
``CANCEL_GRACE_S`` to clean up; afterwards every planned output that was created or
replaced by this run and not reported as finished is removed (safety net).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any, TypeVar

from . import jobs, runtime
from .errors import CancelledError, ErrorCode, ExitCode, InputError, OutputError, QtgmcError, UsageError
from .events import Emitter, JsonLinesSink, NullSink, TextSink
from .models import (
    AnalysisResult,
    FieldOrder,
    FieldOrderChoice,
    JobResult,
    JobSpec,
    MasterSettings,
    PreviewMode,
    PreviewRequest,
    ProbeInfo,
    RateMode,
)

T = TypeVar("T")

CANCEL_GRACE_S = 15.0  # worker cleanup budget after cancel (pipeline: 5 s SIGTERM->SIGKILL + rm)


# --------------------------------------------------------------------------- helpers
def internal_error(exc: BaseException) -> QtgmcError:
    """Any non-QtgmcError becomes an INTERNAL error with the traceback in details, never a bare
    stack trace on the terminal."""
    if isinstance(exc, QtgmcError):
        return exc
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    if isinstance(exc, NotImplementedError):
        frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        where = f"{os.path.basename(frame.filename)}:{frame.name}" if frame else "?"
        return QtgmcError(
            ErrorCode.INTERNAL,
            f"not implemented in this build: {where}",
            "this component is not available; check the installation (qtgmc-6ui doctor)",
            {"traceback": "".join(tb[-6:])},
        )
    if isinstance(exc, MemoryError):
        return QtgmcError(ErrorCode.INTERNAL, "out of memory", "lower --cache-mb or --threads, or use a faster preset")
    return QtgmcError(
        ErrorCode.INTERNAL,
        f"unexpected error: {type(exc).__name__}: {exc}",
        "please report this bug and include the doctor report (qtgmc-6ui doctor --json)",
        {"traceback": "".join(tb[-12:])},
    )


def _mode(args: argparse.Namespace) -> str:
    if getattr(args, "events", False):
        return "events"
    if getattr(args, "json", False):
        return "json"
    return "text"


def _print_json(obj: Any, stream: Any = None) -> None:
    s = stream or sys.stdout
    s.write(json.dumps(obj, ensure_ascii=False) + "\n")
    s.flush()


class GuardedSink:
    """Serialises emits from the main, worker and pump threads (one line at a time, so
    ``done`` really is last), and turns a lost reader (BrokenPipeError: the GUI died or
    closed the pipe) into a cancel instead of a crash, so partial outputs get removed."""

    def __init__(self, inner: Any, token: runtime.CancelToken) -> None:
        self.inner = inner
        self.token = token
        self.lock = threading.RLock()  # re-entrant: the hard-exit hook may emit from a signal handler
        self.lost = False
        self.observers: list[Callable[[Any], None]] = []

    def emit(self, event: Any) -> None:
        for ob in self.observers:
            ob(event)
        if self.lost:
            return
        if not self.lock.acquire(timeout=2.0):
            return
        try:
            self.inner.emit(event)
        except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
            self.lost = True
            if isinstance(self.inner, JsonLinesSink):  # the event reader (GUI) is gone
                self.token.cancel("output closed")
            try:
                sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - silence later writes/flush at exit
            except OSError:
                pass
        finally:
            self.lock.release()


class Ctx:
    """Per-command output mode, emitter and cancel token."""

    def __init__(self, args: argparse.Namespace, *, long_running: bool = False, watch_stdin: bool = False) -> None:
        self.args = args
        self.mode = _mode(args)
        self.verbose = bool(getattr(args, "verbose", False))
        self.token = runtime.CancelToken()
        if self.mode == "events":
            sink: Any = JsonLinesSink(sys.stdout)
        elif self.mode == "json":
            sink = TextSink(sys.stderr, self.verbose) if sys.stderr.isatty() else NullSink()
        else:
            sink = TextSink(sys.stderr, self.verbose)
        self.sink = GuardedSink(sink, self.token)
        self.em = Emitter(self.sink)
        self.guard: OutputGuard | None = None
        if long_running:
            runtime.on_hard_exit(self._hard_exit)
            runtime.set_default_cancel(self.token)
            runtime.install_signal_handlers(self.token)
            if watch_stdin:
                self.watch_stdin()

    def _hard_exit(self) -> None:
        if self.guard is not None:
            removed, kept = self.guard.cleanup()
        else:
            removed, kept = [], []
        self.cancelled(removed, kept)

    def watch_stdin(self) -> None:
        try:
            interactive = sys.stdin is None or sys.stdin.isatty()
        except (ValueError, OSError):
            interactive = True
        if not interactive:
            runtime.watch_stdin_for_cancel(self.token)

    # ---- terminal states
    def result(self, data: dict[str, Any], text: str | None = None) -> int:
        if self.mode == "events":
            self.em.result(data)
            self.em.done(ExitCode.OK)
        elif self.mode == "json":
            _print_json(data)
        else:
            if text:
                sys.stdout.write(text if text.endswith("\n") else text + "\n")
                sys.stdout.flush()
        return int(ExitCode.OK)

    def fail(self, err: QtgmcError) -> int:
        d = err.to_dict()
        if self.mode == "events":
            self.em.error(d)
            self.em.done(err.exit_code)
        elif self.mode == "json":
            _print_json({"error": d})
        else:
            sys.stderr.write(f"\nerror [{err.code}]: {err.message}\n")
            if err.hint:
                sys.stderr.write(f"  hint: {err.hint}\n")
            if self.verbose and err.details.get("traceback"):
                sys.stderr.write(str(err.details["traceback"]))
            tail = err.details.get("stderr_tail")
            if tail:
                sys.stderr.write("  last tool output:\n" + "".join(f"    {ln}\n" for ln in list(tail)[-10:]))
            sys.stderr.flush()
        return int(err.exit_code)

    def cancelled(self, removed: list[str], kept: list[str]) -> int:
        if self.mode == "events":
            self.em.cancelled(removed, kept)
            self.em.done(ExitCode.CANCELLED)
        elif self.mode == "json":
            _print_json({"error": CancelledError(removed, kept).to_dict(), "cancelled": {"removed": removed, "kept": kept}})
        else:
            self.em.cancelled(removed, kept)
            for p in removed:
                sys.stderr.write(f"  removed partial: {p}\n")
            for p in kept:
                sys.stderr.write(f"  kept finished: {p}\n")
        return int(ExitCode.CANCELLED)


def run_cancellable(token: runtime.CancelToken, fn: Callable[[], T], grace_s: float | None = None, em: Emitter | None = None) -> T:
    """Run ``fn`` on a worker thread; the calling (main) thread keeps handling signals.

    Raises CancelledError when the token fires (after giving ``fn`` ``grace_s`` to wind
    down and clean up) - also when ``fn`` itself raised because its child was killed.
    On cancel a ``log{source:"app", level:"warning"}`` "cancelling ..." event is emitted at
    once, so a front end can show that cleanup is in progress."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["r"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised on the main thread
            box["e"] = e

    th = threading.Thread(target=work, name="job", daemon=True)
    th.start()
    while th.is_alive():
        th.join(0.1)
        if token.cancelled:
            g = CANCEL_GRACE_S if grace_s is None else grace_s
            if em is not None:
                em.log("app", f"cancelling ({token.reason}): stopping and removing partial output (up to {g:.0f} s)", "warning")
            th.join(g)
            if th.is_alive():
                runtime.kill_all_children(signal.SIGKILL)
                th.join(2.0)
                raise CancelledError()
            break
    e = box.get("e")
    if isinstance(e, CancelledError):
        raise e
    if token.cancelled:
        if isinstance(e, QtgmcError) and e.code == ErrorCode.CANCELLED:
            raise e
        raise CancelledError()
    if e is not None:
        raise e
    return box["r"]


def _stat(p: str) -> tuple[int, int, int] | None:
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


PARTIAL_SUFFIX = ".partial"


def partial_names(output: str) -> list[str]:
    """Temp names a stage may write before renaming onto ``output`` when verified:
    ``<dir>/.<name>.partial`` (pipeline.partial_path) and ``<output>.partial``."""
    d, n = os.path.split(output)
    return [os.path.join(d, f".{n}{PARTIAL_SUFFIX}"), output + PARTIAL_SUFFIX]


class OutputGuard:
    """Planned outputs of one command: creates their directories when the run starts and
    removes what this run created or replaced when it is cancelled - also from the hard-exit
    path (second Ctrl-C), where it emits ``cancelled`` + ``done{130}`` itself.

    Outputs are finished in order (master, then views); an output counts as finished once
    the next ``view`` stage has started, and finished outputs are never removed."""

    def __init__(self, ctx: Ctx, planned: list[str]) -> None:
        self.ctx = ctx
        self.views_started = 0
        ctx.sink.observers.append(self._observe)
        self.planned = [p for p in planned if p]
        self.files = self.planned + [x for p in self.planned for x in partial_names(p)]
        self.before = {p: _stat(p) for p in self.files}
        self.created_dirs: list[str] = []
        self.kept: list[str] = []
        ctx.guard = self

    def make_dirs(self, skip: set[str] | None = None) -> None:
        for p in self.planned:
            if skip and p in skip:
                continue
            d = os.path.dirname(p)
            missing = []
            while d and not os.path.isdir(d):
                missing.append(d)
                d = os.path.dirname(d)
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
            except OSError as e:
                raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"cannot create {os.path.dirname(p)}: {e.strerror}") from None
            self.created_dirs += missing  # deepest first

    def _observe(self, ev: Any) -> None:
        if ev.ev == "stage" and ev.data.get("name") == "view":
            self.views_started += 1

    def finished(self) -> list[str]:
        """Outputs whose stage is over: the master once the first view starts, and so on
        (single-output commands: none until the result)."""
        return self.planned[: self.views_started] if len(self.planned) > 1 else []

    def cleanup(self, err: CancelledError | None = None) -> tuple[list[str], list[str]]:
        removed = list((err.details.get("removed") if err else None) or [])
        kept = list((err.details.get("kept") if err else None) or []) + self.kept
        kept += [p for p in self.finished() if p not in kept and p not in removed and os.path.exists(p)]
        for p in self.files:
            if p in kept or p in removed:
                continue
            now = _stat(p)
            if now is not None and now != self.before.get(p):  # created or replaced by this run
                removed += runtime.remove_partial([p])
        for d in self.created_dirs:
            try:
                os.rmdir(d)  # only if empty
            except OSError:
                pass
        return removed, kept

    def close(self) -> None:
        if self.ctx.guard is self:
            self.ctx.guard = None
        if self._observe in self.ctx.sink.observers:
            self.ctx.sink.observers.remove(self._observe)


def _probe_dict(p: ProbeInfo) -> str:
    v = p.video
    return (
        f"{v.codec} {v.width}x{v.height} {v.pix_fmt} {v.fps.numerator}/{v.fps.denominator} fps "
        f"SAR {v.sar.numerator}:{v.sar.denominator} frames {v.nb_frames if v.nb_frames is not None else '?'}"
        f"{'' if v.nb_frames_exact else ' (est.)'}"
    )


def analysis_text(a: AnalysisResult) -> str:
    c, r, p = a.classification, a.recommendation, a.probe
    lines = [f"input:          {a.input}", f"container:      {p.container}  {p.duration_s:.2f} s  {p.size_bytes} bytes"]
    lines.append(f"video:          {_probe_dict(p)}")
    lines.append(f"container flag: field_order={p.video.container_field_order} (not used for decisions)")
    for au in p.audio:
        lines.append(f"audio #{au.index}:       {au.codec} {au.sample_rate} Hz {au.channels} ch {au.duration_s:.2f} s")
    tm, tr = a.totals_multi, a.totals_repeated
    scope = "all frames" if a.sampled_all_frames else f"{len(a.windows)} window(s)"
    lines.append(f"idet ({scope}):  TFF {tm.tff}  BFF {tm.bff}  progressive {tm.progressive}  undetermined {tm.undetermined}")
    lines.append(f"repeated fields: top {tr.top}  bottom {tr.bottom}  neither {tr.neither}" + ("  (fieldmatch pass run)" if a.fieldmatch_run else ""))
    fo = f" ({c.field_order})" if c.field_order else ""
    lines.append(f"classification: {c.content}{fo}  confidence {c.confidence:.2f}")
    lines += [f"  - {x}" for x in c.reasons]
    lines += [f"  warning: {x}" for x in c.warnings]
    lines.append(f"recommendation: {r.action}" + ("" if r.force_allowed else " (no override)"))
    if r.message:
        lines += [f"  {ln}" for ln in r.message.splitlines()]
    col = a.colorimetry
    lines.append(f"colorimetry:    {col.matrix}/{col.primaries}/{col.transfer} {col.range} ({col.source})")
    out = ""
    if a.frames_out_double is not None and a.fps_out_double is not None:
        out = f"; double rate -> {a.frames_out_double} frames @ {a.fps_out_double.numerator}/{a.fps_out_double.denominator}"
    lines.append(f"default preset: {a.preset_default}{out}")
    lines.append(f"took {a.took_s:.2f} s")
    return "\n".join(lines)


def _input_path(path: str) -> str:
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(p):
        raise InputError(ErrorCode.INPUT_NOT_FOUND, f"input not found: {p}", "check the path")
    if not os.path.isfile(p):
        raise InputError(ErrorCode.INPUT_UNREADABLE, f"input is not a regular file: {p}")
    return p


def _analyze(path: str, em: Emitter, **kw: Any) -> AnalysisResult:
    from . import analysis

    return analysis.analyze(path, emitter=em, **kw)


# --------------------------------------------------------------------------- analyze
def cmd_analyze(args: argparse.Namespace) -> int:
    ctx = Ctx(args, long_running=True, watch_stdin=True)
    try:
        inp = _input_path(args.input)
        ctx.em.start("analyze", inp, [])
        res = run_cancellable(
            ctx.token,
            lambda: _analyze(inp, ctx.em, full_scan=args.full, windows=args.windows, window_frames=args.window_frames, use_cache=not args.no_cache),
            em=ctx.em,
        )
    except CancelledError:
        return ctx.cancelled([], [])
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, KeyboardInterrupt):
            return ctx.cancelled([], [])
        return ctx.fail(internal_error(e))
    return ctx.result(res.to_dict(), analysis_text(res))


# --------------------------------------------------------------------------- deint / run-job
def _synth_master_probe(a: AnalysisResult, settings: MasterSettings, path: str) -> ProbeInfo:
    p = ProbeInfo.from_dict(a.probe.to_dict())
    p.path = path
    p.container = "matroska,webm"
    p.video.codec = "ffv1"
    if settings.rate == RateMode.DOUBLE:
        p.video.fps = p.video.fps * 2
        if p.video.nb_frames is not None:
            p.video.nb_frames *= 2
    for au in p.audio:
        au.codec = "flac"
    return p


def _dry_run_plan(job: JobSpec, a: AnalysisResult, outputs: list[str], warnings: list[str], profs: list[Any]) -> dict[str, Any]:
    plan: dict[str, Any] = {"dry_run": True, "job": job.to_dict(), "outputs": outputs, "warnings": warnings, "notes": []}
    plan["job"]["analysis"] = None  # the analysis is reported separately
    plan["analysis"] = a.to_dict()
    try:
        from . import master

        col = job.master.colorimetry_override or a.colorimetry
        plan["master_command"] = master.master_command(job.input, job.master_output or "", a.probe.video.sar, col, overwrite=job.overwrite)
    except Exception as e:  # noqa: BLE001 - a plan is best effort
        plan["notes"].append(f"master command unavailable: {internal_error(e).message}")
    try:
        from . import graph

        plan["script"] = graph.script_text(a, job.master)
    except Exception as e:  # noqa: BLE001
        plan["notes"].append(f"script unavailable: {internal_error(e).message}")
    try:
        from . import qtgmc

        plan["warnings"] = warnings + list(qtgmc.fidelity_warnings(job.master))
    except Exception:  # noqa: BLE001
        pass
    views = []
    mp = _synth_master_probe(a, job.master, job.master_output or "")
    for v, prof in zip(job.views, profs, strict=True):
        entry: dict[str, Any] = {"profile": prof.id, "output": v.output}
        try:
            from . import viewcopy

            entry["commands"] = viewcopy.build_commands(job.master_output or "", mp, prof, v.output or "", overwrite=job.overwrite)
        except Exception as e:  # noqa: BLE001
            entry["note"] = f"commands unavailable: {internal_error(e).message}"
        views.append(entry)
    plan["views"] = views
    return plan


def _plan_text(plan: dict[str, Any]) -> str:
    import shlex

    lines = ["dry run: nothing was written", "outputs:"] + [f"  {o}" for o in plan["outputs"]]
    lines += [f"warning: {w}" for w in plan.get("warnings", [])]
    if plan.get("master_command"):
        lines += ["master (y4m from VapourSynth on stdin):", "  " + shlex.join(plan["master_command"])]
    for v in plan.get("views", []):
        lines.append(f"view {v['profile']} -> {v['output']}:")
        for c in v.get("commands") or []:
            lines.append("  " + shlex.join(c))
        if v.get("note"):
            lines.append(f"  ({v['note']})")
    lines += [f"note: {n}" for n in plan.get("notes", [])]
    return "\n".join(lines)


def _job_text(res: JobResult) -> str:
    lines = [f"done in {res.elapsed_s:.1f} s" + (f" ({res.average_fps:.1f} fps)" if res.average_fps else "")]
    for o in res.outputs:
        extra = []
        if o.frames is not None:
            extra.append(f"{o.frames} frames")
        if o.duration_s is not None:
            extra.append(f"{o.duration_s:.2f} s")
        if o.size_bytes is not None:
            extra.append(f"{o.size_bytes / 1e6:.1f} MB")
        lines.append(f"{o.kind:<6} {o.path}" + (f"  ({', '.join(extra)})" if extra else ""))
    lines += [f"warning: {w}" for w in res.warnings]
    return "\n".join(lines)


def execute_job(ctx: Ctx, job: JobSpec, command: str, *, dry_run: bool = False) -> int:
    """Validate, plan outputs, emit start, analyze (unless given), apply blocking rules, run
    pipeline.run_job; cancel/error/result handling with the matching events."""
    guard: OutputGuard | None = None
    try:
        profs = jobs.resolve_profiles(job) if job.views else []
        planned = jobs.plan_outputs(job, profs)
        jobs.check_job(job, planned)
        guard = OutputGuard(ctx, planned)
        ctx.em.start(command, job.input, planned)
        if job.analysis is None:
            job.analysis = run_cancellable(ctx.token, lambda: _analyze(job.input, ctx.em), em=ctx.em)
        warnings = jobs.check_blocked(job.analysis, job.master, job.force)
        for w in warnings:
            ctx.em.warning("forced" if w.startswith("--force") else "field_order_override", w)
        if dry_run:
            plan = _dry_run_plan(job, job.analysis, planned, warnings, profs)
            return ctx.result(plan, _plan_text(plan))
        if ctx.token.cancelled:
            raise CancelledError()
        if job.skip_master and job.master_output:
            guard.kept.append(job.master_output)
        guard.make_dirs({job.master_output} if job.skip_master and job.master_output else None)
        from . import pipeline

        res: JobResult = run_cancellable(ctx.token, lambda: pipeline.run_job(job, emitter=ctx.em, cancel=ctx.token), em=ctx.em)
        for w in warnings:
            if w not in res.warnings:
                res.warnings.insert(0, w)
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, (CancelledError, KeyboardInterrupt)) or ctx.token.cancelled:
            err = e if isinstance(e, CancelledError) else CancelledError()
            removed, kept = guard.cleanup(err) if guard else (list(err.details.get("removed") or []), [])
            return ctx.cancelled(removed, kept)
        if guard:
            guard.cleanup()  # failed run: no half-written outputs and no new empty dirs
        return ctx.fail(internal_error(e))
    finally:
        if guard:
            guard.close()
    return ctx.result(res.to_dict(), _job_text(res))


def _save_job(args: argparse.Namespace, job: JobSpec) -> int:
    text = jobs.dump_job(job)
    if args.save_job == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
        return 0
    path = os.path.abspath(os.path.expanduser(args.save_job))
    if os.path.exists(path) and not args.overwrite:
        raise OutputError(ErrorCode.OUTPUT_EXISTS, f"job file exists: {path}", "choose another file, or allow overwriting (--overwrite)")
    if jobs.same_file(path, job.input):
        raise UsageError("--save-job would overwrite the input")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"cannot write {path}: {e.strerror}") from None
    if _mode(args) == "json":
        _print_json({"job": path})
    elif _mode(args) == "text":
        print(path)
    else:
        em = Emitter(JsonLinesSink(sys.stdout))
        em.result({"job": path})
        em.done(0)
    return 0


def cmd_deint(args: argparse.Namespace) -> int:
    job = jobs.job_from_args(args)
    if args.save_job:
        return _save_job(args, job)
    ctx = Ctx(args, long_running=True, watch_stdin=True)
    return execute_job(ctx, job, "deint", dry_run=args.dry_run)


def cmd_run_job(args: argparse.Namespace) -> int:
    # ``-``: the job document is read first; the rest of stdin is then watched for "cancel".
    ctx = Ctx(args, long_running=True, watch_stdin=False)
    try:
        job = jobs.load_job(args.job)
    except QtgmcError as e:
        return ctx.fail(e)
    ctx.watch_stdin()
    return execute_job(ctx, job, "run-job", dry_run=getattr(args, "dry_run", False))


# --------------------------------------------------------------------------- view
def cmd_view(args: argparse.Namespace) -> int:
    ctx = Ctx(args, long_running=True, watch_stdin=True)
    guard: OutputGuard | None = None
    try:
        from . import ffprobe, profiles, viewcopy

        master = _input_path(args.master)
        prof = profiles.load(args.profile)
        if args.output:
            out = os.path.abspath(os.path.expanduser(args.output))
        else:
            stem = os.path.splitext(os.path.basename(master))[0]
            out = os.path.join(os.path.dirname(master), f"{stem}.{prof.id}.{prof.extension}")
        job = JobSpec(input=master, output_dir=os.path.dirname(out), master_output=out, overwrite=args.overwrite)
        jobs.check_job(job, [out])
        guard = OutputGuard(ctx, [out])
        ctx.em.start("view", master, [out])
        probe = ffprobe.probe(master)
        guard.make_dirs()
        of = run_cancellable(
            ctx.token,
            lambda: viewcopy.run_view(master, probe, prof, out, emitter=ctx.em, cancel=ctx.token, overwrite=args.overwrite),
            em=ctx.em,
        )
        res = JobResult(input=master, outputs=[of])
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, (CancelledError, KeyboardInterrupt)) or ctx.token.cancelled:
            err = e if isinstance(e, CancelledError) else CancelledError()
            removed, kept = guard.cleanup(err) if guard else ([], [])
            return ctx.cancelled(removed, kept)
        if guard:
            guard.cleanup()
        return ctx.fail(internal_error(e))
    finally:
        if guard:
            guard.close()
    return ctx.result(res.to_dict(), _job_text(res))


# --------------------------------------------------------------------------- profiles
def _read_profile_source(target: str) -> tuple[str, str | None]:
    """Text of FILE (or stdin for '-') and the source path (None for stdin)."""
    if target == "-":
        return sys.stdin.read(), None
    path = os.path.abspath(os.path.expanduser(target))
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(), path
    except FileNotFoundError:
        raise UsageError(f"profile file not found: {path}") from None
    except OSError as e:
        raise UsageError(f"cannot read {path}: {e.strerror}") from None


def parse_profile_text(text: str, source: str | None = None) -> Any:
    """TOML or JSON (detected by a leading '{') -> Profile; PROFILE_INVALID on problems."""
    from dataclasses import fields

    from . import profiles
    from .models import Profile

    if text.lstrip().startswith("{"):
        try:
            d = json.loads(text)
        except ValueError as e:
            raise QtgmcError(ErrorCode.PROFILE_INVALID, f"profile is not valid JSON: {e}") from None
        if not isinstance(d, dict):
            raise QtgmcError(ErrorCode.PROFILE_INVALID, "profile JSON must be an object")
        unknown = sorted(set(d) - {f.name for f in fields(Profile)})
        if unknown:
            raise QtgmcError(ErrorCode.PROFILE_INVALID, "profile has unknown keys: " + ", ".join(unknown))
        try:
            text = profiles.to_toml(Profile.from_dict(d))  # one parser for both: nested key checks too
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            raise QtgmcError(ErrorCode.PROFILE_INVALID, f"profile has an invalid value: {e}") from None
    return profiles.from_toml(text, path=source)


def cmd_profiles(args: argparse.Namespace) -> int:
    from . import paths, profiles

    js = bool(args.json)
    act = args.action
    if act in ("show", "validate", "export", "save", "delete") and not args.target:
        what = "a profile file or - for stdin" if act == "save" else "a profile id or .toml path"
        raise UsageError(f"profiles {act} needs {what}", "list the profiles with: qtgmc-6ui profiles list")
    if act == "path":
        d = str(paths.profiles_dir())
        _print_json({"path": d}) if js else print(d)
        return 0
    if act == "list":
        profs = profiles.list_profiles(include_unavailable=args.all)
        if js:
            _print_json([p.to_dict() | {"available": profiles.is_available(p)} for p in profs])
        else:
            for p in profs:
                kind = "built-in" if p.builtin else "user"
                v = p.video.encoder if p.video.backend == "ffmpeg" else f"{p.video.backend} (CLI)"
                print(f"{p.id:<24} {kind:<8} {p.container:<10} {v:<12} {p.audio.encoder:<10} {p.name}")
        return 0
    if act == "save":
        return _profiles_save(args)
    if act == "delete":
        return _profiles_delete(args)
    prof = profiles.load(args.target)
    if act == "show":
        if js:
            _print_json(prof.to_dict())
        else:
            sys.stdout.write(profiles.to_toml(prof))
        return 0
    if act == "validate":
        problems = profiles.validate(prof)
        if js:
            _print_json({"id": prof.id, "path": prof.path, "valid": not problems, "problems": problems})
        elif problems:
            print(f"{prof.id}: INVALID")
            for pr in problems:
                print(f"  - {pr}")
        else:
            print(f"{prof.id}: ok")
        return int(ExitCode.OK if not problems else ExitCode.USAGE)
    # export
    text = profiles.to_toml(prof)
    if not args.output or args.output == "-":
        sys.stdout.write(text)
        return 0
    path = os.path.abspath(os.path.expanduser(args.output))
    if os.path.exists(path):
        raise OutputError(ErrorCode.OUTPUT_EXISTS, f"file exists: {path}", "remove it or choose another file name")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"cannot write {path}: {e.strerror}") from None
    _print_json({"path": path}) if js else print(path)
    return 0


def _profiles_save(args: argparse.Namespace) -> int:
    """Validate a profile (TOML or JSON, file or stdin) and store it in the user profile dir.

    --json prints ``{"id", "path", "valid", "problems", "profile"}`` (path null and an
    ``error`` object when it was not stored).  Exit 2 invalid or built-in id, 7 exists."""
    from . import paths, profiles

    text, source = _read_profile_source(args.target)
    prof = parse_profile_text(text, source)
    if args.id:
        prof.id = args.id
    prof.builtin, prof.path = False, None
    doc: dict[str, Any] = {"id": prof.id, "path": None, "valid": False, "problems": [], "profile": prof.to_dict()}

    def refuse(err: QtgmcError) -> int:
        if args.json:
            _print_json(doc | {"error": err.to_dict()})
            return int(err.exit_code)
        raise err

    if any(b.id == prof.id for b in profiles.builtin_profiles()):
        return refuse(QtgmcError(ErrorCode.PROFILE_INVALID, f"'{prof.id}' is the id of a built-in profile",
                                 "save it under a new id (--id NEW-ID)", {"profile": prof.id}))
    target = paths.profiles_dir() / f"{prof.id}.toml"
    if target.exists() and not args.overwrite:
        return refuse(OutputError(ErrorCode.OUTPUT_EXISTS, f"a user profile '{prof.id}' already exists",
                                  "replace it (--overwrite) or choose another id", {"path": str(target)}))
    problems = profiles.validate(prof)
    doc["problems"] = problems
    if problems:
        return refuse(QtgmcError(ErrorCode.PROFILE_INVALID, f"profile '{prof.id}' is invalid: " + "; ".join(problems),
                                 "fix the listed problems and save again", {"problems": problems}))
    path = profiles.save(prof)
    doc.update(path=str(path), valid=True, profile=prof.to_dict())
    _print_json(doc) if args.json else print(path)
    return 0


def _profiles_delete(args: argparse.Namespace) -> int:
    """Delete a user profile; built-ins refused (exit 2); already absent is fine (idempotent)."""
    from . import profiles

    deleted = True
    try:
        profiles.delete(args.target)
    except QtgmcError as e:
        if e.code != ErrorCode.PROFILE_NOT_FOUND:
            raise
        deleted = False
    _print_json({"id": args.target, "deleted": deleted}) if args.json else print(
        f"deleted {args.target}" if deleted else f"{args.target}: no such user profile (nothing to do)"
    )
    return 0


# --------------------------------------------------------------------------- encoders
COMMON_CONTAINERS = ("matroska", "mp4", "mov", "webm", "avi", "mpegts", "ogg", "nut")


def _mux_entry(container: str, enc: Any, probe_now: bool) -> dict[str, Any]:
    """Verdict + human reason for one pair; never runs ffmpeg unless ``probe_now``."""
    from . import muxmatrix
    from .models import MuxVerdict

    p = muxmatrix.probe(container, enc.name, kind=enc.kind) if probe_now else muxmatrix.cached(container, enc.name, kind=enc.kind)
    if p.verdict == MuxVerdict.UNKNOWN:
        reason = "not tested yet (qtgmc-6ui encoders --probe --container ... tests it)"
    else:
        reason = muxmatrix.reason(container, enc.name, kind=enc.kind)  # cache hit: no ffmpeg run
    return {"container": container, "encoder": enc.name, "kind": enc.kind, "verdict": str(p.verdict), "reason": reason, "note": p.note}


def cmd_encoders(args: argparse.Namespace) -> int:
    """Tools, muxers (with file extensions), encoders, and container x encoder verdicts.

    ``--container C`` answers from the probe cache (seeded at install, so instant); pairs never
    tested report verdict ``unknown`` and are tested only with ``--probe``."""
    from . import encoders, muxmatrix

    js = bool(args.json)
    tools = encoders.detect_tools()
    muxers = encoders.list_muxers()
    all_encs = encoders.list_encoders()
    kinds = {e.name: e.kind for e in all_encs}
    if args.encoder and args.encoder not in kinds:
        raise UsageError(f"unknown encoder {args.encoder!r}", "list them with: qtgmc-6ui encoders")
    if args.container and args.container not in {m.name for m in muxers}:
        raise UsageError(f"unknown container (muxer) {args.container!r}", "list them with: qtgmc-6ui encoders")
    encs = [e for e in all_encs if (not args.kind or e.kind == args.kind) and not (args.common and encoders.is_hw_encoder(e.name))]
    if args.common:
        muxers = [m for m in muxers if m.name in COMMON_CONTAINERS or m.name == args.container]
    for m in muxers:  # extensions come from `-h muxer=` (disk-cached); only where they matter
        if not m.extensions and (m.name in COMMON_CONTAINERS or m.name == args.container):
            try:
                det = encoders.muxer_details(m.name)
                m.extensions = det.extensions
                m.default_video, m.default_audio = m.default_video or det.default_video, m.default_audio or det.default_audio
            except QtgmcError:
                pass
    out: dict[str, Any] = {"tools": [t.to_dict() for t in tools]}
    if args.container:
        out["muxer"] = next(m for m in muxers if m.name == args.container).to_dict()
        sel = [e for e in encs if not args.encoder or e.name == args.encoder]
        out["probes"] = [_mux_entry(args.container, e, args.probe) for e in sel]
        out["unprobed"] = sum(1 for p in out["probes"] if p["verdict"] == "unknown")
    elif args.encoder:
        out["encoder"] = encoders.encoder_details(args.encoder).to_dict()
        if args.probe:
            e = next(x for x in all_encs if x.name == args.encoder)
            out["probes"] = [_mux_entry(c, e, True) for c in COMMON_CONTAINERS if c in {m.name for m in muxers}]
    elif args.probe:
        cons = [c for c in COMMON_CONTAINERS if c in {m.name for m in muxers}]
        probes = muxmatrix.probe_all(cons, [e.name for e in encs if e.kind == "video"], [e.name for e in encs if e.kind == "audio"])
        out["probes"] = [p.to_dict() for p in probes]
    if not (args.container or args.encoder):
        out["muxers"] = [m.to_dict() for m in muxers]
        out["encoders"] = [e.to_dict() for e in encs]
    if js:
        _print_json(out)
        return 0
    print("tools:")
    for t in tools:
        state = f"{t.version or ''} {t.path or ''}".strip() if t.available else "not installed"
        print(f"  {t.name:<8} {state}" + (f"  ({t.note})" if t.note else ""))
    if "muxers" in out:
        print(f"containers ({len(muxers)}): " + " ".join(m.name for m in muxers))
        print("video encoders: " + " ".join(e.name for e in encs if e.kind == "video"))
        print("audio encoders: " + " ".join(e.name for e in encs if e.kind == "audio"))
    if "muxer" in out:
        m = out["muxer"]
        print(f"{m['name']}: extensions {', '.join(m['extensions']) or '?'}")
    if "encoder" in out:
        e = out["encoder"]
        print(f"{e['name']} ({e['kind']}): {e['description']}")
        if e["pix_fmts"]:
            print("  pix_fmts: " + " ".join(e["pix_fmts"]))
        for o in e["options"]:
            ch = f" [{'|'.join(o['choices'])}]" if o["choices"] else ""
            print(f"  -{o['name']} <{o['type']}>{ch} {o['help']}")
    for p in out.get("probes", []):
        why = p.get("reason") or p.get("note") or ""
        print(f"  {p['container']:<10} {p['encoder']:<16} {p['verdict']:<16} {why}")
    return 0


# --------------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    report = doctor.run(load_plugins=not args.no_load, smoke=not (args.no_smoke or args.no_load))
    if args.json:
        _print_json(report.to_dict())
    else:
        sys.stdout.write(doctor.render_text(report).rstrip("\n") + "\n")
    return int(ExitCode.OK if report.ok else ExitCode.COMPONENT)


# --------------------------------------------------------------------------- preview
def _preview_request(args: argparse.Namespace, inp: str, frame: int, output: str) -> PreviewRequest:
    settings = MasterSettings(
        field_order=FieldOrderChoice(args.field_order) if getattr(args, "field_order", None) else FieldOrderChoice.AUTO,
        rate=RateMode(getattr(args, "rate", None) or "double"),
        preset=None,
        lossless=not getattr(args, "no_lossless", False),
    )
    if getattr(args, "preset", None):
        from .models import Preset

        settings.preset = Preset(args.preset)
    return PreviewRequest(
        input=inp,
        frame=frame,
        mode=PreviewMode(args.mode),
        field_order=FieldOrder(args.field_order) if getattr(args, "field_order", None) else None,
        master=settings,
        output=output,
        window=args.window,
    )


def _preview_output(inp: str, out: str) -> str:
    if out == "-":
        return out
    p = os.path.abspath(os.path.expanduser(out))
    if jobs.same_file(p, inp):
        raise UsageError("preview output would overwrite the input", "choose another -o")
    return p


def cmd_preview_frame(args: argparse.Namespace) -> int:
    from . import preview

    inp = _input_path(args.input)
    out = _preview_output(inp, args.output)
    if args.frame < 0:
        raise UsageError("frame number must be >= 0")
    res = preview.render(_preview_request(args, inp, args.frame, out))
    data = res.to_dict()
    data["source"] = preview.source_info(inp)
    if args.json:
        _print_json(data, sys.stderr if out == "-" else sys.stdout)
    elif out != "-":
        print(f"{res.output} {res.width}x{res.height} ({res.took_s * 1000:.0f} ms){' - ' + res.note if res.note else ''}")
    return 0


def cmd_preview_info(args: argparse.Namespace) -> int:
    from . import preview

    inp = _input_path(args.input)
    info = preview.source_info(inp)
    if args.json:
        _print_json(info)
    else:
        print(f"{inp}: {info['frames']} frames, {info['fps']} fps, {info['width']}x{info['height']} {info['format']} SAR {info['sar']}")
    return 0


def cmd_preview_serve(args: argparse.Namespace) -> int:
    """Long-lived renderer for GUI scrubbing: one JSON request per stdin line, one event line
    per reply (see preview.serve)."""
    from . import preview

    inp = _input_path(args.input)
    return preview.serve(inp, sys.stdin, sys.stdout)


# --------------------------------------------------------------------------- script
def cmd_script(args: argparse.Namespace) -> int:
    from . import graph

    inp = _input_path(args.input)
    settings = jobs.master_settings_from_args(args)
    a = _analyze(inp, Emitter(NullSink()))
    if settings.field_order == FieldOrderChoice.AUTO and a.classification.field_order is None:
        raise QtgmcError(
            ErrorCode.BLOCKED_UNDETERMINED,
            "field order could not be determined",
            "choose the field order explicitly (--field-order tff|bff)",
        )
    sys.stdout.write(graph.script_text(a, settings).rstrip("\n") + "\n")
    return 0
