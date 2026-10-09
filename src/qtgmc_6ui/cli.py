"""Command-line interface (WP-D).  The argument contract is fixed here; subcommand bodies
are dispatched lazily so importing this module never imports vapoursynth or Qt.

  qtgmc-6ui [gui [FILE ...]]
  qtgmc-6ui analyze INPUT [--json|--events] [--full] [--windows N] [--window-frames W] [--no-cache]
  qtgmc-6ui deint INPUT [-o OUT] [--out-dir DIR] [--name TEMPLATE] [--field-order auto|tff|bff]
            [--rate double|single] [--preset P] [--no-lossless] [--no-source-match]
            [--denoise SIGMA] [--chroma keep|422] [--view PROFILE ...] [--force] [--overwrite]
            [--threads N] [--cache-mb N] [--json|--events] [--dry-run] [--save-job FILE]
  qtgmc-6ui run-job JOB.json|-  [--json|--events] [--dry-run]
  qtgmc-6ui view MASTER --profile P [-o OUT] [--overwrite] [--json|--events]
  qtgmc-6ui profiles list [--all]|show ID|validate ID_OR_PATH|export ID [-o FILE]|path [--json]
  qtgmc-6ui profiles save FILE.toml|FILE.json|- [--id ID] [--overwrite] [--json]
  qtgmc-6ui profiles delete ID [--json]
  qtgmc-6ui encoders [--json] [--probe] [--container C] [--encoder E] [--kind video|audio] [--common]
  qtgmc-6ui doctor [--json] [--no-load] [--no-smoke]
  qtgmc-6ui preview-frame INPUT N --mode source|fields|output [--field-order tff|bff] [--preset P]
            [--no-lossless] [--window N] [--rate double|single] -o OUT.png|- [--json]
  qtgmc-6ui preview-info INPUT [--json]            frames/fps/size for the scrubber
  qtgmc-6ui preview-serve INPUT                    JSON-lines preview server (see preview.serve)
  qtgmc-6ui script INPUT [deint options]           prints the equivalent .vpy
  qtgmc-6ui version [--json]

Exit codes: errors.ExitCode.  ``--events`` = JSON lines on stdout (events.py); ``--json`` =
one JSON document (result) on stdout; otherwise human-readable text.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from . import __version__
from .errors import ExitCode, QtgmcError
from .models import ChromaMode, FieldOrderChoice, Preset, PreviewMode, RateMode

EPILOG = """examples:
  qtgmc-6ui analyze clip.avi
  qtgmc-6ui deint clip.avi --view mp4-h264-aac
  qtgmc-6ui doctor

