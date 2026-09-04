"""The two strings that are the actual product.

Everything else in this package exists to put the wake message in front of the
agent at the right moment.
"""

import time

from .duration import format_duration

RESOLVED = "RESOLVED"
FAILED = "FAILED"
TIMED_OUT = "TIMED_OUT"
ERROR = "ERROR"

TAIL_LINES = 20
TAIL_BYTES = 4000


def tail(output, lines=TAIL_LINES, limit=TAIL_BYTES):
    """Trim command output to the last few lines, bounded in bytes."""
    if not output:
        return ""
    trimmed = output.rstrip("\n")
    if not trimmed:
        return ""
    kept = trimmed.splitlines()[-lines:]
    text = "\n".join(kept)
    if len(text.encode("utf-8", "replace")) > limit:
        text = text.encode("utf-8", "replace")[-limit:].decode("utf-8", "replace")
        text = "...\n" + text.split("\n", 1)[-1]
    return text


def describe_mode(state):
    """One line describing what the waiter is doing, for the start banner."""
    mode = state["mode"]
    if mode == "in":
        return "sleep %s" % format_duration(state["in_seconds"])
    if mode == "then":
        if state["in_seconds"]:
            return "sleep %s, then run once" % format_duration(state["in_seconds"])
        return "run once"
    return "poll every %s until exit 0" % format_duration(state["every_seconds"])


def start_banner(state, waiter_pid):
    """Printed by the front command. Its job is to stop the agent's turn."""
    lines = [
        "[herdr-delay] Waiter started (pid %d) for pane %s."
        % (waiter_pid, state["pane_id"]),
        "  mode     : %s" % describe_mode(state),
    ]
    if state.get("command"):
        lines.append("  command  : %s" % state["command"])
    if state.get("reason"):
        lines.append("  reason   : %s" % state["reason"])
    if state["mode"] == "until":
        expires = time.strftime(
            "%H:%M:%S", time.localtime(state["started_unix"] + state["deadline_seconds"])
        )
        lines.append(
            "  deadline : %s (expires %s)"
            % (format_duration(state["deadline_seconds"]), expires)
        )
    lines += [
        "",
        "END YOUR TURN NOW. Do not poll, sleep, or re-run this command.",
        "You will be prompted automatically with the result.",
    ]
    return "\n".join(lines)


def wake_message(state, result):
    """Injected into the agent's composer once the wait resolves."""
    lines = ["[herdr-delay:%s] Your scheduled wait finished." % state["marker"]]

    if state.get("command"):
        lines.append("  waited for : %s" % state["command"])
    elif state.get("reason"):
        lines.append("  waited for : %s" % state["reason"])
    else:
        lines.append("  waited for : %s" % format_duration(state["in_seconds"]))

    outcome = result["outcome"]
    elapsed = format_duration(result["elapsed_seconds"])
    if result.get("exit_code") is None:
        lines.append("  outcome    : %s after %s" % (outcome, elapsed))
    else:
        lines.append(
            "  outcome    : %s (exit %d) after %s"
            % (outcome, result["exit_code"], elapsed)
        )

    if state.get("reason") and state.get("command"):
        lines.append("  reason     : %s" % state["reason"])
    if result.get("polls"):
        lines.append("  polls      : %d" % result["polls"])
    if outcome == TIMED_OUT:
        lines.append(
            "  note       : the deadline expired before the check passed."
        )
    if outcome == ERROR and result.get("error"):
        lines.append("  error      : %s" % result["error"])

    body = tail(result.get("output", ""))
    if body:
        lines += ["", "  --- last %d lines of output ---" % TAIL_LINES, body]
    return "\n".join(lines)
