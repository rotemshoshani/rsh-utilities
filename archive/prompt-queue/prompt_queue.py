#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).with_name("config.json")
LOCAL_CONFIG = Path(__file__).with_name("config.local.json")
DEFAULT_ENV = Path(__file__).with_name(".env.local")
QUEUES_DIR = Path(__file__).with_name("queues")
ARCHIVE_DIR = Path(__file__).with_name("archive")
STOP_NEXT_FLAG = "stop-next.flag"
FINISH_SLEEP_FLAG = "finish-sleep.flag"
PROGRESS_FILE = "progress.json"
TIMINGS_FILE = "timings.jsonl"
PROMPT_LIST_FILE = "prompt-list.txt"
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
NO_MORE_PROMPTS = "no more prompts"
DEFAULT_PROMPT_END_MARKER = "::end"
DEFAULT_BLOCK_MARKER = "DO-NOT-PROCEED"
DEFAULT_RECOVERY_SUCCESS_MARKER = "PROCEED-ALLOWED"
DEFAULT_RECOVERY_HUMAN_MARKER = "HUMAN-DECISION-REQUIRED"
WORKDIR_ENV_KEY = "PROMPT_QUEUE_WORKDIR"
DEFAULT_CODEX_COMMAND = "cdx"
DEFAULT_CLAUDE_COMMAND = "cld"
DEFAULT_CODEX_PROMPT_DELIVERY = "argument_file"
DEFAULT_CLAUDE_PROMPT_DELIVERY = "paste"
TMUX_SESSION_ENV_KEYS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def default_config_path() -> Path:
    return LOCAL_CONFIG if LOCAL_CONFIG.exists() else DEFAULT_CONFIG


def validate_queue_id(value: str) -> str:
    queue_id = value.strip()
    if not queue_id:
        raise SystemExit("prompt-queue: queue id is required")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    if queue_id in {".", ".."} or any(character not in allowed for character in queue_id):
        raise SystemExit("prompt-queue: queue id may contain only letters, numbers, hyphens, and underscores")
    return queue_id


def queue_dir(queue_id: str) -> Path:
    return QUEUES_DIR / validate_queue_id(queue_id)


def queue_config_path(queue_id: str) -> Path:
    path = queue_dir(queue_id) / "config.json"
    if not path.is_file():
        raise SystemExit(f"prompt-queue: queue {queue_id!r} not found at {path}")
    return path


def config_path_for_args(args: argparse.Namespace) -> Path:
    queue_id = getattr(args, "queue_id", "")
    if queue_id:
        return queue_config_path(queue_id)
    return Path(args.config).expanduser().resolve()


@dataclass(frozen=True)
class PromptItem:
    index: int
    name: str
    text: str
    source: str = "config"


@dataclass(frozen=True)
class Config:
    project_dir: Path
    runtime_dir: Path
    session_name: str
    command: str
    startup_wait_seconds: int
    capture_lines: int
    history_limit: int
    prompt_delivery: str
    ready_check_seconds: int
    ready_check_lines: int
    ready_markers: tuple[str, ...]
    block_marker: str
    block_check_lines: int
    blocked_recovery: bool
    blocked_recovery_command: str
    blocked_recovery_session_id: str
    blocked_recovery_success_marker: str
    blocked_recovery_human_marker: str
    blocked_recovery_action: str
    blocked_recovery_max_attempts: int
    blocked_recovery_check_lines: int
    completion_notify: bool
    completion_notify_command: str
    completion_notify_session_id: str
    completion_notify_check_lines: int
    completion_prompt: str
    prompts: tuple[PromptItem, ...]


@dataclass(frozen=True)
class RunItem:
    index: int
    prompt_name: str
    prompt_text: str
    prompt_source: str
    command: str
    prompt_delivery: str = "argument_file"


@dataclass(frozen=True)
class RunPlan:
    project_dir: Path
    session: str
    prompt_count: int
    completed: list[int]
    pending: list[int]
    resume_runtime_dir: Path | None
    existing_session: bool
    queue_id: str = ""


def expand_project_path(value: str, base_dir: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def load_env_file(path: Path) -> dict[str, str]:
    path = path.expanduser()
    if not path.exists():
        return {}

    loaded: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"{path}:{line_number}: empty env key")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        loaded[key] = value
        os.environ[key] = value
    return loaded


def load_config(
    path: Path = DEFAULT_CONFIG,
    project_dir_override: Path | None = None,
    runtime_dir_override: Path | None = None,
) -> Config:
    path = path.expanduser().resolve()
    load_env_file(path.with_name(DEFAULT_ENV.name))
    raw = json.loads(path.read_text())
    base_dir = path.parent

    raw_project_dir = str(raw.get("project_dir", f"${{{WORKDIR_ENV_KEY}}}"))
    project_dir = (
        project_dir_override.expanduser().resolve()
        if project_dir_override is not None
        else expand_project_path(raw_project_dir, base_dir)
    )
    runtime_dir = runtime_dir_override if runtime_dir_override is not None else make_runtime_dir(project_dir)
    prompts = tuple(load_config_prompts(raw, base_dir))

    ready_markers = raw.get("ready_markers", ["Ready"])
    if isinstance(ready_markers, str):
        ready_marker_values = (ready_markers,)
    elif isinstance(ready_markers, list):
        ready_marker_values = tuple(str(marker) for marker in ready_markers if str(marker))
    else:
        raise ValueError("ready_markers must be a string or an array of strings")

    blocked_recovery = bool(raw.get("blocked_recovery", False))
    blocked_recovery_session_id = expand_config_string(str(raw.get("blocked_recovery_session_id", "")))
    blocked_recovery_success_marker = str(
        raw.get("blocked_recovery_success_marker", DEFAULT_RECOVERY_SUCCESS_MARKER)
    )
    blocked_recovery_human_marker = str(raw.get("blocked_recovery_human_marker", DEFAULT_RECOVERY_HUMAN_MARKER))
    blocked_recovery_action = str(raw.get("blocked_recovery_action", "retry"))
    if blocked_recovery_action not in {"retry", "continue"}:
        raise ValueError("blocked_recovery_action must be 'retry' or 'continue'")
    completion_notify = bool(raw.get("completion_notify", False))
    completion_notify_session_id = expand_config_string(
        str(raw.get("completion_notify_session_id", raw.get("blocked_recovery_session_id", "")))
    )
    completion_prompt_file = str(raw.get("completion_prompt_file", "")).strip()
    completion_prompt = ""
    if completion_prompt_file:
        completion_prompt_path = expand_path(completion_prompt_file, base_dir)
        if not completion_prompt_path.is_file():
            raise ValueError(f"completion_prompt_file does not exist: {completion_prompt_path}")
        completion_prompt = completion_prompt_path.read_text().strip()

    return Config(
        project_dir=project_dir,
        runtime_dir=runtime_dir,
        session_name=str(raw.get("session_name", "prompt-queue")),
        command=str(raw.get("command", DEFAULT_CODEX_COMMAND)),
        startup_wait_seconds=int(raw.get("startup_wait_seconds", 10)),
        capture_lines=int(raw.get("capture_lines", 800)),
        history_limit=int(raw.get("history_limit", 200000)),
        prompt_delivery=str(raw.get("prompt_delivery", DEFAULT_CODEX_PROMPT_DELIVERY)),
        ready_check_seconds=int(raw.get("ready_check_seconds", 60)),
        ready_check_lines=int(raw.get("ready_check_lines", 1)),
        ready_markers=ready_marker_values,
        block_marker=str(raw.get("block_marker", DEFAULT_BLOCK_MARKER)),
        block_check_lines=int(raw.get("block_check_lines", 10)),
        blocked_recovery=blocked_recovery,
        blocked_recovery_command=str(raw.get("blocked_recovery_command", DEFAULT_CODEX_COMMAND)),
        blocked_recovery_session_id=blocked_recovery_session_id,
        blocked_recovery_success_marker=blocked_recovery_success_marker,
        blocked_recovery_human_marker=blocked_recovery_human_marker,
        blocked_recovery_action=blocked_recovery_action,
        blocked_recovery_max_attempts=int(raw.get("blocked_recovery_max_attempts", 1)),
        blocked_recovery_check_lines=int(raw.get("blocked_recovery_check_lines", 20)),
        completion_notify=completion_notify,
        completion_notify_command=str(raw.get("completion_notify_command", raw.get("blocked_recovery_command", DEFAULT_CODEX_COMMAND))),
        completion_notify_session_id=completion_notify_session_id,
        completion_notify_check_lines=int(raw.get("completion_notify_check_lines", 20)),
        completion_prompt=completion_prompt,
        prompts=prompts,
    )


def expand_config_string(value: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(value)).strip()
    if "$" in expanded:
        return ""
    return expanded


