"""State directory, state files, and waiter process lifecycle."""

import errno
import json
import os
import re
import signal
import tempfile
import time

VERSION = 1


def state_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    path = os.path.join(base, "herdr-delay")
    os.makedirs(path, exist_ok=True)
    return path


def _slug(pane_id):
    return re.sub(r"[^A-Za-z0-9_-]", "-", pane_id)


def state_path(pane_id):
    return os.path.join(state_dir(), "%s.json" % _slug(pane_id))


def log_path(pane_id):
    return os.path.join(state_dir(), "%s.log" % _slug(pane_id))


def write_state(state):
    path = state_path(state["pane_id"])
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(state, handle, indent=1)
    os.replace(tmp, path)
    return path


def read_state(pane_id):
    try:
        with open(state_path(pane_id)) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def clear_state(pane_id):
    try:
        os.unlink(state_path(pane_id))
    except OSError:
        pass


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def live_state(pane_id):
    """The pane's waiter, or None when there isn't one running."""
    state = read_state(pane_id)
    if not state:
        return None
    if not pid_alive(state.get("waiter_pid")):
        clear_state(pane_id)
        return None
    return state


def kill_waiter(state):
    """Signal the waiter's whole session so a running check dies with it."""
    pid = state.get("waiter_pid")
    if not pid_alive(pid):
        return False
    for target in (lambda: os.killpg(pid, signal.SIGTERM), lambda: os.kill(pid, signal.SIGTERM)):
        try:
            target()
            break
        except OSError:
            continue
    for _ in range(20):
        if not pid_alive(pid):
            break
        time.sleep(0.05)
    return True
