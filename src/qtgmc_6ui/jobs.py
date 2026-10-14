"""Job model helpers (WP-D): build a JobSpec from CLI flags, read/write JobSpec JSON,
validate paths, plan outputs and apply the blocking rules before anything is written.

The queue itself lives in the GUI (gui/queue_model.py); the CLI runs one job per process
(``qtgmc-6ui run-job JOB.json`` or ``-`` for stdin).  One job = one master + N viewing
copies; the stages themselves run in pipeline.run_job.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import IO, Any

from . import osdep
from .errors import BlockedError, ErrorCode, InputError, OutputError, QtgmcError, UsageError
from .models import (
    JOB_SCHEMA,
    AnalysisResult,
    ChromaMode,
    ContentClass,
    FieldOrder,
    FieldOrderChoice,
    JobSpec,
    MasterSettings,
    Preset,
    RateMode,
    RecommendedAction,
    ViewCopySpec,
)

MASTER_EXT = ".mkv"


def _abs(p: str, base: str | None = None) -> str:
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(base or os.getcwd(), p)
    return os.path.normpath(p)


def master_settings_from_args(args: argparse.Namespace) -> MasterSettings:
    preset = getattr(args, "preset", None)
    return MasterSettings(
        field_order=FieldOrderChoice(getattr(args, "field_order", None) or "auto"),
        rate=RateMode(getattr(args, "rate", None) or "double"),
        preset=Preset(preset) if preset else None,
        source_match=not getattr(args, "no_source_match", False),
        lossless=not getattr(args, "no_lossless", False),
        denoise_sigma=float(getattr(args, "denoise", 0.0) or 0.0),
        chroma=ChromaMode(getattr(args, "chroma", None) or "keep"),
        threads=int(getattr(args, "threads", 0) or 0),
        cache_mb=int(getattr(args, "cache_mb", 0) or 0),
    )


def job_from_args(args: argparse.Namespace) -> JobSpec:
    inp = _abs(args.input)
    master_output = _abs(args.output) if getattr(args, "output", None) else None
    if getattr(args, "out_dir", None):
        out_dir = _abs(args.out_dir)
    elif master_output:
        out_dir = os.path.dirname(master_output)
    else:
        out_dir = os.path.dirname(inp)
    settings = master_settings_from_args(args)
    keep_master = not getattr(args, "no_master", False)
    if settings.denoise_sigma < 0:
        raise UsageError("--denoise must be >= 0", "0 disables denoising")
    if settings.threads < 0 or settings.cache_mb < 0:
        raise UsageError("--threads/--cache-mb must be >= 0")
    job = JobSpec(
        schema=JOB_SCHEMA,
        input=inp,
        output_dir=out_dir,
        name_template=getattr(args, "name", None) or "{stem}.deint",
        master=settings,
        views=[ViewCopySpec(profile=v) for v in (getattr(args, "view", None) or [])],
        force=bool(getattr(args, "force", False)),
        overwrite=bool(getattr(args, "overwrite", False)),
        master_output=master_output,
        keep_master=keep_master,
    )
    check_master_choice(job)
    return job


def check_master_choice(job: JobSpec) -> None:
    """``keep_master=False`` (--no-master) rules; UsageError (exit 2) with an actionable hint."""
    if job.keep_master:
        return
    if not job.views:
        raise UsageError(
            "--no-master needs at least one viewing copy",
            "add --view PROFILE (list them with: qtgmc-6ui profiles list), or keep the master",
            reason="no_master_without_view",
        )
    if job.skip_master:
        raise UsageError("--no-master cannot be combined with an existing master (skip_master)", reason="no_master_skip_master")
    if job.master_output:
        raise UsageError(
            "-o names the archival master, but --no-master keeps none",
            "place the viewing copies with --out-dir and --name instead",
            reason="no_master_with_output",
        )


TEMP_MASTER_TAG = ".nomaster."
TEMP_MASTER_SUFFIX = ".tmp.mkv"


def temp_master_path(job: JobSpec, pid: int | None = None) -> str:
    """Hidden temporary master for --no-master: ``<output_dir>/.<name>.nomaster.<pid>.tmp.mkv``
    (same filesystem as the views, never a RAM-backed /tmp)."""
    name = format_name(job.name_template, job.input)
    return os.path.join(job.output_dir, f".{name}{TEMP_MASTER_TAG}{pid or os.getpid()}{TEMP_MASTER_SUFFIX}")


def remove_stale_temp_masters(output_dir: str) -> list[str]:
    """Delete temporary masters (and their .partial files) left by crashed --no-master runs in
    ``output_dir``: those whose owning pid no longer runs.  Returns the removed paths."""
    removed: list[str] = []
    try:
        names = os.listdir(output_dir)
    except OSError:
        return removed
    for n in names:
        if not n.startswith(".") or TEMP_MASTER_TAG not in n:
            continue
        base = n[:-len(".partial")] if n.endswith(".partial") else n
        base = base.lstrip(".")
        if not base.endswith(TEMP_MASTER_SUFFIX):
            continue
        pid_s = base[: -len(TEMP_MASTER_SUFFIX)].rsplit(".", 1)[-1]
        if not pid_s.isdigit() or int(pid_s) == os.getpid() or osdep.process_alive(int(pid_s)):
            continue
        p = os.path.join(output_dir, n)
        try:
            os.unlink(p)
            removed.append(p)
        except OSError:
            pass
    return removed


def estimate_master_bytes(analysis: AnalysisResult, settings: MasterSettings) -> int:
    """Rough ffv1 master size: output frames x raw frame bytes x 0.5 (the reference DV clip
    measures ~0.35); audio is negligible next to it."""
    v = analysis.probe.video
    frames = v.nb_frames or int(round(v.duration_s * float(v.fps))) or 0
    if settings.rate == RateMode.DOUBLE:
        frames *= 2
    pf = v.pix_fmt or ""
    chroma = 3.0 if "444" in pf else 2.0 if "422" in pf else 1.0 if pf.startswith("gray") else 1.5
    bps = 2 if (v.bit_depth or 8) > 8 else 1
    return int(frames * v.width * v.height * chroma * bps * 0.5)


def disk_space_warning(job: JobSpec, analysis: AnalysisResult) -> str:
    """Warning text when the output folder has less free space than the estimated master."""
    import shutil

    d = Path(job.output_dir or os.path.dirname(job.input))
    while not d.exists() and d.parent != d:
        d = d.parent
    try:
        free = shutil.disk_usage(d).free
    except OSError:
        return ""
    need = estimate_master_bytes(analysis, job.master)
    if need <= free:
        return ""
    what = "the archival master" if job.keep_master else "the temporary master"
    return f"{what} needs about {need / 1e9:.1f} GB but only {free / 1e9:.1f} GB is free in {d}"


def read_json_document(stream: IO[str]) -> Any:
    """Read lines until they form one complete JSON document; the rest of ``stream`` stays
    unread (so ``run-job -`` can keep watching stdin for ``cancel`` afterwards)."""
    buf = ""
    dec = json.JSONDecoder()
    for line in stream:
        buf += line
        s = buf.strip()
        if not s:
            continue
        try:
            obj, end = dec.raw_decode(s)
        except ValueError:
            continue
        return obj
    s = buf.strip()
    if not s:
        raise UsageError("no job JSON on stdin", "pipe a JobSpec document (qtgmc-6ui deint ... --save-job -)")
    try:
        return json.loads(s)
    except ValueError as e:
        raise UsageError(f"job JSON is malformed: {e}") from None


def job_from_dict(d: Any, base_dir: str | None = None) -> JobSpec:
    if not isinstance(d, dict):
        raise UsageError("job JSON must be an object")
    schema = d.get("schema", JOB_SCHEMA)
    if schema != JOB_SCHEMA:
        raise UsageError(f"unsupported job schema {schema!r} (this version reads {JOB_SCHEMA})")
    if not d.get("input"):
        raise UsageError("job has no input")
    try:
        job = JobSpec.from_dict(d)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise UsageError(f"invalid job JSON: {e}") from None
    job.input = _abs(job.input, base_dir)
    job.output_dir = _abs(job.output_dir, base_dir) if job.output_dir else os.path.dirname(job.input)
    if job.master_output:
        job.master_output = _abs(job.master_output, base_dir)
    for v in job.views:
        if v.output:
            v.output = _abs(v.output, base_dir)
        if v.profile.endswith(".toml"):
            v.profile = _abs(v.profile, base_dir)
    return job


def load_job(path_or_dash: str, stdin: IO[str] | None = None) -> JobSpec:
    """Read JSON (file or stdin), validate schema, expand ~ and relative paths; UsageError on problems.

    Relative paths are relative to the job file's directory (cwd for stdin)."""
    if path_or_dash == "-":
        return job_from_dict(read_json_document(stdin or sys.stdin), os.getcwd())
    p = _abs(path_or_dash)
    try:
        text = Path(p).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise UsageError(f"job file not found: {p}") from None
    except OSError as e:
        raise UsageError(f"cannot read job file {p}: {e.strerror}") from None
    try:
        d = json.loads(text)
    except ValueError as e:
        raise UsageError(f"job file {p} is not valid JSON: {e}") from None
    return job_from_dict(d, os.path.dirname(p))


