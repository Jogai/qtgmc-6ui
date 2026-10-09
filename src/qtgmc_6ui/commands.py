"""Subcommand implementations (WP-D).  ``cli.dispatch`` calls ``cmd_<name>(args)``.

Each handler: pick the sink (JsonLinesSink for --events, TextSink otherwise), build an
Emitter, install signal handlers + stdin cancel watcher for long commands, run, emit
``result`` + ``done``, return the exit code.
"""

from __future__ import annotations

import argparse


def cmd_analyze(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_deint(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_run_job(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_view(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_profiles(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_encoders(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_doctor(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_preview_frame(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_script(args: argparse.Namespace) -> int:
    raise NotImplementedError