def load_config_prompts(raw: dict[str, object], base_dir: Path) -> list[PromptItem]:
    loaded: list[tuple[str, str, str]] = []

    for item in raw.get("prompts", []):
        if isinstance(item, str):
            if looks_like_prompt_file_reference(item, base_dir):
                raise ValueError(
                    "config error: string entries in 'prompts' are sent as literal prompt text. "
                    f"Move file path {item!r} to 'prompt_files' or use an object with a 'file' key."
                )
            loaded.append((f"prompt-{len(loaded) + 1}", item, "config"))
        elif isinstance(item, dict):
            name = str(item.get("name", f"prompt-{len(loaded) + 1}"))
            if "file" in item:
                file_path = expand_path(str(item["file"]), base_dir)
                loaded.append((name or file_path.stem, file_path.read_text(), str(file_path)))
            elif "lines" in item:
                lines = item["lines"]
                if not isinstance(lines, list):
                    raise ValueError("prompt lines must be an array of strings")
                loaded.append((name, "\n".join(str(line) for line in lines), "config"))
            else:
                loaded.append((name, str(item.get("text", "")), "config"))
        else:
            raise ValueError("prompts entries must be strings or objects")

    for file_value in raw.get("prompt_files", []):
        file_path = expand_path(str(file_value), base_dir)
        loaded.append((file_path.stem, file_path.read_text(), str(file_path)))

    return normalize_prompt_items(loaded)


def looks_like_prompt_file_reference(value: str, base_dir: Path) -> bool:
    stripped = value.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        return False
    suffix = Path(stripped).suffix.lower()
    if suffix not in {".md", ".txt", ".markdown"}:
        return False
    path = expand_path(stripped, base_dir)
    return path.exists()