exit codes: 0 ok, 1 error, 2 usage, 3 blocked (telecined/mixed/progressive/field order unknown),
            4 component (plugin/ABI/tool), 5 input, 6 encode, 7 output, 130 cancelled"""


class _Parser(argparse.ArgumentParser):
    """Keeps argparse's message so --json/--events errors carry it (not just "invalid command line")."""

    last_error: str = ""

    def error(self, message: str):  # type: ignore[override]
        _Parser.last_error = f"{self.prog}: {message}"
        super().error(message)


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
    p = _Parser(
        prog="qtgmc-6ui",
        description="QTGMC deinterlacer (VapourSynth) with a Qt6 GUI",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")

    g = sub.add_parser("gui", help="launch the GUI (default)")
    g.add_argument("files", nargs="*", help="files to add to the queue")

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
    r.add_argument("--dry-run", action="store_true", help="analyze, print the plan and commands, do nothing")
    _output_flags(r)

    v = sub.add_parser("view", help="encode a viewing copy from an existing master")
    v.add_argument("master")
    v.add_argument("--profile", required=True)
    v.add_argument("-o", "--output", default=None)
    v.add_argument("--overwrite", action="store_true")
    _output_flags(v)

    pr = sub.add_parser("profiles", help="list/show/validate/export/save/delete viewing-copy profiles")
    pr.add_argument("action", choices=["list", "show", "validate", "export", "path", "save", "delete"])
    pr.add_argument("target", nargs="?", help="profile id or .toml path; for save: FILE.toml|FILE.json|- (stdin)")
    pr.add_argument("-o", "--output", default=None)
    pr.add_argument("--id", default=None, help="save: store under this id (overrides the id in the file)")
    pr.add_argument("--overwrite", action="store_true", help="save: replace an existing user profile")
    pr.add_argument("--all", action="store_true", help="list: include profiles whose tools are missing")
    pr.add_argument("--json", action="store_true")

    e = sub.add_parser("encoders", help="detected containers/encoders and optional tools")
    e.add_argument("--probe", action="store_true", help="probe-mux every container x encoder pair now")
    e.add_argument("--container", default=None)
    e.add_argument("--encoder", default=None)
    e.add_argument("--kind", choices=["video", "audio"], default=None, help="only video or audio encoders")
    e.add_argument("--common", action="store_true", help="common containers and software encoders only")
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
    pv.add_argument("--rate", choices=[c.value for c in RateMode], default="double", help="output mode: 2 stacked frames (double) or 1")
    pv.add_argument("-o", "--output", required=True, help="PNG path or - for stdout")
    pv.add_argument("--json", action="store_true")

    pi = sub.add_parser("preview-info", help="frame count / fps / size of the source for the scrubber")
    pi.add_argument("input")
    pi.add_argument("--json", action="store_true")

    ps = sub.add_parser("preview-serve", help="long-lived preview renderer: JSON requests on stdin, event lines on stdout")
    ps.add_argument("input")

    sc = sub.add_parser("script", help="print the equivalent VapourSynth script")
    sc.add_argument("input")
    _deint_options(sc)

    ve = sub.add_parser("version", help="print the version")
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

        return gui_main(list(getattr(args, "files", []) or []))
    from . import commands  # WP-D: one function per subcommand

    handler = getattr(commands, "cmd_" + cmd.replace("-", "_"))
    return int(handler(args))


def _emit_usage_error(argv: Sequence[str], code: int) -> None:
    """argparse already printed usage to stderr; in --events/--json mode also emit the error."""
    from .errors import ErrorCode

    msg = _Parser.last_error or "invalid command line"
    hint = "see qtgmc-6ui --help"
    first = next((a for a in argv if not a.startswith("-")), "")
    if first and "invalid choice" in msg and os.path.exists(first):
        hint = f"did you mean: qtgmc-6ui analyze {first}  /  qtgmc-6ui deint {first}  /  qtgmc-6ui gui {first}"
        if not any(a in ("--json", "--events") for a in argv):
            print(f"hint: {hint}", file=sys.stderr)
    e = QtgmcError(ErrorCode.USAGE, msg, hint, {"argv": list(argv)})
    if "--events" in argv:
        from .events import Emitter, JsonLinesSink

        em = Emitter(JsonLinesSink())
        em.error(e.to_dict())
        em.done(code)
    elif "--json" in argv:
        print(json.dumps({"error": e.to_dict()}))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    argv_list = list(sys.argv[1:] if argv is None else argv)
    _Parser.last_error = ""
    try:
        args = parser.parse_args(argv_list)
    except SystemExit as se:
        code = se.code if isinstance(se.code, int) else int(ExitCode.USAGE)
        if code != 0:
            _emit_usage_error(argv_list, code)
        return code
    try:
        return dispatch(args)
    except BrokenPipeError:
        # reader (GUI, head, ...) went away; nothing sensible left to print
        try:
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - silence the flush at exit
        except OSError:
            pass
        return int(ExitCode.ERROR)
    except (QtgmcError, Exception) as exc:
        if isinstance(exc, QtgmcError):
            e = exc
        else:
            from .commands import internal_error

            e = internal_error(exc)
        if getattr(args, "events", False):
            from .events import Emitter, JsonLinesSink

            em = Emitter(JsonLinesSink())
            em.error(e.to_dict())
            em.done(e.exit_code)
        elif getattr(args, "json", False):
            print(json.dumps({"error": e.to_dict()}))
        else:
            print(f"error {e}", file=sys.stderr)
            if getattr(args, "verbose", False) and e.details.get("traceback"):
                print(e.details["traceback"], file=sys.stderr)
        return int(e.exit_code)
    except KeyboardInterrupt:
        return int(ExitCode.CANCELLED)


if __name__ == "__main__":
    sys.exit(main())
