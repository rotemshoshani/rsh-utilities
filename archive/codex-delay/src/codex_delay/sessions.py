from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CodexSession:
    id: str
    name: str
    updated_at: str
    cwd: Path
    thread_name: str = ""
    title: str = ""
    originator: str = ""
    source: str = ""
    cli_version: str = ""
    git_branch: str = ""
    model: str = ""
    reasoning_effort: str = ""
    tokens_used: int = 0

    @property
    def updated_display(self) -> str:
        try:
            parsed = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return self.updated_at[:16].replace("T", " ") or "unknown"


def _read_index(index_path: Path) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    if not index_path.is_file():
        return records

    with index_path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, TypeError):
                continue
            session_id = str(record.get("id", ""))
            if session_id:
                records[session_id] = {
                    "name": str(record.get("thread_name", "")),
                    "updated_at": str(record.get("updated_at", "")),
                }
    return records


def _first_record(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            record = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if record.get("type") != "session_meta" or not isinstance(record.get("payload"), dict):
        return None
    return record["payload"]


def _source_label(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        return str(next(iter(value)))
    return ""


def _state_sessions(
    database_path: Path,
    workdir: Path,
    indexed: dict[str, dict[str, str]],
) -> dict[str, CodexSession]:
    if not database_path.is_file():
        return {}
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        required = {"id", "cwd", "title", "updated_at", "source"}
        if not required.issubset(columns):
            connection.close()
            return {}

        optional = {
            "thread_source": "''",
            "archived": "0",
            "cli_version": "''",
            "git_branch": "''",
            "model": "''",
            "reasoning_effort": "''",
            "tokens_used": "0",
            "first_user_message": "''",
            "updated_at_ms": "0",
        }
        fields = ["id", "cwd", "title", "updated_at", "source"]
        fields.extend(f"{name}" if name in columns else f"{fallback} AS {name}" for name, fallback in optional.items())
        rows = connection.execute(
            f"SELECT {', '.join(fields)} FROM threads WHERE cwd = ? ORDER BY updated_at DESC",
            (str(workdir),),
        ).fetchall()
        connection.close()
    except sqlite3.Error:
        return {}

    sessions: dict[str, CodexSession] = {}
    for row in rows:
        if row["archived"]:
            continue
        if row["thread_source"] not in ("", "user") or row["source"] != "cli":
            continue
        session_id = str(row["id"])
        index_record = indexed.get(session_id, {})
        thread_name = index_record.get("name", "")
        title = str(row["title"] or row["first_user_message"] or "")
        timestamp = (int(row["updated_at_ms"]) / 1000) if row["updated_at_ms"] else int(row["updated_at"])
        updated_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        sessions[session_id] = CodexSession(
            id=session_id,
            name=thread_name or title or "Untitled session",
            updated_at=updated_at,
            cwd=workdir,
            thread_name=thread_name,
            title=title,
            originator="codex-tui",
            source=str(row["source"]),
            cli_version=str(row["cli_version"] or ""),
            git_branch=str(row["git_branch"] or ""),
            model=str(row["model"] or ""),
            reasoning_effort=str(row["reasoning_effort"] or ""),
            tokens_used=int(row["tokens_used"] or 0),
        )
    return sessions


def find_repo_sessions(workdir: Path, codex_dir: Path | None = None) -> list[CodexSession]:
    """Return interactive Codex sessions whose recorded cwd exactly matches workdir."""
    workdir = workdir.expanduser().resolve()
    codex_dir = (codex_dir or Path.home() / ".codex").expanduser()
    indexed = _read_index(codex_dir / "session_index.jsonl")
    found = _state_sessions(codex_dir / "state_5.sqlite", workdir, indexed)
    sessions_dir = codex_dir / "sessions"
    if not sessions_dir.is_dir():
        return sorted(found.values(), key=lambda item: item.updated_at, reverse=True)

    for path in sessions_dir.rglob("*.jsonl"):
        payload = _first_record(path)
        if payload is None:
            continue
        if payload.get("thread_source", "user") != "user":
            continue
        if str(payload.get("originator", "codex-tui")) != "codex-tui":
            continue
        try:
            session_cwd = Path(str(payload["cwd"])).expanduser().resolve()
        except (KeyError, OSError):
            continue
        if session_cwd != workdir:
            continue

        session_id = str(payload.get("session_id") or payload.get("id") or "")
        if not session_id:
            continue
        index_record = indexed.get(session_id, {})
        updated_at = index_record.get("updated_at") or str(payload.get("timestamp", ""))
        candidate = CodexSession(
            id=session_id,
            name=index_record.get("name") or "Untitled session",
            updated_at=updated_at,
            cwd=session_cwd,
            thread_name=index_record.get("name", ""),
            originator=str(payload.get("originator", "")),
            source=_source_label(payload.get("source", "")),
            cli_version=str(payload.get("cli_version", "")),
        )
        previous = found.get(session_id)
        if previous is None or (previous.name == "Untitled session" and candidate.name != previous.name):
            found[session_id] = candidate

    return sorted(found.values(), key=lambda item: item.updated_at, reverse=True)
