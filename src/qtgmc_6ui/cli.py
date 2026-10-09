"""Command-line interface (WP-D).  The argument contract is fixed here; subcommand bodies
are dispatched lazily so importing this module never imports vapoursynth or Qt.

  qtgmc-6ui [gui]
  qtgmc-6ui analyze INPUT [--json|--events] [--full] [--windows N] [--window-frames W] [--no-cache]
  qtgmc-6ui deint INPUT [-o OUT] [--out-dir DIR] [--name TEMPLATE] [--field-order auto|tff|bff]
            [--rate double|single] [--preset P] [--no-lossless] [--no-source-match]
            [--denoise SIGMA] [--chroma keep|422] [--view PROFILE ...] [--force] [--overwrite]
            [--threads N] [--cache-mb N] [--json|--events] [--dry-run] [--save-job FILE]
  qtgmc-6ui run-job JOB.json|-  [--json|--events]
  qtgmc-6ui view MASTER --profile P [-o OUT] [--overwrite] [--json|--events]
  qtgmc-6ui profiles list|show ID|validate ID_OR_PATH|export ID [-o FILE]|path [--json]
  qtgmc-6ui encoders [--json] [--probe] [--container C] [--encoder E]
  qtgmc-6ui doctor [--json] [--no-load] [--no-smoke]
  qtgmc-6ui preview-frame INPUT N --mode source|fields|output [--field-order tff|bff] [--preset P]
            [--no-lossless] [--window N] -o OUT.png|- [--json]
  qtgmc-6ui script INPUT [deint options]           prints the equivalent .vpy
  qtgmc-6ui version [--json]

Exit codes: errors.ExitCode.  ``--events`` = JSON lines on stdout (events.py); ``--json`` =
one JSON document (result) on stdout; otherwise human-readable text.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import __version__
from .errors import ExitCode, QtgmcError
from .models import ChromaMode, FieldOrderChoice, Preset, PreviewMode, RateMode


def _output_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    g.add_argument("--events", action="store_true", help="JSON-lines progress events on stdout")


def _deint_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--field-order", choices=[c.value for c in FieldOrderChoice], default="auto")
    p.add_argument("--rate", choices=[c.value for c in RateMode], default="double")
    p.add_argument("--preset", choices=[c.value for c in Preset], default=None, help="default: by resolution")
    p.add_argument("--no-lossless", action="store_true")
    p.add_argument("--no-source-match", action="store_true")
    p.add_argument("--denoise", type=float, default=0.0, metavar="SIGMA", help="DFTTest sigma; alters the image")
    p.add_argument("--chroma", choices=[c.value for c in ChromaMode], default="keep")
    p.add_argument("--threads", type=int, default=0)
    p.add_argument("--cache-mb", type=int, default=0)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qtgmc-6ui", description="QTGMC deinterlacer (VapourSynth) with a Qt6 GUI")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("gui", help="launch the GUI (default)")

    a = sub.add_parser("analyze", help="probe + idet + telecine detection + classification")
    a.add_argument("input")
    a.add_argument("--full", action="store_true", help="scan every frame instead of sampled windows")
    a.add_argument("--windows", type=int, default=None)
    a.add_argument("--window-frames", type=int, default=None)
    a.add_argument("--no-cache", action="store_true")
    _output_flags(a)

    d = sub.add_parser("deint", help="create the archival master (and viewing copies)")
    d.add_argument("input")
    d.add_argument("-o", "--output", default=None, help="master path (default <out-dir>/<name>.mkv)")
    d.add_argument("--out-dir", default=None)
    d.add_argument("--name", default="{stem}.deint", help="name template, {stem} = input stem")
    _deint_options(d)
    d.add_argument("--view", action="append", default=[], metavar="PROFILE", help="viewing-copy profile id or .toml")
    d.add_argument("--force", action="store_true", help="process despite telecine/mixed/progressive detection")
    d.add_argument("--overwrite", action="store_true")
    d.add_argument("--dry-run", action="store_true", help="analyze, print the plan and commands, do nothing")
    d.add_argument("--save-job", default=None, metavar="FILE", help="write the JobSpec JSON and exit")
    _output_flags(d)

    r = sub.add_parser("run-job", help="run a JobSpec JSON (file or - for stdin)")
    r.add_argument("job")
    _output_flags(r)

    v = sub.add_parser("view", help="encode a viewing copy from an existing master")
    v.add_argument("master")
    v.add_argument("--profile", required=True)
    v.add_argument("-o", "--output", default=None)
    v.add_argument("--overwrite", action="store_true")
    _output_flags(v)

    pr = sub.add_parser("profiles", help="list/show/validate/export viewing-copy profiles")
    pr.add_argument("action", choices=["list", "show", "validate", "export", "path"])
    pr.add_argument("target", nargs="?")
    pr.add_argument("-o", "--output", default=None)
    pr.add_argument("--json", action="store_true")

    e = sub.add_parser("encoders", help="detected containers/encoders and optional tools")
    e.add_argument("--probe", action="store_true", help="probe-mux every container x encoder pair now")
    e.add_argument("--container", default=None)
    e.add_argument("--encoder", default=None)
    e.add_argument("--json", action="store_true")

    do = sub.add_parser("doctor", help="check components, ABI, plugins; paste into bug reports")
    do.add_argument("--no-load", action="store_true", help="skip creating a core and loading plugins")
    do.add_argument("--no-smoke", action="store_true", help="skip the QTGMC smoke run")
    do.add_argument("--json", action="store_true")

    pv = sub.add_parser("preview-frame", help="render one source/fields/output frame as PNG")
    pv.add_argument("input")
    pv.add_argument("frame", type=int)
    pv.add_argument("--mode", choices=[c.value for c in PreviewMode], default="source")
    pv.add_argument("--field-order", choices=["tff", "bff"], default=None)
    pv.add_argument("--preset", choices=[c.value for c in Preset], default=None)
    pv.add_argument("--no-lossless", action="store_true")
    pv.add_argument("--window", type=int, default=10)
    pv.add_argument("-o", "--output", required=True, help="PNG path or - for stdout")
    pv.add_argument("--json", action="store_true")

    sc = sub.add_parser("script", help="print the equivalent VapourSynth script")
    sc.add_argument("input")
    _deint_options(sc)

    ve = sub.add_parser("version")
    ve.add_argument("--json", action="store_true")
    return p


def cmd_version(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps({"version": __version__}))
    else:
        print(f"qtgmc-6ui {__version__}")
    return 0


def dispatch(args: argparse.Namespace) -> int:
    """Lazy imports keep startup fast and keep vapoursynth/Qt out of non-processing commands."""
    cmd = args.command or "gui"
    if cmd == "version":
        return cmd_version(args)
    if cmd == "gui":
        from .gui.app import main as gui_main

        return gui_main([])
    from . import commands  # WP-D: one function per subcommand

    handler = getattr(commands, "cmd_" + cmd.replace("-", "_"))
    return int(handler(args))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return dispatch(args)
    except QtgmcError as e:
        if getattr(args, "events", False):
            from .events import JsonLinesSink, Emitter

            em = Emitter(JsonLinesSink())
            em.error(e.to_dict())
            em.done(e.exit_code)
        elif getattr(args, "json", False):
            print(json.dumps({"error": e.to_dict()}))
        else:
            print(str(e), file=sys.stderr)
        return int(e.exit_code)
    except KeyboardInterrupt:
        return int(ExitCode.CANCELLED)


if __name__ == "__main__":
    sys.exit(main())