def dump_job(job: JobSpec) -> str:
    return json.dumps(job.to_dict(), indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- output planning
def format_name(template: str, input_path: str) -> str:
    stem = Path(input_path).stem
    try:
        name = template.format(stem=stem, name=Path(input_path).name)
    except (KeyError, IndexError, ValueError) as e:
        raise UsageError(f"bad --name template {template!r}: {e}", "only {stem} and {name} are available") from None
    reason = osdep.invalid_name_reason(name)
    if reason:
        raise UsageError(f"bad --name template {template!r}: gives {name!r}", f"the file name {reason}")
    return name


def master_path(job: JobSpec) -> str:
    """``<output_dir>/<name_template formatted with stem>.mkv`` unless job.master_output."""
    if job.master_output:
        return job.master_output
    return os.path.join(job.output_dir, format_name(job.name_template, job.input) + MASTER_EXT)


def view_path(job: JobSpec, view: ViewCopySpec, profile: Any) -> str:
    """``<output_dir>/<template>.<profile.id>.<profile.extension>`` unless view.output."""
    if view.output:
        return view.output
    from . import viewcopy  # single source of truth for viewing-copy names (WP-C)

    return viewcopy.view_output_path(job.input, profile, job.name_template, job.output_dir)


def resolve_profiles(job: JobSpec) -> list[Any]:
    from . import profiles

    return [profiles.load(v.profile) for v in job.views]


def plan_outputs(job: JobSpec, profs: list[Any] | None = None) -> list[str]:
    """Fix every output path in the job (master_output, views[i].output) and return the KEPT
    outputs in stage order, so the ``start`` event, pipeline and cleanup agree.  Without a
    kept master (keep_master=False) master_output stays None and only views are listed."""
    check_master_choice(job)
    if job.keep_master:
        job.master_output = master_path(job)
    if profs is None:
        profs = resolve_profiles(job) if job.views else []
    for v, prof in zip(job.views, profs, strict=True):
        v.output = view_path(job, v, prof)
    head = [job.master_output] if job.keep_master and job.master_output else []
    return head + [v.output or "" for v in job.views]


def same_file(a: str, b: str) -> bool:
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.realpath(a) == os.path.realpath(b)


def _writable_dir(d: str) -> bool:
    p = Path(d)
    while not p.exists():
        if p.parent == p:
            return False
        p = p.parent
    return osdep.dir_writable(p)


def job_problems(job: JobSpec, outputs: list[str] | None = None) -> list[QtgmcError]:
    """Structured version of validate_job (first one is raised by check_job)."""
    probs: list[QtgmcError] = []
    try:
        check_master_choice(job)
    except UsageError as e:
        probs.append(e)
    if not os.path.exists(job.input):
        probs.append(InputError(ErrorCode.INPUT_NOT_FOUND, f"input not found: {job.input}", "check the path"))
    elif not os.path.isfile(job.input):
        probs.append(InputError(ErrorCode.INPUT_UNREADABLE, f"input is not a regular file: {job.input}"))
    elif not os.access(job.input, os.R_OK):
        probs.append(InputError(ErrorCode.INPUT_UNREADABLE, f"input is not readable: {job.input}", "check permissions"))
    if outputs is None:
        outputs = [master_path(job)] + [v.output for v in job.views if v.output]
    seen: dict[str, str] = {}
    for out in outputs:
        if not out:
            continue
        is_master = out == job.master_output
        if same_file(out, job.input):
            probs.append(
                UsageError(
                    f"output would overwrite the input: {out}",
                    "the original is never modified; choose another -o/--out-dir/--name",
                    output=out,
                )
            )
            continue
        key = os.path.realpath(out)
        if key in seen:
            probs.append(UsageError(f"two outputs share one path: {out}", "give the views different profiles or -o paths"))
        seen[key] = out
        if job.skip_master and is_master:
            if not os.path.isfile(out):
                probs.append(InputError(ErrorCode.INPUT_NOT_FOUND, f"master not found: {out}", "skip_master needs an existing master_output"))
            continue
        if os.path.isdir(out):
            probs.append(OutputError(ErrorCode.OUTPUT_EXISTS, f"output is a directory: {out}"))
        elif os.path.exists(out) and not job.overwrite:
            probs.append(OutputError(ErrorCode.OUTPUT_EXISTS, f"output exists: {out}", "choose another output name, or allow overwriting (--overwrite)", {"output": out}))
        elif not _writable_dir(os.path.dirname(out) or "."):
            probs.append(OutputError(ErrorCode.OUTPUT_NOT_WRITABLE, f"cannot write to {os.path.dirname(out)}", "choose a writable --out-dir"))
    return probs


def validate_job(job: JobSpec) -> list[str]:
    """Problem lines: input exists, output dir writable, profiles resolvable, outputs not existing
    unless overwrite."""
    lines: list[str] = []
    profs = None
    if job.views:
        try:
            profs = resolve_profiles(job)
        except QtgmcError as e:
            lines.append(e.message)
    outputs = [master_path(job)] if job.keep_master else []
    if profs is not None:
        outputs += [view_path(job, v, p) for v, p in zip(job.views, profs, strict=True)]
    lines += [e.message for e in job_problems(job, outputs)]
    return lines


def check_job(job: JobSpec, outputs: list[str]) -> None:
    probs = job_problems(job, outputs)
    if probs:
        first = probs[0]
        if len(probs) > 1:
            first.details.setdefault("problems", [p.message for p in probs])
        raise first


# --------------------------------------------------------------------------- blocking rules
def effective_field_order(analysis: AnalysisResult, settings: MasterSettings) -> FieldOrder | None:
    if settings.field_order != FieldOrderChoice.AUTO:
        return FieldOrder(settings.field_order.value)
    return analysis.classification.field_order


def check_blocked(analysis: AnalysisResult, settings: MasterSettings, force: bool) -> list[str]:
    """CLI pre-flight of the blocking rules (pipeline.check_blocked applies the same rules).

    * telecined / mixed / progressive: blocked unless ``force`` (returns a warning line then)
    * a field order is always required: an explicit --field-order, or one from the analysis;
      UNDETERMINED (and forced content without a detected order) is refused without one.
    Returns the warning lines for forced overrides."""
    cls = analysis.classification
    rec = analysis.recommendation
    warnings: list[str] = []
    blocks = {
        ContentClass.TELECINED: (ErrorCode.BLOCKED_TELECINED, "telecined (3:2 pulldown)"),
        ContentClass.MIXED: (ErrorCode.BLOCKED_MIXED, "mixed telecined/interlaced"),
        ContentClass.PROGRESSIVE: (ErrorCode.BLOCKED_PROGRESSIVE, "progressive"),
    }
    if cls.content in blocks or rec.action in (
        RecommendedAction.BLOCK_TELECINED,
        RecommendedAction.BLOCK_MIXED,
        RecommendedAction.SKIP_PROGRESSIVE,
    ):
        code, what = blocks.get(cls.content, (ErrorCode.BLOCKED_TELECINED, str(cls.content)))
        if rec.action == RecommendedAction.BLOCK_MIXED:
            code, what = blocks[ContentClass.MIXED]
        elif rec.action == RecommendedAction.SKIP_PROGRESSIVE:
            code, what = blocks[ContentClass.PROGRESSIVE]
        elif rec.action == RecommendedAction.BLOCK_TELECINED:
            code, what = blocks[ContentClass.TELECINED]
        if not force or not rec.force_allowed:
            hint = "you can deinterlace it anyway with force (--force), not recommended" if rec.force_allowed else ""
            if code != ErrorCode.BLOCKED_PROGRESSIVE:
                ivtc = "inverse telecine (IVTC) it with another tool"
                hint = f"{ivtc}; {hint}" if hint else ivtc
            raise BlockedError(
                code,
                rec.message or f"content detected as {what}; not deinterlacing",
                hint,
                {"content": str(cls.content), "action": str(rec.action), "confidence": cls.confidence},
            )
        warnings.append(f"--force: processing content detected as {what}; results may be inferior")
    fo = effective_field_order(analysis, settings)
    if fo is None:
        raise BlockedError(
            ErrorCode.BLOCKED_UNDETERMINED,
            "field order could not be determined" + (f" (content: {cls.content})" if cls.content != ContentClass.UNDETERMINED else ""),
            "check the fields preview, then choose the field order explicitly (--field-order tff|bff)",
            {"content": str(cls.content)},
        )
    if settings.field_order != FieldOrderChoice.AUTO and cls.field_order and cls.field_order != fo:
        warnings.append(f"field order override: {fo} given, analysis detected {cls.field_order}")
    return warnings
