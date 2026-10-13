"""The detached half: resolve the wait, then wake the agent.

Both phases take their collaborators as arguments so the tests can drive them
with a fake clock and a recording herdr stub.
"""

import json
import os
import subprocess
import sys
import time

from . import herdr as herdr_cli
from . import state as state_mod
from .message import ERROR, FAILED, RESOLVED, TIMED_OUT, wake_message

VERIFY_WINDOW_SECONDS = 6.0
VERIFY_POLL_SECONDS = 0.75


def run_check(command, timeout=None):
    """Run a check command under bash, combining stdout and stderr."""
    proc = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def resolve(state, runner=run_check, sleeper=time.sleep, clock=time.monotonic):
    """Phase one: wait for the thing, and decide what happened."""
    started = clock()
    mode = state["mode"]
    command = state.get("command")

    def elapsed():
        return int(round(clock() - started))

    if state.get("in_seconds"):
        sleeper(state["in_seconds"])

    if mode == "in":
        return {
            "outcome": RESOLVED,
            "exit_code": None,
            "elapsed_seconds": elapsed(),
            "polls": 0,
            "output": "",
        }

    if mode == "then":
        try:
            code, output = runner(command)
        except Exception as exc:  # command could not be executed at all
            return _error(exc, elapsed())
        return {
            "outcome": RESOLVED if code == 0 else FAILED,
            "exit_code": code,
            "elapsed_seconds": elapsed(),
            "polls": 1,
            "output": output,
        }

    # mode == "until": poll until exit 0 or the deadline.
    deadline = state["deadline_seconds"]
    every = state["every_seconds"]
    poll_started = clock()
    polls = 0
    last_output = ""
    last_code = None
    while True:
        remaining = deadline - (clock() - poll_started)
        if remaining <= 0:
            return {
                "outcome": TIMED_OUT,
                "exit_code": last_code,
                "elapsed_seconds": elapsed(),
                "polls": polls,
                "output": last_output,
            }
        try:
            last_code, last_output = runner(command, timeout=remaining)
        except subprocess.TimeoutExpired:
            return {
                "outcome": TIMED_OUT,
                "exit_code": None,
                "elapsed_seconds": elapsed(),
                "polls": polls + 1,
                "output": last_output,
            }
        except Exception as exc:
            return _error(exc, elapsed())
        polls += 1
        if last_code == 0:
            return {
                "outcome": RESOLVED,
                "exit_code": 0,
                "elapsed_seconds": elapsed(),
                "polls": polls,
                "output": last_output,
            }
        remaining = deadline - (clock() - poll_started)
        if remaining <= 0:
            return {
                "outcome": TIMED_OUT,
                "exit_code": last_code,
                "elapsed_seconds": elapsed(),
                "polls": polls,
                "output": last_output,
            }
        sleeper(min(every, remaining))


def _error(exc, elapsed_seconds):
    return {
        "outcome": ERROR,
        "exit_code": None,
        "elapsed_seconds": elapsed_seconds,
        "polls": 0,
        "output": "",
        "error": str(exc),
    }


def _still_ours(state, hd):
    """Same pid foreground in the pane means the same conversation.

    Codex reports no agent_session, so pid is the only identity check that
    works uniformly across agent kinds.
    """
    try:
        return state["agent_pid"] in hd.foreground_pids(state["pane_id"])
    except hd.HerdrError:
        return False


def _verify(state, hd, sleeper=time.sleep, clock=time.monotonic):
    """Composer text is not delivery; require the idle agent to start working."""
    deadline = clock() + VERIFY_WINDOW_SECONDS
    while clock() < deadline:
        if not _still_ours(state, hd):
            return False
        if hd.agent_status(state["pane_id"]) == "working":
            return True
        sleeper(VERIFY_POLL_SECONDS)
    return False


def deliver(state, result, hd=herdr_cli, sleeper=time.sleep, clock=time.monotonic, log=print):
    """Phase two. See the design doc's Delivery section for why this shape."""
    pane = state["pane_id"]
    text = wake_message(state, result)

    if not _still_ours(state, hd):
        log("undelivered: agent pid %s is no longer in pane %s" % (state["agent_pid"], pane))
        hd.notification_show("herdr-delay: wake undelivered", "agent gone from %s" % pane)
        return False

    status = hd.agent_status(pane)
    if status is None:
        log("undelivered: no agent in pane %s" % pane)
        hd.notification_show("herdr-delay: wake undelivered", "no agent in %s" % pane)
        return False

    if status in ("blocked", "working"):
        # Prompting a blocked agent does not steer it. The Enter answers the
        # overlay's highlighted default and the wake text is discarded.
        log("agent is busy or blocked; holding until idle")
        try:
            hd.agent_wait_unblocked(pane)
        except hd.HerdrError as exc:
            log("undelivered: wait failed (%s)" % exc.code)
            return False
        if not _still_ours(state, hd):
            log("undelivered: agent replaced while blocked")
            return False
        status = hd.agent_status(pane)

    if not _still_ours(state, hd) or status not in ("idle", "done"):
        log("undelivered: agent changed or is no longer ready")
        return False
    try:
        hd.agent_prompt(pane, text, expected_pid=state["agent_pid"])
    except hd.HerdrError as exc:
        log("submission failed: %s" % exc.code)
    else:
        if _verify(state, hd, sleeper=sleeper, clock=clock):
            log("delivered: agent started working")
            return True
        log("submission unconfirmed: agent did not start working")

    # Do not paste again or blindly send a second Enter: the first submission
    # may have landed, or the user may have started editing the composer.

    hd.notification_show(
        "herdr-delay: wake undelivered",
        "%s did not accept the wake message" % pane,
    )
    return False


def main(argv):
    """Entry point for the detached child: herdr-delay __waiter <state-file>."""
    with open(argv[0]) as handle:
        state = json.load(handle)

    started = time.time()
    result = resolve(state)
    print(
        "[%s] resolved: %s after %ss (%d polls)"
        % (
            time.strftime("%H:%M:%S"),
            result["outcome"],
            result["elapsed_seconds"],
            result["polls"],
        ),
        flush=True,
    )
    try:
        deliver(state, result)
    finally:
        state_mod.clear_state(state["pane_id"])
    print("[%s] waiter done (%.1fs total)" % (time.strftime("%H:%M:%S"), time.time() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
