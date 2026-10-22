#!/usr/bin/env python3
"""Read and validate a project-local .rsh-utils.json configuration file."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


def fail(message: str) -> None:
    raise ConfigError(message)


def object_value(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{name} must be an object")
    return value


def string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        fail(f"{name} must be a non-empty string")
    return value


def integer(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail(f"{name} must be an integer of at least {minimum}")
    return value


def positive_number(value: Any, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        fail(f"{name} must be a positive number")
    return value


def validate_section(raw: dict[str, Any], section: str, fields: set[str]) -> dict[str, Any]:
    value = raw.get(section, {})
    data = object_value(value, section)
    unknown = sorted(set(data) - fields)
    if unknown:
        fail(f"{section} has unsupported field(s): {', '.join(unknown)}")
    return data


def dev_env_values(raw: dict[str, Any]) -> dict[str, str]:
    data = validate_section(raw, "dev_env", {
        "top_command", "bottom_command", "mode", "repair", "remote", "branch",
        "interval_seconds", "restart_hours", "uat_port",
    })
    result: dict[str, str] = {}
    if "uat_port" in data:
        port = data['uat_port']
        if port is None:
            result['uat_port'] = '0'
        else:
            port = integer(port, 'dev_env.uat_port', 1024)
            if port > 65535:
                fail('dev_env.uat_port must be at most 65535')
            result['uat_port'] = str(port)
    for field in ("top_command", "bottom_command", "remote", "branch"):
        if field in data:
            result[field] = string(data[field], f"dev_env.{field}")
    if "mode" in data:
        mode = string(data["mode"], "dev_env.mode")
        if mode not in {"normal", "pull", "sync"}:
            fail("dev_env.mode must be one of: normal, pull, sync")
        result["mode"] = mode
    if "repair" in data:
        if not isinstance(data["repair"], bool):
            fail("dev_env.repair must be a boolean")
        result["repair"] = "true" if data["repair"] else "false"
    if "interval_seconds" in data:
        result["interval_seconds"] = str(integer(data["interval_seconds"], "dev_env.interval_seconds", 5))
    if "restart_hours" in data:
        restart_hours = data["restart_hours"]
        result["restart_hours"] = "off" if restart_hours is None else str(positive_number(restart_hours, "dev_env.restart_hours"))
    return result


def council_values(raw: dict[str, Any]) -> dict[str, str]:
    data = validate_section(raw, "council", {
        "claude_command", "codex_command", "approval_key", "capture_lines",
        "claude_ready_pattern", "codex_ready_pattern", "ready_timeout_seconds",
        "claude_chrome_marker", "claude_chrome_lines", "codex_chrome_marker",
        "codex_chrome_lines", "paste_settle_seconds", "terminal",
    })
    result: dict[str, str] = {}
    for field in (
        "claude_command", "codex_command", "approval_key", "claude_ready_pattern",
        "codex_ready_pattern", "claude_chrome_marker", "claude_chrome_lines",
        "codex_chrome_marker", "codex_chrome_lines",
    ):
        if field in data:
            result[field] = string(data[field], f"council.{field}")
    if "capture_lines" in data:
        result["capture_lines"] = str(integer(data["capture_lines"], "council.capture_lines", 1))
    if "ready_timeout_seconds" in data:
        result["ready_timeout_seconds"] = str(integer(data["ready_timeout_seconds"], "council.ready_timeout_seconds", 1))
    if "paste_settle_seconds" in data:
        settle = data["paste_settle_seconds"]
        result["paste_settle_seconds"] = "" if settle is None else str(positive_number(settle, "council.paste_settle_seconds"))
    if "terminal" in data:
        terminal = data["terminal"]
        result["terminal"] = "" if terminal is None else string(terminal, "council.terminal")
    return result


def prod_env_values(raw: dict[str, Any]) -> dict[str, Any]:
    data = validate_section(raw, "prod_env", {"panes"})
    if "panes" not in data:
        fail("prod_env.panes is required")
    panes = data["panes"]
    if not isinstance(panes, list):
        fail("prod_env.panes must be an array")
    if not 1 <= len(panes) <= 9:
        fail("prod_env.panes must contain between 1 and 9 panes")

    normalized: list[dict[str, Any]] = []
    titles: set[str] = set()
    common_fields = {"type", "title"}
    type_fields = {
        "convex": {"deployment", "log_mode", "history"},
        "vercel": {"project", "target", "scope", "poll_seconds", "error_log_lines"},
        "command": {"command"},
    }

    for index, value in enumerate(panes):
        name = f"prod_env.panes[{index}]"
        pane = object_value(value, name)
        pane_type = string(pane.get("type"), f"{name}.type")
        if pane_type not in type_fields:
            fail(f"{name}.type must be one of: command, convex, vercel")
        unknown = sorted(set(pane) - common_fields - type_fields[pane_type])
        if unknown:
            fail(f"{name} has unsupported field(s): {', '.join(unknown)}")

        title = string(pane.get("title"), f"{name}.title")
        if title in titles:
            fail(f"prod_env pane titles must be unique: {title}")
        titles.add(title)

        result: dict[str, Any] = {"type": pane_type, "title": title}
        if pane_type == "convex":
            result["deployment"] = string(pane.get("deployment"), f"{name}.deployment")
            log_mode = pane.get("log_mode", "errors")
            if log_mode not in {"errors", "all"}:
                fail(f"{name}.log_mode must be one of: all, errors")
            result["log_mode"] = log_mode
            result["history"] = integer(pane.get("history", 500), f"{name}.history", 0)
        elif pane_type == "vercel":
            result["project"] = string(pane.get("project"), f"{name}.project")
            target = pane.get("target", "production")
            if target not in {"production", "preview", "all"}:
                fail(f"{name}.target must be one of: all, preview, production")
            result["target"] = target
            if "scope" in pane:
                result["scope"] = string(pane["scope"], f"{name}.scope")
            result["poll_seconds"] = integer(
                pane.get("poll_seconds", 30), f"{name}.poll_seconds", 1
            )
            result["error_log_lines"] = integer(
                pane.get("error_log_lines", 120), f"{name}.error_log_lines", 1
            )
        else:
            result["command"] = string(pane.get("command"), f"{name}.command")
        normalized.append(result)

    return {"panes": normalized}


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as config_file:
        raw = json.load(config_file)
    raw = object_value(raw, "root")
    unknown = sorted(set(raw) - {"version", "dev_env", "council", "prod_env"})
    if unknown:
        fail(f"root has unsupported field(s): {', '.join(unknown)}")
    if raw.get("version") != 1:
        fail("version must be 1")
    return raw


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in {"dev_env", "council", "prod_env"}:
        print("usage: project_config.py PATH {dev_env|council|prod_env}", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        raw = load_config(path)
        if sys.argv[2] == "dev_env":
            values = dev_env_values(raw)
        elif sys.argv[2] == "council":
            values = council_values(raw)
        else:
            values = {"json": json.dumps(prod_env_values(raw), separators=(",", ":"))}
    except (OSError, json.JSONDecodeError, ConfigError) as error:
        print(f"rsh-utils config error in {path}: {error}", file=sys.stderr)
        return 1

    for key, value in values.items():
        sys.stdout.buffer.write(key.encode() + b"\0" + value.encode() + b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
