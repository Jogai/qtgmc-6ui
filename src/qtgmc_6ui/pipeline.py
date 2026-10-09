"""Master pipeline (WP-B): analysis -> graph -> in-process y4m -> ffmpeg stdin -> master.mkv.

Execution model (verified): ``graph.clip.output(ffmpeg.stdin, y4m=True, progress_update=cb)``
in a worker thread; the main thread owns the CancelToken (SIGTERM/SIGINT/stdin
"cancel").  Cancel = SIGTERM to ffmpeg -> output() raises BrokenPipeError ->
partial file removed -> CancelledError.  ffmpeg stderr is drained on a thread
and forwarded as log events (level warning/error by prefix).
"""

from __future__ import annotations

from .events import Emitter
from .models import AnalysisResult, JobSpec, JobResult, MasterSettings, OutputFile
from .runtime import CancelToken


def check_blocked(analysis: AnalysisResult, settings: MasterSettings, force: bool) -> None:
    """Raise BlockedError (BLOCKED_TELECINED/MIXED/PROGRESSIVE/UNDETERMINED) unless ``force``
    or, for UNDETERMINED, an explicit field order is given."""
    raise NotImplementedError


def run_master(
    job: JobSpec,
    analysis: AnalysisResult,
    output: str,
    *,
    emitter: Emitter,
    cancel: CancelToken,
) -> OutputFile:
    """Deinterlace ``job.input`` into ``output`` (ffv1/flac mkv).  Emits stage setup/master
    with per-frame progress, verifies the result with ffprobe (frames, fps, SAR, colour)."""
    raise NotImplementedError


def run_job(job: JobSpec, *, emitter: Emitter, cancel: CancelToken) -> JobResult:
    """analyze (unless job.analysis) -> check_blocked -> run_master -> viewcopy.run_view for each
    view -> JobResult.  Views always read from the master, never from the original."""
    raise NotImplementedError


def master_output_path(job: JobSpec) -> str:
    """``<output_dir>/<name_template formatted with stem>.mkv`` unless job.master_output."""
    raise NotImplementedError
