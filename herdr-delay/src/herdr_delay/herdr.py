"""Typed wrapper over the `herdr` CLI.

Every call returns parsed JSON or raises HerdrError carrying the server's own
error code, so callers can branch on `agent_not_found` and friends instead of
scraping messages.
"""

import json
import subprocess

BINARY = "herdr"


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def _run(args, timeout=None, allow_empty=False, idle_event_pane=None):
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
    # pane run succeeds silently in Herdr 0.7.4. Reads and agent send
    # still require JSON, so usage/help output cannot masquerade as success.
    if allow_empty and not blob:
        return {}
    # agent wait returns an agent result when already idle, but an event
    # envelope when it had to wait for the transition (Herdr 0.7.4).
    if idle_event_pane is not None and isinstance(payload, dict):
        data = payload.get("data")
        if (
            payload.get("event") in ("pane.agent_status_changed", "pane_agent_status_changed")
            and isinstance(data, dict)
            and data.get("pane_id") == idle_event_pane
            and data.get("agent_status") in ("idle", "done")
        ):
            return data
    if not isinstance(payload, dict) or "result" not in payload:
        raise HerdrError("invalid_response", "herdr did not return a JSON result")
    return payload["result"]


def foreground_pids(pane_id):
    """Pids in the pane's foreground process group."""
    result = _run(["pane", "process-info", "--pane", pane_id])
    info = result.get("process_info", {})
    return [p["pid"] for p in info.get("foreground_processes", [])]


def agent_list():
    """Every live agent Herdr knows about, with pane, cwd, and status."""
    return _run(["agent", "list"]).get("agents", [])


def pane_ids(workspace_id=None):
    args = ["pane", "list"]
    if workspace_id:
        args += ["--workspace", workspace_id]
    return [p["pane_id"] for p in _run(args).get("panes", [])]


def foreground_group_id(pane_id):
    """Leader pid of the pane's foreground group: the agent process itself."""
    result = _run(["pane", "process-info", "--pane", pane_id])
    return result.get("process_info", {}).get("foreground_process_group_id")


def agent_status(pane_id):
    """Current lifecycle state, or None when no agent occupies the pane."""
    try:
        result = _run(["agent", "get", pane_id])
    except HerdrError as exc:
        if exc.code in ("agent_not_found", "invalid_target", "pane_not_found"):
            return None
        raise
    return result.get("agent", {}).get("agent_status")


def agent_wait_unblocked(pane_id, timeout_ms=None):
    """Wait for CLI completion; Herdr maps the UI's `done` state to idle."""
    args = ["agent", "wait", pane_id, "--status", "idle"]
    if timeout_ms:
        args += ["--timeout", str(timeout_ms)]
    return _run(args, idle_event_pane=pane_id)


def agent_prompt(pane_id, text, expected_pid):
    """Use Herdr's paste-aware submission only for the scheduled idle agent.

    `agent send` writes raw bytes. `pane run` frames a bracketed paste when
    supported and appends the correctly encoded Enter in the same API request.
    This is the primary submission path, never a fallback to a missing agent.
    """
    if expected_pid not in foreground_pids(pane_id):
        raise HerdrError("agent_replaced", "the scheduled agent is no longer foreground")
    if agent_status(pane_id) not in ("idle", "done"):
        raise HerdrError("agent_not_ready", "the agent cannot safely accept a wake")
    return _run(["pane", "run", pane_id, text], allow_empty=True)


def agent_read(pane_id, source="detection", lines=200):
    try:
        result = _run(
            ["agent", "read", pane_id, "--source", source, "--lines", str(lines)]
        )
    except HerdrError:
        return ""
    for key in ("text", "output", "content", "data"):
        if isinstance(result.get(key), str):
            return result[key]
    return json.dumps(result)


def notification_show(title, body=None):
    args = ["notification", "show", title]
    if body:
        args += ["--body", body]
    try:
        return _run(args)
    except HerdrError:
        return None
