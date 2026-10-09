"""Job model helpers (WP-D): build a JobSpec from CLI flags, read/write JobSpec JSON.

The queue itself lives in the GUI (gui/queue_model.py); the CLI runs one job per process
(``qtgmc-6ui run-job JOB.json`` or ``-`` for stdin).
"""

from __future__ import annotations

import argparse

from .models import JobSpec


def job_from_args(args: argparse.Namespace) -> JobSpec:
    raise NotImplementedError


def load_job(path_or_dash: str) -> JobSpec:
    """Read JSON (file or stdin), validate schema, expand ~ and relative paths; UsageError on problems."""
    raise NotImplementedError


def dump_job(job: JobSpec) -> str:
    raise NotImplementedError


def validate_job(job: JobSpec) -> list[str]:
    """Problem lines: input exists, output dir writable, profiles resolvable, outputs not existing
    unless overwrite."""
    raise NotImplementedError
