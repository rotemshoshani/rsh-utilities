"""Front command: ask what is missing, confirm, run, report.

Every flag suppresses exactly the question it answers, so a fully-flagged
invocation with --yes is non-interactive and a partial one still asks for the
rest.
"""

import argparse
import os
import sys

from . import display
from . import herdr as herdr_cli
from . import picker
from . import runner as runner_mod
from .duration import DurationError, parse_duration

DEFAULT_PROMPT = "Go ahead and implement the approved plan."
DEFAULT_START = "5m"
DEFAULT_GAP = "20m"
DEFAULT_HOLD_CAP = "30m"

EXIT_USAGE = 2
EXIT_ENV = 3


class UsageError(Exception):
    pass


class Aborted(Exception):
    """The operator said no, or closed the input."""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="herdr-stagger",
        description=(
            "Release plan-ready agents one at a time: pick the panes in fire "
            "order, set the delay before the first and the gaps between the "
            "rest, walk away."
        ),
        epilog=(
            "examples:\n"
            "  herdr-stagger\n"
            "  herdr-stagger --workspace wQ\n"
            "  herdr-stagger --pane wQ:p5Z --pane wQ:p64 --start 5m --gap 20m --yes\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--pane", action="append", metavar="ID", default=[],
        help="pane to release; repeat once per pane, in fire order",
    )
    parser.add_argument("--start", metavar="DUR", help="delay before the first send")
    parser.add_argument(
        "--gap", action="append", metavar="DUR", default=[],
        help="delay between sends; one value for all, or one per gap",
    )
    parser.add_argument("--prompt", metavar="TEXT", help="text sent to every pane")
    parser.add_argument(
        "--hold-cap", metavar="DUR", dest="hold_cap",
        help="give up on a pane that is not promptable after this (default %s)"
        % DEFAULT_HOLD_CAP,
    )
    parser.add_argument("--workspace", metavar="ID", help="only offer this workspace")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    parser.add_argument(
        "--no-notify", action="store_true", help="no desktop notification at the end"
    )
    return parser


def require_herdr():
    if os.environ.get("HERDR_ENV") != "1":
        raise EnvironmentError(
            "herdr-stagger must run inside a Herdr-managed pane (HERDR_ENV is not 1).\n"
            "It drives other panes through the herdr CLI; outside a session there "
            "is nothing to drive."
        )


def _duration(value, label):
    try:
        return parse_duration(value, allow_zero=True)
    except DurationError as exc:
        raise UsageError("%s: %s" % (label, exc))


def ask(in_stream, out, question, default=None):
    """One question, with its default shown. EOF aborts the run."""
    suffix = " [%s]" % default if default is not None else ""
    print("%s%s: " % (question, suffix), end="", file=out, flush=True)
    line = in_stream.readline()
    if not line:
        raise Aborted("end of input")
    answer = line.strip()
    return answer if answer else (default if default is not None else "")


def ask_until_valid(in_stream, out, question, default, parse):
    """Re-ask rather than dying: a typo should not cost the whole setup."""
    while True:
        answer = ask(in_stream, out, question, default)
        try:
            return parse(answer)
        except picker.PickerError as exc:
            print("  %s" % exc, file=out)


def select_panes(args, rows, in_stream, out):
    if not args.pane and args.yes:
        raise UsageError("--yes needs --pane: there is no default set of panes")

    if args.pane:
        by_id = {row["pane_id"]: row for row in rows}
        picked = []
        for pane_id in args.pane:
            row = by_id.get(pane_id)
            if row is None:
                raise UsageError("no live agent in pane %s" % pane_id)
            if row["is_self"]:
                raise UsageError("pane %s is this pane" % pane_id)
            if row in picked:
                raise UsageError("pane %s given twice" % pane_id)
            picked.append(row)
        return picked

    print(display.candidate_table(rows), file=out)
    print("", file=out)
    return ask_until_valid(
        in_stream, out, "Pick panes in fire order", None,
        lambda answer: picker.parse_selection(answer, rows),
    )


