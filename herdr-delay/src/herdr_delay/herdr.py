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
    """Block until the agent leaves `blocked`.

    Waiting on idle alone is wrong: an agent whose tab has not been seen in the
    focused UI settles into `done`, not `idle`, which is exactly the case this
    tool exists for.
    """
    args = [
        "agent", "wait", pane_id,
        "--until", "idle", "--until", "done", "--until", "working",
    ]
    if timeout_ms:
        args += ["--timeout", str(timeout_ms)]
    return _run(args)


def agent_prompt(pane_id, text):
    """Submit text plus Enter. Never call this on a blocked agent."""
    return _run(["agent", "prompt", pane_id, text])


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
