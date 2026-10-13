"""Front command: validate, spawn the detached waiter, print, exit."""

import argparse
import os
import subprocess
import sys
import time
import uuid

from . import herdr as herdr_cli
from . import state as state_mod
from .duration import DurationError, format_duration, parse_duration
from .message import start_banner

DEFAULT_EVERY = "30s"
DEFAULT_DEADLINE = "30m"

EXIT_USAGE = 2
EXIT_ENV = 3


class UsageError(Exception):
    pass


def build_parser():
    parser = argparse.ArgumentParser(
        prog="herdr-delay",
        description=(
            "Wait without burning turns. Schedules a detached waiter that wakes "
            "the calling agent once, with a result."
        ),
        epilog=(
            "examples:\n"
            "  herdr-delay 10m 'check the preflight run'\n"
            "  herdr-delay --in 10m --then 'npm run preflight'\n"
            "  herdr-delay --until 'curl -sf localhost:3000/health' --every 30s --deadline 10m\n"
            "  herdr-delay cancel\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("positional", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument("--in", dest="in_", metavar="DUR", help="wait this long first")
    parser.add_argument("--then", metavar="CMD", help="run CMD once after the wait")
    parser.add_argument("--until", metavar="CMD", help="poll CMD until it exits 0")
    parser.add_argument("--every", metavar="DUR", help="poll interval (default %s)" % DEFAULT_EVERY)
    parser.add_argument(
        "--deadline", metavar="DUR", help="give up polling after this (default %s)" % DEFAULT_DEADLINE
    )
    parser.add_argument("--reason", metavar="TEXT", help="what this wait is for")
    parser.add_argument(
        "--pane", metavar="ID",
        help="pane to wake; only needed when it cannot be resolved automatically",
    )
    return parser


def parse_args(argv):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.until and args.then:
        raise UsageError("--until and --then are mutually exclusive")

    positional = list(args.positional)
    if positional:
        if args.in_:
            raise UsageError("pass a duration positionally or with --in, not both")
        args.in_ = positional.pop(0)
        if positional and not args.reason:
            args.reason = " ".join(positional)
        elif positional:
            raise UsageError("unexpected arguments: %s" % " ".join(positional))

    if not args.until and not args.in_ and not args.then:
        raise UsageError("nothing to wait for: pass a duration, --in, --then, or --until")

    if not args.until and (args.every or args.deadline):
        raise UsageError("--every and --deadline apply to --until only")

    mode = "until" if args.until else ("then" if args.then else "in")
    command = args.until or args.then

    try:
        in_seconds = parse_duration(args.in_) if args.in_ else 0
        every_seconds = parse_duration(args.every or DEFAULT_EVERY) if mode == "until" else 0
        deadline_seconds = (
            parse_duration(args.deadline or DEFAULT_DEADLINE) if mode == "until" else 0
        )
    except DurationError as exc:
        raise UsageError(str(exc))

    return {
        "version": state_mod.VERSION,
        "pane": args.pane,
        "mode": mode,
        "command": command,
        "reason": args.reason,
        "in_seconds": in_seconds,
        "every_seconds": every_seconds,
        "deadline_seconds": deadline_seconds,
    }


def require_herdr():
    if os.environ.get("HERDR_ENV") != "1":
        raise EnvironmentError(
            "herdr-delay must run inside a Herdr-managed pane (HERDR_ENV is not 1).\n"
            "There is no fallback: a blocking sleep would pin the agent's turn, "
            "which is the thing this tool exists to avoid."
        )


def _ancestry():
    """argv[0] basenames from this process up to init."""
    names = []
    pid = os.getpid()
    for _ in range(40):
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as handle:
                argv0 = handle.read().split(b"\0")[0].decode("utf-8", "replace")
            with open("/proc/%d/stat" % pid) as handle:
                pid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if argv0:
            names.append(os.path.basename(argv0))
        if pid <= 1:
            break
    return names


def caller_agent_kind(agents):
    """Which kind of agent invoked us, read off our own process ancestry.

    Codex's app-server breaks the pane environment but not this: the shell
    command still runs underneath a process named `codex`.
    """
    kinds = {a.get("agent") for a in agents if a.get("agent")}
    for name in _ancestry():
        if name in kinds:
            return name
    return None


def _same_cwd(agent, cwd):
    for key in ("foreground_cwd", "cwd"):
        value = agent.get(key)
        if value and os.path.realpath(value) == cwd:
            return True
    return False


def resolve_pane(hd, explicit=None):
    """Find the pane to wake, and report how we found it.

    $HERDR_PANE_ID is the fast path but cannot be trusted on its own. Codex
    delegates shell commands to a long-lived app-server process whose
    environment belongs to whichever pane first launched it -- frequently a
    pane that no longer exists -- so the variable can name a dead pane while
    the agent is very much alive somewhere else.
    """
    agents = hd.agent_list()
    by_pane = {a.get("pane_id"): a for a in agents}

    if explicit:
        if explicit not in by_pane:
            raise EnvironmentError("no agent is running in pane %s" % explicit)
        return explicit, "explicit"

    env_pane = os.environ.get("HERDR_PANE_ID")
    if env_pane and env_pane in by_pane:
        return env_pane, "environment"

    if env_pane:
        try:
            live = env_pane in hd.pane_ids()
        except hd.HerdrError:
            live = False
        if live:
            raise EnvironmentError(
                "pane %s has no agent to wake" % env_pane
            )

    cwd = os.path.realpath(os.getcwd())
    same_cwd = [a for a in agents if _same_cwd(a, cwd)]
    kind = caller_agent_kind(agents)

    def working(pool):
        return [a for a in pool if a.get("agent_status") == "working"]

    pools = []
    if kind:
        kin = [a for a in same_cwd if a.get("agent") == kind]
        pools += [(working(kin), "kind+cwd+working"), (kin, "kind+cwd")]
        all_kin = [a for a in agents if a.get("agent") == kind]
        pools.append((working(all_kin), "kind+working"))
    pools += [(working(same_cwd), "cwd+working"), (same_cwd, "cwd")]

    for candidates, how in pools:
        if len(candidates) == 1:
            return candidates[0]["pane_id"], how

    detail = ", ".join(
        "%s (%s, %s)" % (a.get("pane_id"), a.get("agent"), a.get("agent_status"))
        for a in same_cwd
    ) or "none"
    raise EnvironmentError(
        "cannot tell which pane to wake.\n"
        "  HERDR_PANE_ID : %s\n"
        "  caller kind   : %s\n"
        "  agents in %s : %s\n"
        "Pass --pane <id> explicitly."
        % (env_pane or "unset", kind or "unknown", cwd, detail)
    )


def cmd_cancel(pane, out):
    existing = state_mod.live_state(pane)
    if not existing:
        print("[herdr-delay] no waiter running for pane %s." % pane, file=out)
        return 0
    state_mod.kill_waiter(existing)
    state_mod.clear_state(pane)
    print("[herdr-delay] cancelled the waiter for pane %s." % pane, file=out)
    return 0


def cmd_status(pane, out):
    existing = state_mod.live_state(pane)
    if not existing:
        print("[herdr-delay] no waiter running for pane %s." % pane, file=out)
        return 0
    age = int(time.time() - existing["started_unix"])
    print("[herdr-delay] waiter running for pane %s (pid %d, started %s ago)"
          % (pane, existing["waiter_pid"], format_duration(age)), file=out)
    if existing.get("command"):
        print("  command : %s" % existing["command"], file=out)
    print("  log     : %s" % state_mod.log_path(pane), file=out)
    return 0


def spawn_waiter(state):
    path = state_mod.write_state(state)
    log = open(state_mod.log_path(state["pane_id"]), "a")
    proc = subprocess.Popen(
        [sys.executable, "-m", "herdr_delay", "__waiter", path],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=os.getcwd(),
        env=os.environ.copy(),
    )
    state["waiter_pid"] = proc.pid
    state_mod.write_state(state)
    return proc.pid


def main(argv=None, out=None, hd=herdr_cli):
    if argv is None:
        argv = sys.argv[1:]
    out = out or sys.stdout

    if argv and argv[0] == "__waiter":
        from .waiter import main as waiter_main

        return waiter_main(argv[1:])

    try:
        require_herdr()
    except EnvironmentError as exc:
        print("herdr-delay: %s" % exc, file=sys.stderr)
        return EXIT_ENV

    if argv and argv[0] in ("cancel", "status"):
        if len(argv) > 1:
            print("herdr-delay: %s takes no arguments" % argv[0], file=sys.stderr)
            return EXIT_USAGE
        try:
            pane, _ = resolve_pane(hd)
        except (EnvironmentError, hd.HerdrError) as exc:
            print("herdr-delay: %s" % exc, file=sys.stderr)
            return EXIT_ENV
        return cmd_cancel(pane, out) if argv[0] == "cancel" else cmd_status(pane, out)

    try:
        state = parse_args(argv)
    except UsageError as exc:
        print("herdr-delay: %s" % exc, file=sys.stderr)
        return EXIT_USAGE

    pane_override = state.pop("pane")
    try:
        pane, how = resolve_pane(hd, pane_override)
        agent_pid = hd.foreground_group_id(pane)
    except (EnvironmentError, hd.HerdrError) as exc:
        print("herdr-delay: %s" % exc, file=sys.stderr)
        return EXIT_ENV

    state.update({
        "pane_id": pane,
        "workspace_id": os.environ.get("HERDR_WORKSPACE_ID", ""),
        "agent_pid": agent_pid,
        "marker": uuid.uuid4().hex[:4],
        "started_unix": time.time(),
        "cwd": os.getcwd(),
    })

    replaced = state_mod.live_state(pane)
    if replaced:
        state_mod.kill_waiter(replaced)

    waiter_pid = spawn_waiter(state)

    if how != "environment":
        print("[herdr-delay] resolved pane %s by %s (HERDR_PANE_ID was %s)."
              % (pane, how, os.environ.get("HERDR_PANE_ID") or "unset"), file=out)
    if replaced:
        age = int(time.time() - replaced["started_unix"])
        print("[herdr-delay] replaced the waiter already running for this pane "
              "(%s, started %s ago)."
              % (replaced.get("command") or "plain delay", format_duration(age)), file=out)
    print(start_banner(state, waiter_pid), file=out)
    return 0