def collect_plan(args, rows, in_stream, out):
    """Flags answer questions; --yes answers the rest with their defaults."""
    asking = not args.yes
    picked = select_panes(args, rows, in_stream, out)

    if args.start is not None:
        start = _duration(args.start, "--start")
    elif asking:
        start = ask_until_valid(
            in_stream, out, "First send in", DEFAULT_START,
            lambda answer: picker.parse_gaps(answer, 1)[0],
        )
    else:
        start = _duration(DEFAULT_START, "--start")

    needed = len(picked) - 1
    if args.gap:
        gaps = _gaps_from_flags(args.gap, needed)
    elif not needed:
        gaps = []
    elif asking:
        gaps = ask_until_valid(
            in_stream, out, "Gap between sends", DEFAULT_GAP,
            lambda answer: picker.parse_gaps(answer, needed),
        )
    else:
        gaps = picker.parse_gaps(DEFAULT_GAP, needed)

    if args.prompt is not None:
        text = args.prompt
    elif asking:
        text = ask(in_stream, out, "Prompt to send", DEFAULT_PROMPT)
    else:
        text = DEFAULT_PROMPT
    if not text.strip():
        raise UsageError("the prompt cannot be empty")

    try:
        return picker.build_plan(picked, start, gaps, text)
    except picker.PickerError as exc:
        raise UsageError(str(exc))


def _gaps_from_flags(values, needed):
    try:
        return picker.parse_gaps(" ".join(values), needed)
    except picker.PickerError as exc:
        raise UsageError("--gap: %s" % exc)


def confirm(steps, args, in_stream, out):
    print("", file=out)
    print(display.plan_table(steps), file=out)
    print("", file=out)
    if args.yes:
        return
    answer = ask(in_stream, out, "Start? [y/N]")
    if answer.strip().lower() not in ("y", "yes"):
        raise Aborted("declined")


def execute(steps, args, out, hd):
    hold_cap = _duration(args.hold_cap or DEFAULT_HOLD_CAP, "--hold-cap")
    live = display.Display(steps, out, tty=hasattr(out, "isatty") and out.isatty())
    run = runner_mod.Runner(steps, hd, observer=live, hold_cap_seconds=hold_cap)
    results = run.run()

    print("", file=out)
    text = display.summary(steps, results)
    print(text, file=out)

    if not args.no_notify:
        hd.notification_show("herdr-stagger finished", text.splitlines()[0])

    return 0 if all(r.outcome == runner_mod.SENT for r in results) else 1


def main(argv=None, out=None, in_stream=None, err=None, hd=herdr_cli):
    argv = sys.argv[1:] if argv is None else argv
    out = out or sys.stdout
    in_stream = in_stream or sys.stdin
    err = err or sys.stderr

    try:
        require_herdr()
    except EnvironmentError as exc:
        print("herdr-stagger: %s" % exc, file=err)
        return EXIT_ENV

    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code or EXIT_USAGE

    try:
        agents = hd.agent_list()
    except hd.HerdrError as exc:
        print("herdr-stagger: %s" % exc, file=err)
        return 1

    rows = picker.candidates(
        agents,
        workspace=args.workspace,
        self_pane=os.environ.get("HERDR_PANE_ID"),
    )
    if not any(not row["is_self"] for row in rows):
        print("herdr-stagger: no other agent is running%s."
              % (" in %s" % args.workspace if args.workspace else ""), file=out)
        return 1

    try:
        steps = collect_plan(args, rows, in_stream, out)
        confirm(steps, args, in_stream, out)
    except KeyboardInterrupt:
        print("", file=out)
        print("[herdr-stagger] aborted (interrupted).", file=out)
        return 0
    except UsageError as exc:
        print("herdr-stagger: %s" % exc, file=err)
        return EXIT_USAGE
    except Aborted as exc:
        print("[herdr-stagger] aborted (%s)." % exc, file=out)
        return 0

    return execute(steps, args, out, hd)
