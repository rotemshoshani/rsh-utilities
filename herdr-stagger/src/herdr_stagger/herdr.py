"""Typed wrapper over the `herdr` CLI.

Every call returns parsed JSON or raises HerdrError carrying the server's own
error code, so callers branch on `agent_prompt_stalled` and friends instead of
scraping messages.

Deliberately small: this tool reads statuses and submits prompts. It never
touches the raw pane surface. `herdr pane run` appends Enter to a shell, so a
"fallback" delivery would execute the prompt text as a command in the
operator's pane.
"""

import json
import subprocess

BINARY = "herdr"

GONE = ("agent_not_found", "invalid_target", "pane_not_found")


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def _run(args, timeout=None):
    proc = subprocess.run(
        [BINARY] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    blob = proc.stdout.strip() or proc.stderr.strip()
    payload = None
    if blob:
        try:
            payload = json.loads(blob)
        except ValueError:
            payload = None
    if payload and isinstance(payload, dict) and "error" in payload:
        err = payload["error"]
        raise HerdrError(err.get("code", "unknown"), err.get("message", ""))
    if proc.returncode != 0:
        raise HerdrError("cli_failed", blob or "herdr exited %d" % proc.returncode)
    return (payload or {}).get("result", {})


def agent_list():
    """Every live agent Herdr knows about, with pane, workspace, and status."""
    return _run(["agent", "list"]).get("agents", [])


def agent_status(pane_id):
    """Current lifecycle state, or None when no agent occupies the pane."""
    try:
        result = _run(["agent", "get", pane_id])
    except HerdrError as exc:
        if exc.code in GONE:
            return None
        raise
    return result.get("agent", {}).get("agent_status")


def agent_prompt(pane_id, text):
    """Submit text plus Enter. Never call this on a blocked agent."""
    return _run(["agent", "prompt", pane_id, text])


def notification_show(title, body=None):
    args = ["notification", "show", title]
    if body:
        args += ["--body", body]
    try:
        return _run(args)
    except HerdrError:
        return None