def expand_path(value: str, base_dir: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute():
        path = base_dir / path
    return path


def normalize_prompt_items(items: list[tuple[str, str, str]]) -> list[PromptItem]:
    prompts: list[PromptItem] = []
    for raw_name, text, source in items:
        stripped = text.strip()
        if not stripped:
            continue
        prompts.append(
            PromptItem(
                index=len(prompts) + 1,
                name=sanitize_name(raw_name) or f"prompt-{len(prompts) + 1}",
                text=stripped,
                source=source,
            )
        )
    return prompts


def write_prompt_queue(path: Path, prompts: tuple[PromptItem, ...]) -> None:
    write_json(
        path,
        {
            "version": 1,
            "queue_id": queue_hash(prompts),
            "items": [prompt_manifest_item(item) for item in prompts],
        },
    )


def read_prompt_queue(path: Path) -> tuple[PromptItem, ...]:
    raw = json.loads(path.read_text())
    items = raw.get("items", []) if isinstance(raw, dict) else raw
    return tuple(
        PromptItem(
            index=int(item["index"]),
            name=str(item["name"]),
            text=str(item["text"]),
            source=str(item.get("source", "queue")),
        )
        for item in items
    )


def prompt_manifest_item(item: PromptItem) -> dict[str, object]:
    return {
        "index": item.index,
        "name": item.name,
        "text": item.text,
        "source": item.source,
        "content_hash": prompt_content_hash(item),
    }


def prompt_content_hash(item: PromptItem) -> str:
    payload = json.dumps(
        {"index": item.index, "name": item.name, "text": item.text},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def queue_hash(prompts: tuple[PromptItem, ...]) -> str:
    payload = json.dumps(
        [prompt_manifest_item(item) for item in prompts],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def prompt_items_match(left: PromptItem, right: PromptItem) -> bool:
    return left.index == right.index and left.name == right.name and left.text == right.text


def read_completed_indices(runtime_dir: Path) -> set[int]:
    progress_path = runtime_dir / PROGRESS_FILE
    state_path = runtime_dir / "state.json"
    for path in (progress_path, state_path):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        completed = raw.get("completed", [])
        if not isinstance(completed, list):
            continue
        return {int(value) for value in completed}
    return set()


def read_resumable_completed_indices(runtime_dir: Path, current_prompts: tuple[PromptItem, ...]) -> set[int]:
    queue_path = runtime_dir / "queue.json"
    if not queue_path.exists():
        return set()
    try:
        prior_prompts = read_prompt_queue(queue_path)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return set()

    current_by_index = {prompt.index: prompt for prompt in current_prompts}
    prior_by_index = {prompt.index: prompt for prompt in prior_prompts}
    completed = read_completed_indices(runtime_dir)
    return {
        index
        for index in completed
        if index in current_by_index
        and index in prior_by_index
        and prompt_items_match(current_by_index[index], prior_by_index[index])
    }


def write_progress_snapshot(
    runtime_dir: Path,
    completed: set[int],
    last_finished: dict[str, object] | None = None,
) -> None:
    value: dict[str, object] = {
        "completed": sorted(completed),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if last_finished is not None:
        value["last_finished"] = last_finished
    write_json(runtime_dir / PROGRESS_FILE, value)


def build_run_queue(config: Config) -> list[RunItem]:
    return [
        RunItem(
            index=prompt.index,
            prompt_name=prompt.name,
            prompt_text=prompt.text,
            prompt_source=prompt.source,
            command=config.command,
            prompt_delivery=config.prompt_delivery,
        )
        for prompt in config.prompts
    ]


def default_work_base_dir(project_dir: Path, queue_id: str = "") -> Path:
    base_dir = project_dir / ".planning" / "work" / "prompt-queue"
    return base_dir / validate_queue_id(queue_id) if queue_id else base_dir


def make_runtime_dir(project_dir: Path, timestamp: str | None = None, queue_id: str = "") -> Path:
    stamp = timestamp or datetime.now().strftime(TIMESTAMP_FORMAT)
    return default_work_base_dir(project_dir, queue_id) / stamp


def latest_runtime_dir(project_dir: Path, queue_id: str = "") -> Path:
    base_dir = default_work_base_dir(project_dir, queue_id)
    candidates = sorted(path for path in base_dir.iterdir() if path.is_dir()) if base_dir.exists() else []
    candidates = [path for path in candidates if (path / "session.json").exists() or (path / "state.json").exists()]
    if not candidates:
        raise SystemExit(f"no prompt-queue runs found under {base_dir}")
    return candidates[-1]


def archive_queue_directory(source_dir: Path, queue_id: str, stamp: str, archive_root: Path = ARCHIVE_DIR) -> Path:
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.is_dir():
        raise SystemExit(f"prompt-queue: active queue directory is missing: {source_dir}")

    archive_root = archive_root.expanduser().resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    base_name = f"{stamp}-{validate_queue_id(queue_id)}"
    destination = archive_root / base_name
    suffix = 2
    while destination.exists():
        destination = archive_root / f"{base_name}-{suffix}"
        suffix += 1
    return Path(shutil.move(str(source_dir), str(destination)))


def format_index_ranges(indices: list[int] | set[int] | tuple[int, ...]) -> str:
    ordered = sorted(set(indices))
    if not ordered:
        return "none"

    ranges: list[str] = []
    start = ordered[0]
    previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def build_run_plan(
    config: Config,
    session: str,
    resume_runtime_dir: Path | None,
    existing_session: bool,
    completed: set[int] | None = None,
    queue_id: str = "",
) -> RunPlan:
    completed_indices = sorted(completed if completed is not None else set())
    pending = [item.index for item in config.prompts if item.index not in completed_indices]
    return RunPlan(
        project_dir=config.project_dir,
        session=session,
        prompt_count=len(config.prompts),
        completed=completed_indices,
        pending=pending,
        resume_runtime_dir=resume_runtime_dir,
        existing_session=existing_session,
        queue_id=queue_id,
    )


def render_run_preflight(plan: RunPlan) -> str:
    lines = [
        "prompt-queue preflight",
        "",
        *([f"queue id: {plan.queue_id}"] if plan.queue_id else []),
        f"target repo: {plan.project_dir}",
        f"queue: {plan.prompt_count} prompts",
        f"resume source: {plan.resume_runtime_dir if plan.resume_runtime_dir else 'none'}",
        f"completed: {format_index_ranges(plan.completed)}",
        f"will run: {format_index_ranges(plan.pending)}",
    ]
    if plan.existing_session:
        lines.append(f"existing tmux session: {plan.session}")
    else:
        lines.append("existing tmux session: none")
    return "\n".join(lines)


def tmux_session_exists(session: str) -> bool:
    return tmux("has-session", "-t", session, check=False, capture=True).returncode == 0


def prompt_existing_session_action(plan: RunPlan) -> str:
    print(render_run_preflight(plan))
    print("")
    while True:
        choice = input("Existing tmux session found. [a]ttach, [r]eplace, [q]uit? ").strip().lower()
        if choice in {"", "a", "attach"}:
            return "attach"
        if choice in {"r", "replace"}:
            return "replace"
        if choice in {"q", "quit"}:
            return "quit"
        print("Choose a, r, or q.")


def prompt_start_new_run(plan: RunPlan) -> bool:
    print(render_run_preflight(plan))
    print("")
    while True:
        choice = input("Start this run? [Y/n] ").strip().lower()
        if choice in {"", "y", "yes"}:
            return True
        if choice in {"n", "no", "q", "quit"}:
            return False
        print("Choose y or n.")


def build_worker_cd_command(project_dir: Path) -> str:
    return f"cd -- {sh_quote(str(project_dir))}"


def build_prompt_argument_command(command: str, prompt_file: Path) -> str:
    return f"{command} \"$(cat {sh_quote(str(prompt_file))})\""


def build_resume_prompt_command(command: str, session_id: str, prompt_file: Path) -> str:
    return f"{command} resume {sh_quote(session_id)} \"$(cat {sh_quote(str(prompt_file))})\""


def build_prompt_list_watch_command(prompt_list_file: Path) -> str:
    quoted_path = sh_quote(str(prompt_list_file))
    return (
        "while true; do "
        "printf '\\033[2J\\033[H'; "
        f"if [ -f {quoted_path} ]; then cat {quoted_path}; "
        "else printf 'Prompt List\\n\\nwaiting for controller...\\n'; fi; "
        "sleep 1; "
        "done"
    )


def apply_agent_override(config: Config, use_claude: bool) -> Config:
    if not use_claude:
        return config
    return replace(
        config,
        command=DEFAULT_CLAUDE_COMMAND,
        prompt_delivery=DEFAULT_CLAUDE_PROMPT_DELIVERY,
    )


def paste_settle_seconds(text_length: int) -> float:
    if text_length < 500:
        return 1.5
    if text_length < 5000:
        return 2.5
    if text_length < 20000:
        return 4.0
    return 6.0


def should_stop_after_current(runtime_dir: Path) -> bool:
    return (runtime_dir / STOP_NEXT_FLAG).exists()


def toggle_stop_after_current(runtime_dir: Path) -> bool:
    flag = runtime_dir / STOP_NEXT_FLAG
    if flag.exists():
        flag.unlink(missing_ok=True)
        return False
    flag.write_text("1\n")
    return True


def should_finish_current_sleep(runtime_dir: Path) -> bool:
    return (runtime_dir / FINISH_SLEEP_FLAG).exists()


def consume_finish_current_sleep(runtime_dir: Path) -> bool:
    flag = runtime_dir / FINISH_SLEEP_FLAG
    if not flag.exists():
        return False
    flag.unlink(missing_ok=True)
    return True


def render_status(
    queue: list[RunItem],
    current_index: int | None,
    completed: set[int],
    stop_after_current: bool,
    phase: str = "idle",
    remaining_seconds: int | None = None,
    total_elapsed_seconds: int | None = None,
    prompt_elapsed_seconds: int | None = None,
    last_check_at: str | None = None,
    last_check_line: str | None = None,
    prompt_durations: dict[int, float] | None = None,
    title: str = "",
    completed_window: int = 3,
    queued_window: int = 8,
) -> str:
    prompt_durations = prompt_durations or {}
    current_item = next((item for item in queue if item.index == current_index), None)
    total = len(queue)
    done = len(completed)
    left = sum(1 for item in queue if item.index not in completed and item.index != current_index)
    elapsed = format_elapsed(total_elapsed_seconds or 0)
    header_title = f"prompt-queue  {title}" if title else "prompt-queue"
    current_label = f"{current_item.index:03d} {current_item.prompt_name}" if current_item else "none"

    lines = [
        f"{header_title}  running  {elapsed}",
        "",
        "Progress",
        f"  done     {done}/{total}",
        f"  current  {current_label}",
        f"  left     {left}",
    ]

    lines.extend(
        [
            "",
            "Current Prompt",
            f"  phase       {phase}",
            f"  elapsed     {format_elapsed(prompt_elapsed_seconds) if prompt_elapsed_seconds is not None else '-'}",
            f"  last check  {format_status_time(last_check_at)}",
            f"  last line   {last_check_line or '-'}",
        ]
    )
    if remaining_seconds is not None:
        lines.append(f"  wait        {format_duration(remaining_seconds)}")
    if stop_after_current:
        lines.append("  stop next   armed")
    lines.extend(["", "Controls", "  S stop after current    F finish wait    Q kill now"])
    return "\n".join(lines)


def render_prompt_list(
    queue: list[RunItem],
    current_index: int | None,
    completed: set[int],
    prompt_durations: dict[int, float] | None = None,
    prompt_elapsed_seconds: int | None = None,
    completed_window: int = 9999,
    queued_window: int = 9999,
) -> str:
    prompt_durations = prompt_durations or {}
    lines = ["Prompt List", ""]
    lines.extend(
        render_queue_rows(
            queue,
            current_index,
            completed,
            prompt_durations,
            prompt_elapsed_seconds,
            completed_window,
            queued_window,
        )
    )
    return "\n".join(lines)


def render_queue_rows(
    queue: list[RunItem],
    current_index: int | None,
    completed: set[int],
    prompt_durations: dict[int, float],
    prompt_elapsed_seconds: int | None,
    completed_window: int,
    queued_window: int,
) -> list[str]:
    rows: list[str] = []
    completed_items = [item for item in queue if item.index in completed]
    hidden_completed = max(0, len(completed_items) - max(0, completed_window))
    shown_completed = completed_items[-completed_window:] if completed_window > 0 else []
    for item in shown_completed:
        rows.append(render_queue_row("x", item, prompt_durations.get(item.index)))
    if hidden_completed:
        rows.insert(0, f"      ... {hidden_completed} completed prompts hidden")

    current_item = next((item for item in queue if item.index == current_index), None)
    if current_item is not None:
        rows.append(render_queue_row(">", current_item, None))

    queued_items = [item for item in queue if item.index not in completed and item.index != current_index]
    shown_queued = queued_items[: max(0, queued_window)]
    for item in shown_queued:
        rows.append(render_queue_row(" ", item, None))
    hidden_queued = max(0, len(queued_items) - len(shown_queued))
    if hidden_queued:
        rows.append(f"      ... {hidden_queued} queued prompts hidden")

    if not rows:
        rows.append("  none")
    return rows


def render_queue_row(marker: str, item: RunItem, duration_seconds: float | int | None) -> str:
    label = f"[{marker}] {item.index:03d} {item.prompt_name}"
    duration = format_elapsed(int(duration_seconds)) if duration_seconds is not None else ""
    return f"  {label:<58} {duration}".rstrip()


def format_status_time(value: str | None) -> str:
    if not value:
        return "-"
    if "T" in value:
        return value.split("T", 1)[1][:8]
    return value


def format_elapsed(seconds: int) -> str:
    return format_duration(seconds)


def format_duration(seconds: int) -> str:
    minutes, sec = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def sanitize_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-")
    return cleaned or "prompt"


def tmux(
    *args: str,
    check: bool = True,
    capture: bool = False,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=check,
    )


def tmux_target_exists(target: str) -> bool:
    return tmux("display-message", "-p", "-t", target, "#{pane_id}", check=False, capture=True).returncode == 0


def tmux_session_environment() -> dict[str, str]:
    values = {
        key: value
        for key in TMUX_SESSION_ENV_KEYS
        if (value := os.environ.get(key, "").strip())
    }
    runtime_dir = values.get("XDG_RUNTIME_DIR", "")
    if not runtime_dir:
        fallback_runtime_dir = Path(f"/run/user/{os.getuid()}")
        if fallback_runtime_dir.is_dir():
            runtime_dir = str(fallback_runtime_dir)
            values["XDG_RUNTIME_DIR"] = runtime_dir
    if "DBUS_SESSION_BUS_ADDRESS" not in values and runtime_dir:
        bus_path = Path(runtime_dir) / "bus"
        if bus_path.exists():
            values["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
    return values


def sync_tmux_session_environment() -> None:
    for key, value in tmux_session_environment().items():
        tmux("set-environment", "-g", key, value)


def current_tmux_session() -> str | None:
    if not os.environ.get("TMUX"):
        return None
    result = tmux("display-message", "-p", "#S", check=False, capture=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def kill_tmux_session_if_exists(session: str) -> bool:
    if tmux("has-session", "-t", session, check=False, capture=True).returncode != 0:
        return False
    tmux("kill-session", "-t", session, check=False)
    return True


def prepare_tmux_session_replacement(session: str) -> str:
    if current_tmux_session() != session:
        kill_tmux_session_if_exists(session)
        return ""
    stale_session = f"{session}-stale-{os.getpid()}"
    tmux("rename-session", "-t", session, stale_session)
    return stale_session


def controller_process_is_running(runtime_dir: Path | None, expected_session: str) -> bool | None:
    if runtime_dir is None:
        return None
    controller_path = runtime_dir / "controller.json"
    if not controller_path.is_file():
        return None
    try:
        controller = read_json(controller_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if controller.get("session") != expected_session:
        return None
    pid = controller.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def relative_to_config(path: Path, config_path: Path) -> str:
    try:
        return str(path.relative_to(config_path.parent))
    except ValueError:
        return str(path)


def write_collected_prompts(
    config_path: Path,
    collected: list[tuple[str, str]],
    append: bool = False,
) -> list[Path]:
    if not collected:
        return []

    config_path = config_path.expanduser().resolve()
    raw = read_json(config_path)
    prompt_dir = config_path.parent / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)

    existing_prompts = raw.get("prompts", []) if append else []
    if not isinstance(existing_prompts, list):
        raise ValueError("prompts must be an array")

    start_index = len(existing_prompts) + 1
    new_entries: list[dict[str, str]] = []
    written_paths: list[Path] = []
    for offset, (name, text) in enumerate(collected):
        clean_name = sanitize_name(name) or f"prompt-{start_index + offset}"
        file_path = prompt_dir / f"{start_index + offset:03d}-{clean_name}.md"
        file_path.write_text(text.strip() + "\n")
        written_paths.append(file_path)
        new_entries.append(
            {
                "name": clean_name,
                "file": relative_to_config(file_path, config_path),
            }
        )

    raw["prompts"] = [*existing_prompts, *new_entries]
    if not append:
        raw["prompt_files"] = []
    config_path.write_text(json.dumps(raw, indent=2) + "\n")
    return written_paths


class Controller:
    def __init__(
        self,
        config: Config,
        session: str,
        worker_pane: str,
        planner_pane: str = "",
        queue_id: str = "",
        queue_dir_path: Path | None = None,
        archive_root: Path | None = None,
        archive_stamp: str = "",
    ) -> None:
        self.config = config
        self.session = session
        self.worker_pane = worker_pane
        self.planner_pane = planner_pane
        self.queue_id = queue_id
        self.queue_dir_path = queue_dir_path
        self.archive_root = archive_root
        self.archive_stamp = archive_stamp
        self.queue = build_run_queue(config)
        self.completed: set[int] = read_resumable_completed_indices(config.runtime_dir, config.prompts)
        self.current_index: int | None = None
        self.phase = "starting"
        self.remaining_seconds: int | None = None
        self.run_started_at = time.monotonic()
        self.run_started_wall_at = datetime.now().isoformat(timespec="seconds")
        self.current_prompt_started_at: float | None = None
        self.current_prompt_started_wall_at: str | None = None
        self.prompt_durations: dict[int, float] = {}
        self.last_ready_check_line: str | None = None
        self.last_ready_check_at: str | None = None
        self.ready_detected = False
        self.prompt_dir = config.runtime_dir / "prompts"
        self.prompt_list_file = config.runtime_dir / PROMPT_LIST_FILE
        self.capture_dir = config.runtime_dir / "captures"
        self.ready_check_dir = config.runtime_dir / "ready-checks"
        self.recovery_prompt_dir = config.runtime_dir / "recovery-prompts"
        self.recovery_check_dir = config.runtime_dir / "recovery-checks"
        self.completion_prompt_dir = config.runtime_dir / "completion-prompts"
        self.completion_check_dir = config.runtime_dir / "completion-checks"
        self.block_detected = False
        self.block_marker_line: str | None = None
        self.block_checked_at: str | None = None
        self.last_recovery_check_line: str | None = None
        self.last_recovery_check_at: str | None = None
        self.recovery_detected = False
        self.recovery_marker_line: str | None = None
        self.last_completion_check_line: str | None = None
        self.last_completion_check_at: str | None = None
        self.completion_notify_done = False

    def run(self) -> None:
        if not self.queue:
            raise SystemExit("prompt-queue: no prompts configured")

        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_dir.mkdir(parents=True, exist_ok=True)
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.ready_check_dir.mkdir(parents=True, exist_ok=True)
        self.recovery_prompt_dir.mkdir(parents=True, exist_ok=True)
        self.recovery_check_dir.mkdir(parents=True, exist_ok=True)
        self.completion_prompt_dir.mkdir(parents=True, exist_ok=True)
        self.completion_check_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.config.runtime_dir / "controller.json", {"pid": os.getpid(), "session": self.session})
        write_progress_snapshot(self.config.runtime_dir, self.completed)
        self.render()

        for item in self.queue:
            if item.index in self.completed:
                continue
            recovery_attempts = 0
            while True:
                if should_stop_after_current(self.config.runtime_dir):
                    self.phase = "stopped before next prompt"
                    self.current_index = None
                    self.render()
                    return
                result = self.run_one(item, recovery_attempts)
                if result == "complete":
                    break
                if result == "continue":
                    self.completed.add(item.index)
                    self.record_prompt_finished(item, "continue")
                    self.current_index = None
                    self.current_prompt_started_at = None
                    self.current_prompt_started_wall_at = None
                    self.phase = "continued after recovery"
                    self.remaining_seconds = None
                    self.stop_worker()
                    self.render()
                    break
                if result == "retry":
                    recovery_attempts += 1
                    self.phase = "retrying after recovery"
                    self.remaining_seconds = None
                    self.render()
                    continue
                return

        self.current_index = None
        if self.config.completion_notify:
            self.notify_completion()
        self.phase = "complete"
        self.remaining_seconds = None
        self.render()
        self.archive_queue()

    def archive_queue(self) -> None:
        if not self.queue_id or self.queue_dir_path is None or self.archive_root is None:
            return
        destination = archive_queue_directory(
            self.queue_dir_path,
            self.queue_id,
            self.archive_stamp or datetime.now().strftime(TIMESTAMP_FORMAT),
            self.archive_root,
        )
        write_json(
            self.config.runtime_dir / "archived.json",
            {
                "queue_id": self.queue_id,
                "source": str(self.queue_dir_path),
                "destination": str(destination),
                "archived_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        self.phase = f"complete; archived to {destination.name}"
        self.render()

    def run_one(self, item: RunItem, recovery_attempts: int = 0) -> str:
        self.current_index = item.index
        self.current_prompt_started_at = time.monotonic()
        self.current_prompt_started_wall_at = datetime.now().isoformat(timespec="seconds")
        self.phase = "launching"
        self.remaining_seconds = None
        self.render()

        self.recycle_worker()
        self.cd_worker()
        prompt_file = self.write_prompt_file(item)
        if item.prompt_delivery == "argument_file":
            self.send_shell_command(build_prompt_argument_command(item.command, prompt_file))
        elif item.prompt_delivery == "paste":
            self.send_shell_command(item.command)
        else:
            raise ValueError(f"unsupported prompt_delivery for {item.prompt_name}: {item.prompt_delivery}")

        self.sleep_with_controls(self.config.startup_wait_seconds, "startup wait")

        if item.prompt_delivery == "paste":
            self.phase = "sending prompt"
            self.render()
            self.paste_prompt(prompt_file)

        sleep_result = self.wait_for_worker_ready("agent working", item)
        capture_path = self.capture_run(item)
        if sleep_result == "blocked":
            self.phase = "blocked"
            self.remaining_seconds = None
            self.write_blocked_state(item, capture_path, recovery_attempts)
            self.render()
            recovery_result = self.try_blocked_recovery(item, capture_path, recovery_attempts)
            if recovery_result in {"retry", "continue"}:
                return recovery_result
            return "blocked"
        if not should_stop_after_current(self.config.runtime_dir):
            self.stop_worker()
        self.completed.add(item.index)
        self.record_prompt_finished(item, "complete")
        self.current_index = None
        self.current_prompt_started_at = None
        self.current_prompt_started_wall_at = None
        self.phase = "captured"
        self.remaining_seconds = None
        self.render()
        return "complete"

    def record_prompt_finished(self, item: RunItem, status: str) -> None:
        finished_at = datetime.now().isoformat(timespec="seconds")
        duration_seconds = None
        if self.current_prompt_started_at is not None:
            duration_seconds = round(time.monotonic() - self.current_prompt_started_at, 2)
        entry: dict[str, object] = {
            "run_index": item.index,
            "name": item.prompt_name,
            "source": item.prompt_source,
            "status": status,
            "started_at": self.current_prompt_started_wall_at,
            "finished_at": finished_at,
            "duration_seconds": duration_seconds,
        }
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        with (self.config.runtime_dir / TIMINGS_FILE).open("a") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        if duration_seconds is not None:
            self.prompt_durations[item.index] = duration_seconds
        write_progress_snapshot(self.config.runtime_dir, self.completed, entry)

    def recycle_worker(self) -> None:
        if tmux_target_exists(self.worker_pane):
            tmux("respawn-pane", "-k", "-t", self.worker_pane, "-c", str(self.config.project_dir))
            self.sleep_with_controls(1, "fresh shell")

    def stop_worker(self) -> None:
        self.phase = "stopping agent"
        self.render()
        if tmux_target_exists(self.worker_pane):
            tmux("respawn-pane", "-k", "-t", self.worker_pane, "-c", str(self.config.project_dir))
            self.sleep_with_controls(1, "fresh shell")

    def cd_worker(self) -> None:
        self.phase = "cd project"
        self.render()
        self.send_shell_command(build_worker_cd_command(self.config.project_dir))
        self.sleep_with_controls(1, "cd project")

    def send_shell_command(self, command: str) -> None:
        tmux("send-keys", "-t", self.worker_pane, command, "Enter")

    def send_key(self, key: str) -> None:
        tmux("send-keys", "-t", self.worker_pane, key)

    def send_planner_shell_command(self, command: str) -> None:
        if not self.planner_pane:
            raise RuntimeError("planner pane is not configured")
        tmux("send-keys", "-t", self.planner_pane, command, "Enter")

    def write_prompt_file(self, item: RunItem) -> Path:
        safe_name = sanitize_name(item.prompt_name)
        prompt_file = self.prompt_dir / f"{item.index:03d}-{safe_name}.txt"
        prompt_file.write_text(item.prompt_text)
        write_json(
            self.config.runtime_dir / "last-prompt.json",
            {
                "run_index": item.index,
                "name": item.prompt_name,
                "source": item.prompt_source,
                "path": str(prompt_file),
            },
        )
        return prompt_file

    def paste_prompt(self, prompt_file: Path) -> None:
        buffer_name = f"prompt-queue-{os.getpid()}"
        prompt_text = prompt_file.read_text().rstrip("\n")
        tmux("load-buffer", "-b", buffer_name, "-", input_text=prompt_text)
        tmux("paste-buffer", "-d", "-p", "-b", buffer_name, "-t", self.worker_pane)
        time.sleep(paste_settle_seconds(len(prompt_text)))
        self.send_key("Enter")

    def capture_run(self, item: RunItem) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_name = sanitize_name(item.prompt_name)
        output = tmux(
            "capture-pane",
            "-p",
            "-J",
            "-S",
            f"-{self.config.capture_lines}",
            "-t",
            self.worker_pane,
            capture=True,
        ).stdout
        path = self.capture_dir / f"{item.index:03d}-{safe_name}-{stamp}.txt"
        path.write_text(output)
        write_json(
            self.config.runtime_dir / "last-capture.json",
            {"run_index": item.index, "name": item.prompt_name, "path": str(path)},
        )
        return path

    def capture_worker_tail(self, lines: int) -> str:
        return self.capture_pane_tail(self.worker_pane, lines)

    def capture_planner_tail(self, lines: int) -> str:
        if not self.planner_pane:
            return ""
        return self.capture_pane_tail(self.planner_pane, lines)

    def capture_pane_tail(self, pane: str, lines: int) -> str:
        return tmux(
            "capture-pane",
            "-p",
            "-J",
            "-S",
            f"-{max(1, lines)}",
            "-t",
            pane,
            capture=True,
        ).stdout

    def check_ready_marker(self, item: RunItem) -> str:
        captured = self.capture_worker_tail(self.config.ready_check_lines)
        non_empty_lines = [line for line in captured.splitlines() if line.strip()]
        last_line = non_empty_lines[-1] if non_empty_lines else ""
        matched_marker = next((marker for marker in self.config.ready_markers if marker in last_line), "")
        checked_at = datetime.now().isoformat(timespec="seconds")
        self.last_ready_check_line = last_line
        self.last_ready_check_at = checked_at
        self.ready_detected = bool(matched_marker)
        block_line = ""
        if matched_marker and self.config.block_marker:
            block_captured = self.capture_worker_tail(self.config.block_check_lines)
            block_lines = [line.strip() for line in block_captured.splitlines() if line.strip()]
            block_line = next((line for line in block_lines[-self.config.block_check_lines :] if line == self.config.block_marker), "")
            self.block_detected = bool(block_line)
            self.block_marker_line = block_line or None
            self.block_checked_at = checked_at

        safe_name = sanitize_name(item.prompt_name)
        log_path = self.ready_check_dir / f"{item.index:03d}-{safe_name}.jsonl"
        with log_path.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "checked_at": checked_at,
                        "run_index": item.index,
                        "name": item.prompt_name,
                        "last_line": last_line,
                        "matched": bool(matched_marker),
                        "matched_marker": matched_marker,
                        "block_checked": bool(matched_marker and self.config.block_marker),
                        "block_matched": bool(block_line),
                        "block_marker": self.config.block_marker,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        if block_line:
            return "blocked"
        if matched_marker:
            return "ready"
        return "waiting"

    def write_blocked_state(self, item: RunItem, capture_path: Path, recovery_attempts: int) -> None:
        write_json(
            self.config.runtime_dir / "blocked.json",
            {
                "run_index": item.index,
                "name": item.prompt_name,
                "capture_path": str(capture_path),
                "marker": self.config.block_marker,
                "matched_line": self.block_marker_line,
                "checked_at": self.block_checked_at,
                "recovery_attempts": recovery_attempts,
                "action": (
                    "auto-recovery pending; worker pane preserved for inspection"
                    if self.config.blocked_recovery
                    else "queue stopped; worker pane preserved for inspection"
                ),
            },
        )

    def try_blocked_recovery(self, item: RunItem, capture_path: Path, recovery_attempts: int) -> str:
        if not self.config.blocked_recovery:
            return "blocked"
        if not self.planner_pane:
            self.write_recovery_state(item, "blocked", "missing planner pane", recovery_attempts)
            return "blocked"
        if not self.config.blocked_recovery_session_id:
            self.write_recovery_state(item, "blocked", "missing blocked_recovery_session_id", recovery_attempts)
            return "blocked"
        if recovery_attempts >= max(0, self.config.blocked_recovery_max_attempts):
            self.write_recovery_state(item, "blocked", "recovery attempts exhausted", recovery_attempts)
            return "blocked"

        prompt_file = self.write_recovery_prompt_file(item, capture_path, recovery_attempts)
        self.phase = "blocked recovery launching"
        self.remaining_seconds = None
        self.render()
        tmux("respawn-pane", "-k", "-t", self.planner_pane, "-c", str(self.config.project_dir), "bash")
        self.sleep_with_controls(1, "planner fresh shell")
        self.send_planner_shell_command(
            build_resume_prompt_command(
                self.config.blocked_recovery_command,
                self.config.blocked_recovery_session_id,
                prompt_file,
            )
        )

        recovery_result = self.wait_for_blocked_recovery(item, recovery_attempts)
        if recovery_result == "proceed":
            action = self.config.blocked_recovery_action
            self.write_recovery_state(item, action, self.config.blocked_recovery_success_marker, recovery_attempts)
            return action
        if recovery_result == "human":
            self.write_recovery_state(item, "blocked", self.config.blocked_recovery_human_marker, recovery_attempts)
            return "blocked"
        self.write_recovery_state(item, "blocked", recovery_result, recovery_attempts)
        return "blocked"

    def write_recovery_prompt_file(self, item: RunItem, capture_path: Path, recovery_attempts: int) -> Path:
        safe_name = sanitize_name(item.prompt_name)
        prompt_file = self.recovery_prompt_dir / f"{item.index:03d}-{safe_name}-attempt-{recovery_attempts + 1}.txt"
        blocked_path = self.config.runtime_dir / "blocked.json"
        prompt_file.write_text(
            "\n".join(
                [
                    "The prompt-queue executor stopped because it output DO-NOT-PROCEED.",
                    "",
                    f"Target repo: {self.config.project_dir}",
                    f"Runtime dir: {self.config.runtime_dir}",
                    f"Blocked metadata: {blocked_path}",
                    f"Failed executor capture: {capture_path}",
                    f"Prompt index: {item.index}",
                    f"Prompt name: {item.prompt_name}",
                    f"Recovery attempt: {recovery_attempts + 1} of {self.config.blocked_recovery_max_attempts}",
                    "",
                    "Inspect the blocked metadata, failed capture, and current worktree.",
                    "If the issue is mechanical, local, and safely fixable, fix it and run focused verification.",
                    "Do not make product decisions, destructive git changes, credential changes, or irreversible data changes.",
                    "If human judgment is needed, do not guess.",
                    "",
                    f"If you fixed the issue and the queue may continue, write exactly {self.config.blocked_recovery_success_marker} on its own final content line.",
                    f"If human judgment is needed, write exactly {self.config.blocked_recovery_human_marker} on its own final content line.",
                    "The controller will only read the marker after your Codex pane is Ready.",
                ]
            )
        )
        write_json(
            self.config.runtime_dir / "last-recovery-prompt.json",
            {
                "run_index": item.index,
                "name": item.prompt_name,
                "path": str(prompt_file),
                "attempt": recovery_attempts + 1,
            },
        )
        return prompt_file

    def wait_for_blocked_recovery(self, item: RunItem, recovery_attempts: int) -> str:
        ready_interval = max(1, self.config.ready_check_seconds)
        next_ready_check = time.time() + ready_interval
        while True:
            self.phase = "blocked recovery"
            self.remaining_seconds = None
            self.render()
            self.handle_keyboard()
            if consume_finish_current_sleep(self.config.runtime_dir):
                self.remaining_seconds = None
                self.phase = "blocked recovery finished manually"
                self.render()
                return "finish"
            if time.time() >= next_ready_check:
                result = self.check_recovery_marker(item, recovery_attempts)
                if result in {"proceed", "human", "ready-without-marker"}:
                    self.remaining_seconds = None
                    self.phase = f"blocked recovery {result}"
                    self.render()
                    return result
                next_ready_check = time.time() + ready_interval
            time.sleep(1)

    def check_recovery_marker(self, item: RunItem, recovery_attempts: int) -> str:
        captured = self.capture_planner_tail(self.config.ready_check_lines)
        non_empty_lines = [line for line in captured.splitlines() if line.strip()]
        last_line = non_empty_lines[-1] if non_empty_lines else ""
        matched_ready = next((marker for marker in self.config.ready_markers if marker in last_line), "")
        checked_at = datetime.now().isoformat(timespec="seconds")
        self.last_recovery_check_line = last_line
        self.last_recovery_check_at = checked_at

        recovery_line = ""
        result = "waiting"
        if matched_ready:
            marker_captured = self.capture_planner_tail(self.config.blocked_recovery_check_lines)
            marker_lines = [line.strip() for line in marker_captured.splitlines() if line.strip()]
            recent_lines = marker_lines[-self.config.blocked_recovery_check_lines :]
            if self.config.blocked_recovery_human_marker in recent_lines:
                recovery_line = self.config.blocked_recovery_human_marker
                result = "human"
            elif self.config.blocked_recovery_success_marker in recent_lines:
                recovery_line = self.config.blocked_recovery_success_marker
                result = "proceed"
            else:
                result = "ready-without-marker"
            self.recovery_detected = bool(recovery_line)
            self.recovery_marker_line = recovery_line or None

        safe_name = sanitize_name(item.prompt_name)
        log_path = self.recovery_check_dir / f"{item.index:03d}-{safe_name}.jsonl"
        with log_path.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "checked_at": checked_at,
                        "run_index": item.index,
                        "name": item.prompt_name,
                        "attempt": recovery_attempts + 1,
                        "last_line": last_line,
                        "ready_matched": bool(matched_ready),
                        "matched_ready_marker": matched_ready,
                        "result": result,
                        "recovery_marker": recovery_line,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        return result

    def write_recovery_state(self, item: RunItem, action: str, reason: str, recovery_attempts: int) -> None:
        write_json(
            self.config.runtime_dir / "recovery.json",
            {
                "run_index": item.index,
                "name": item.prompt_name,
                "action": action,
                "reason": reason,
                "attempt": recovery_attempts + 1,
                "checked_at": self.last_recovery_check_at,
                "last_line": self.last_recovery_check_line,
                "marker_line": self.recovery_marker_line,
            },
        )

    def notify_completion(self) -> None:
        if not self.planner_pane:
            self.write_completion_state("skipped", "missing planner pane")
            return
        if not self.config.completion_notify_session_id:
            self.write_completion_state("skipped", "missing completion_notify_session_id")
            return

        prompt_file = self.write_completion_prompt_file()
        self.phase = "completion notify launching"
        self.remaining_seconds = None
        self.render()
        tmux("respawn-pane", "-k", "-t", self.planner_pane, "-c", str(self.config.project_dir), "bash")
        self.sleep_with_controls(1, "planner fresh shell")
        self.send_planner_shell_command(
            build_resume_prompt_command(
                self.config.completion_notify_command,
                self.config.completion_notify_session_id,
                prompt_file,
            )
        )
        result = self.wait_for_completion_notify()
        self.completion_notify_done = result == "ready"
        self.write_completion_state(result, "planner reached Ready" if result == "ready" else result)

    def write_completion_prompt_file(self) -> Path:
        prompt_file = self.completion_prompt_dir / "verify-completed-run.txt"
        default_instructions = [
            "Verify the original plan is complete against the current worktree.",
            "Inspect the executor captures and run focused verification commands.",
            "If you find implementation errors or missing work, make the needed changes and verify them.",
            "Do not commit unless explicitly requested.",
        ]
        completion_instructions = (
            ["Follow these workflow-specific completion instructions:", "", self.config.completion_prompt]
            if self.config.completion_prompt
            else default_instructions
        )
        prompt_file.write_text(
            "\n".join(
                [
                    "The prompt-queue executor finished all queued prompts.",
                    "",
                    f"Target repo: {self.config.project_dir}",
                    f"Runtime dir: {self.config.runtime_dir}",
                    f"Queue metadata: {self.config.runtime_dir / 'queue.json'}",
                    f"State file: {self.config.runtime_dir / 'state.json'}",
                    f"Executor captures: {self.capture_dir}",
                    f"Executor prompts: {self.prompt_dir}",
                    "",
                    *completion_instructions,
                    "",
                    "When finished, provide a concise verification summary and let Codex return to Ready.",
                ]
            )
        )
        write_json(
            self.config.runtime_dir / "last-completion-prompt.json",
            {"path": str(prompt_file), "prompt_count": len(self.queue)},
        )
        return prompt_file

    def wait_for_completion_notify(self) -> str:
        ready_interval = max(1, self.config.ready_check_seconds)
        next_ready_check = time.time() + ready_interval
        while True:
            self.phase = "completion notify"
            self.remaining_seconds = None
            self.render()
            self.handle_keyboard()
            if consume_finish_current_sleep(self.config.runtime_dir):
                self.remaining_seconds = None
                self.phase = "completion notify finished manually"
                self.render()
                return "finish"
            if time.time() >= next_ready_check:
                result = self.check_completion_ready()
                if result == "ready":
                    self.remaining_seconds = None
                    self.phase = "completion notify ready"
                    self.render()
                    return "ready"
                next_ready_check = time.time() + ready_interval
            time.sleep(1)

    def check_completion_ready(self) -> str:
        captured = self.capture_planner_tail(self.config.ready_check_lines)
        non_empty_lines = [line for line in captured.splitlines() if line.strip()]
        last_line = non_empty_lines[-1] if non_empty_lines else ""
        matched_ready = next((marker for marker in self.config.ready_markers if marker in last_line), "")
        checked_at = datetime.now().isoformat(timespec="seconds")
        self.last_completion_check_line = last_line
        self.last_completion_check_at = checked_at

        log_path = self.completion_check_dir / "verify-completed-run.jsonl"
        with log_path.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "checked_at": checked_at,
                        "last_line": last_line,
                        "ready_matched": bool(matched_ready),
                        "matched_ready_marker": matched_ready,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        return "ready" if matched_ready else "waiting"

    def write_completion_state(self, status: str, reason: str) -> None:
        write_json(
            self.config.runtime_dir / "completion.json",
            {
                "status": status,
                "reason": reason,
                "checked_at": self.last_completion_check_at,
                "last_line": self.last_completion_check_line,
                "prompt_count": len(self.queue),
            },
        )

    def sleep_with_controls(self, seconds: int, phase: str, ready_item: RunItem | None = None) -> str:
        if ready_item is not None:
            return self.wait_for_worker_ready(phase, ready_item)
        deadline = time.time() + max(0, seconds)
        while True:
            remaining = int(round(deadline - time.time()))
            if remaining <= 0:
                self.remaining_seconds = None
                self.phase = phase
                self.render()
                return "elapsed"
            self.phase = phase
            self.remaining_seconds = remaining
            self.render()
            self.handle_keyboard()
            if consume_finish_current_sleep(self.config.runtime_dir):
                self.remaining_seconds = None
                self.phase = f"{phase} finished early"
                self.render()
                return "finish"
            time.sleep(min(1, remaining))

    def wait_for_worker_ready(self, phase: str, ready_item: RunItem) -> str:
        ready_interval = max(1, self.config.ready_check_seconds)
        next_ready_check = time.time() + ready_interval
        while True:
            self.phase = phase
            self.remaining_seconds = None
            self.render()
            self.handle_keyboard()
            if consume_finish_current_sleep(self.config.runtime_dir):
                self.remaining_seconds = None
                self.phase = f"{phase} finished manually"
                self.render()
                return "finish"
            if time.time() >= next_ready_check:
                ready_result = self.check_ready_marker(ready_item)
                if ready_result == "blocked":
                    self.remaining_seconds = None
                    self.phase = f"{phase} blocked"
                    self.render()
                    return "blocked"
                if ready_result == "ready":
                    self.remaining_seconds = None
                    self.phase = f"{phase} ready"
                    self.render()
                    return "ready"
                next_ready_check = time.time() + ready_interval
            time.sleep(1)

    def handle_keyboard(self) -> None:
        while select.select([sys.stdin], [], [], 0)[0]:
            key = sys.stdin.read(1)
            if key in {"q", "Q"}:
                tmux("kill-session", "-t", self.session, check=False)
                raise SystemExit(0)
            if key in {"s", "S"}:
                toggle_stop_after_current(self.config.runtime_dir)
                self.render()
            if key in {"f", "F"}:
                (self.config.runtime_dir / FINISH_SLEEP_FLAG).write_text("1\n")
                self.render()

    def render(self) -> None:
        write_json(
            self.config.runtime_dir / "state.json",
            {
                "session": self.session,
                "worker_pane": self.worker_pane,
                "planner_pane": self.planner_pane,
                "phase": self.phase,
                "current_index": self.current_index,
                "completed": sorted(self.completed),
                "stop_after_current": should_stop_after_current(self.config.runtime_dir),
                "finish_current_sleep": should_finish_current_sleep(self.config.runtime_dir),
                "remaining_seconds": self.remaining_seconds,
                "last_ready_check_at": self.last_ready_check_at,
                "last_ready_check_line": self.last_ready_check_line,
                "ready_detected": self.ready_detected,
                "block_detected": self.block_detected,
                "block_marker_line": self.block_marker_line,
                "block_checked_at": self.block_checked_at,
                "last_recovery_check_at": self.last_recovery_check_at,
                "last_recovery_check_line": self.last_recovery_check_line,
                "recovery_detected": self.recovery_detected,
                "recovery_marker_line": self.recovery_marker_line,
                "last_completion_check_at": self.last_completion_check_at,
                "last_completion_check_line": self.last_completion_check_line,
                "completion_notify_done": self.completion_notify_done,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        print("\033[2J\033[H", end="")
        total_elapsed = int(time.monotonic() - self.run_started_at)
        prompt_elapsed = (
            int(time.monotonic() - self.current_prompt_started_at)
            if self.current_prompt_started_at is not None
            else None
        )
        self.prompt_list_file.write_text(
            render_prompt_list(
                self.queue,
                self.current_index,
                self.completed,
                self.prompt_durations,
            )
            + "\n"
        )
        print(
            render_status(
                self.queue,
                self.current_index,
                self.completed,
                should_stop_after_current(self.config.runtime_dir),
                self.phase,
                self.remaining_seconds,
                total_elapsed,
                prompt_elapsed,
                self.last_ready_check_at,
                self.last_ready_check_line,
                self.prompt_durations,
                self.config.project_dir.name,
            ),
            flush=True,
        )


def run_controller(args: argparse.Namespace) -> None:
    runtime_dir_override = Path(args.runtime_dir) if args.runtime_dir else None
    config = load_config(
        Path(args.config),
        runtime_dir_override=runtime_dir_override,
    )
    if args.queue_file:
        config = replace(config, prompts=read_prompt_queue(Path(args.queue_file)))

    old_attrs = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        controller = Controller(
            config,
            args.session,
            args.worker_pane,
            args.planner_pane,
            queue_id=args.queue_id,
            queue_dir_path=Path(args.queue_dir).resolve() if args.queue_dir else None,
            archive_root=Path(args.archive_root).resolve() if args.archive_root else None,
            archive_stamp=args.archive_stamp,
        )
        controller.run()
        while True:
            select.select([sys.stdin], [], [], 1)
            controller.handle_keyboard()
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)


def start_session(args: argparse.Namespace) -> None:
    if shutil.which("tmux") is None:
        raise SystemExit("prompt-queue: tmux is required")

    queue_id = validate_queue_id(args.queue_id) if getattr(args, "queue_id", "") else ""
    config_path = config_path_for_args(args)
    initial_config = apply_agent_override(load_config(config_path), args.cld)
    if not initial_config.prompts:
        raise SystemExit(f"prompt-queue: configure prompts or prompt_files in {config_path.name}")
    if initial_config.blocked_recovery and not initial_config.blocked_recovery_session_id:
        raise SystemExit("prompt-queue: blocked_recovery requires blocked_recovery_session_id")
    if initial_config.completion_notify and not initial_config.completion_notify_session_id:
        raise SystemExit("prompt-queue: completion_notify requires completion_notify_session_id")

    start_stamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    config = replace(
        initial_config,
        runtime_dir=make_runtime_dir(initial_config.project_dir, start_stamp, queue_id),
    )
    if not config.project_dir.is_dir():
        raise SystemExit(f"project_dir does not exist: {config.project_dir}")

    resume_completed: set[int] = set()
    resume_runtime_dir: Path | None = None
    try:
        resume_runtime_dir = latest_runtime_dir(config.project_dir, queue_id)
        resume_completed = read_resumable_completed_indices(resume_runtime_dir, config.prompts)
    except SystemExit:
        resume_completed = set()

    session_parts = [config.session_name]
    if queue_id:
        session_parts.append(sanitize_name(queue_id))
    session_parts.append(sanitize_name(config.project_dir.name))
    session = args.session or "-".join(session_parts)
    existing_session = tmux_session_exists(session)
    plan = build_run_plan(config, session, resume_runtime_dir, existing_session, resume_completed, queue_id)
    no_confirm = bool(getattr(args, "no_confirm", False))
    if existing_session:
        controller_running = controller_process_is_running(resume_runtime_dir, session)
        if controller_running is False:
            print(render_run_preflight(plan))
            print("")
            print("Stale tmux session found; replacing its stopped controller automatically.")
            action = "replace"
        else:
            action = prompt_existing_session_action(plan) if sys.stdin.isatty() else "attach"
        if action == "attach":
            attach_session(session)
            return
        if action == "quit":
            return
    else:
        if sys.stdin.isatty() and not no_confirm and not prompt_start_new_run(plan):
            return
        if not sys.stdin.isatty():
            print(render_run_preflight(plan))
            print("")

    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    (config.runtime_dir / STOP_NEXT_FLAG).unlink(missing_ok=True)
    (config.runtime_dir / FINISH_SLEEP_FLAG).unlink(missing_ok=True)
    queue_file = config.runtime_dir / "queue.json"
    write_prompt_queue(queue_file, config.prompts)
    write_progress_snapshot(config.runtime_dir, resume_completed)

    stale_session_to_kill = prepare_tmux_session_replacement(session) if existing_session else ""

    term_w = str(shutil.get_terminal_size((200, 50)).columns)
    term_h = str(shutil.get_terminal_size((200, 50)).lines)
    script = Path(__file__).resolve()
    prompt_list_file = config.runtime_dir / PROMPT_LIST_FILE
    prompt_list_file.write_text("Prompt List\n\nstarting controller...\n")

    controller_id = tmux(
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        "queue",
        "-c",
        str(config.project_dir),
        "-x",
        term_w,
        "-y",
        term_h,
        "-P",
        "-F",
        "#{pane_id}",
        "bash",
        capture=True,
    ).stdout.strip()
    sync_tmux_session_environment()
    prompt_list_id = tmux(
        "split-window",
        "-t",
        controller_id,
        "-h",
        "-c",
        str(config.project_dir),
        "-P",
        "-F",
        "#{pane_id}",
        "bash",
        capture=True,
    ).stdout.strip()
    tmux("send-keys", "-t", prompt_list_id, build_prompt_list_watch_command(prompt_list_file), "Enter")

    planner_id = ""
    if config.blocked_recovery or config.completion_notify:
        planner_id = tmux(
            "split-window",
            "-t",
            controller_id,
            "-v",
            "-p",
            "75",
            "-c",
            str(config.project_dir),
            "-P",
            "-F",
            "#{pane_id}",
            "bash",
            capture=True,
        ).stdout.strip()
    worker_id = tmux(
        "split-window",
        "-t",
        prompt_list_id,
        "-v",
        "-p",
        "75",
        "-c",
        str(config.project_dir),
        "-P",
        "-F",
        "#{pane_id}",
        "bash",
        capture=True,
    ).stdout.strip()

    tmux("set-option", "-t", session, "history-limit", str(config.history_limit))
    tmux("set-option", "-t", session, "mouse", "on")
    tmux("set-option", "-t", session, "status", "on")
    tmux("set-option", "-t", session, "status-position", "top")
    queue_label = f"{queue_id} · " if queue_id else ""
    tmux("set-option", "-t", session, "status-left", f"#[fg=cyan,bold] prompt-queue {queue_label}{config.project_dir.name} ")
    tmux("set-option", "-t", session, "status-right", "")

    controller_cmd = (
        f"python3 {sh_quote(str(script))} "
        f"--config {sh_quote(str(config_path))} "
        f"__controller "
        f"--session {sh_quote(session)} "
        f"--worker-pane {sh_quote(worker_id)} "
        f"--planner-pane {sh_quote(planner_id)} "
        f"--runtime-dir {sh_quote(str(config.runtime_dir))} "
        f"--queue-file {sh_quote(str(queue_file))} "
        f"--queue-id {sh_quote(queue_id)} "
        f"--queue-dir {sh_quote(str(config_path.parent) if queue_id else '')} "
        f"--archive-root {sh_quote(str(ARCHIVE_DIR) if queue_id else '')} "
        f"--archive-stamp {sh_quote(start_stamp)}"
    )
    tmux("select-pane", "-t", controller_id)

    write_json(
        config.runtime_dir / "session.json",
        {
            "session": session,
            "queue_id": queue_id,
            "queue_config": str(config_path),
            "worker_pane": worker_id,
            "prompt_list_pane": prompt_list_id,
            "planner_pane": planner_id,
            "project_dir": str(config.project_dir),
            "runtime_dir": str(config.runtime_dir),
            "queue_file": str(queue_file),
            "started_at": start_stamp,
            "prompt_count": len(config.prompts),
            "command": config.command,
            "prompt_delivery": config.prompt_delivery,
            "blocked_recovery": config.blocked_recovery,
            "blocked_recovery_session_id": config.blocked_recovery_session_id,
            "completion_notify": config.completion_notify,
            "completion_notify_session_id": config.completion_notify_session_id,
        },
    )
    if stale_session_to_kill and not args.no_attach:
        tmux("switch-client", "-t", session)
    if stale_session_to_kill:
        controller_cmd = (
            f"tmux kill-session -t {sh_quote(stale_session_to_kill)}; "
            f"{controller_cmd}"
        )
    tmux("send-keys", "-t", controller_id, controller_cmd, "Enter")
    if stale_session_to_kill:
        return
    if not args.no_attach:
        attach_session(session)


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def attach_session(session: str) -> None:
    if os.environ.get("TMUX"):
        os.execvp("tmux", ["tmux", "switch-client", "-t", session])
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])


def read_session(runtime_dir: Path) -> str:
    session_file = runtime_dir / "session.json"
    if not session_file.exists():
        raise SystemExit(f"no session metadata at {session_file}")
    return str(json.loads(session_file.read_text())["session"])


def load_config_for_control(args: argparse.Namespace) -> Config:
    return load_config(config_path_for_args(args))


def latest_runtime_dir_for_args(args: argparse.Namespace, config: Config) -> Path:
    return latest_runtime_dir(config.project_dir, getattr(args, "queue_id", ""))


def stop_next(args: argparse.Namespace) -> None:
    config = load_config_for_control(args)
    runtime_dir = latest_runtime_dir_for_args(args, config)
    armed = toggle_stop_after_current(runtime_dir)
    state = "armed" if armed else "disarmed"
    print(f"{state} stop-after-current: {runtime_dir / STOP_NEXT_FLAG}")


def finish_sleep(args: argparse.Namespace) -> None:
    config = load_config_for_control(args)
    runtime_dir = latest_runtime_dir_for_args(args, config)
    (runtime_dir / FINISH_SLEEP_FLAG).write_text("1\n")
    print(f"armed finish-current-wait: {runtime_dir / FINISH_SLEEP_FLAG}")


def kill(args: argparse.Namespace) -> None:
    config = load_config_for_control(args)
    runtime_dir = latest_runtime_dir_for_args(args, config)
    session = args.session or read_session(runtime_dir)
    tmux("kill-session", "-t", session, check=False)
    print(f"killed tmux session: {session}")


def status(args: argparse.Namespace) -> None:
    config = load_config_for_control(args)
    runtime_dir = latest_runtime_dir_for_args(args, config)
    state_file = runtime_dir / "state.json"
    if not state_file.exists():
        raise SystemExit(f"no state file at {state_file}")
    print(state_file.read_text(), end="")


def collect_prompts(args: argparse.Namespace) -> None:
    config_path = config_path_for_args(args)
    collected: list[tuple[str, str]] = []

    print(f"Writing prompts into {config_path.parent / 'prompts'}")
    print(f"Paste each prompt, then finish it with a line containing only {args.end_marker!r}.")
    print(f"When asked for the next prompt, type {NO_MORE_PROMPTS!r} to finish.")

    index = 1
    while True:
        name = f"prompt-{index}"
        print(f"\nprompt {index}: paste text; end with {args.end_marker!r} on its own line.")
        print(f"or type {NO_MORE_PROMPTS!r} on its own line to finish:")
        lines: list[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                line = args.end_marker
            if not lines and line.strip().lower() == NO_MORE_PROMPTS:
                lines = []
                break
            if line == args.end_marker:
                break
            lines.append(line)

        text = "\n".join(lines).strip()
        if not lines and not text:
            break
        if text:
            collected.append((name, text))
            index += 1
        else:
            print("empty prompt skipped")

    written_paths = write_collected_prompts(config_path, collected, append=args.append)
    if not written_paths:
        print("no prompts collected; config unchanged")
        return

    action = "appended" if args.append else "wrote"
    print(f"{action} {len(written_paths)} prompt file(s) and updated {config_path}")
    for path in written_paths:
        print(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prompt-queue")
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="path to config file; defaults to config.local.json when present, otherwise config.json",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start or attach to a tagged tmux prompt queue session")
    run.add_argument("queue_id", help="queue folder name under queues/")
    run.add_argument("--session", default="", help="override tmux session name")
    run.add_argument("--no-attach", action="store_true", help="create session without attaching")
    run.add_argument(
        "-y",
        "--yes",
        "--no-confirm",
        dest="no_confirm",
        action="store_true",
        help="start a new run without asking for interactive confirmation",
    )
    run.add_argument("--cld", action="store_true", help="run Claude via cld and paste prompts instead of Codex")
    run.set_defaults(func=start_session)

    controller = sub.add_parser("__controller")
    controller.add_argument("--session", required=True)
    controller.add_argument("--worker-pane", required=True)
    controller.add_argument("--planner-pane", default="")
    controller.add_argument("--runtime-dir", default="")
    controller.add_argument("--queue-file", default="")
    controller.add_argument("--queue-id", default="")
    controller.add_argument("--queue-dir", default="")
    controller.add_argument("--archive-root", default="")
    controller.add_argument("--archive-stamp", default="")
    controller.set_defaults(func=run_controller)

    attach = sub.add_parser("attach", help="attach to a tagged queue's last session")
    attach.add_argument("queue_id", help="queue folder name under queues/")
    attach.set_defaults(
        func=lambda args: attach_session(read_session(latest_runtime_dir_for_args(args, load_config_for_control(args))))
    )

    stop = sub.add_parser("stop-next", help="finish current prompt, then stop")
    stop.add_argument("queue_id", help="queue folder name under queues/")
    stop.set_defaults(func=stop_next)

    finish = sub.add_parser("finish-sleep", help="finish the current controller wait immediately")
    finish.add_argument("queue_id", help="queue folder name under queues/")
    finish.set_defaults(func=finish_sleep)

    kill_cmd = sub.add_parser("kill", help="kill the tmux session now")
    kill_cmd.add_argument("queue_id", help="queue folder name under queues/")
    kill_cmd.add_argument("--session", default="", help="override tmux session name")
    kill_cmd.set_defaults(func=kill)

    stat = sub.add_parser("status", help="print JSON controller state")
    stat.add_argument("queue_id", help="queue folder name under queues/")
    stat.set_defaults(func=status)

    collect = sub.add_parser("collect-prompts", help="paste prompts into files and update config.json")
    collect.add_argument("queue_id", help="queue folder name under queues/")
    collect.add_argument("--append", action="store_true", help="append to existing config prompts instead of replacing them")
    collect.add_argument("--end-marker", default=DEFAULT_PROMPT_END_MARKER, help="line that ends one pasted prompt")
    collect.set_defaults(func=collect_prompts)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
