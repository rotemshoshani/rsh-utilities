#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import termios
import time
import traceback
import tty
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(__file__).with_name("config.json")
STOP_NEXT_FLAG = "stop-next.flag"
FINISH_SLEEP_FLAG = "finish-sleep.flag"
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
RESOLVED_CONFIG_NAME = "run-config.json"
DEFAULT_REVIEWER_PROMPT = """You are the final reviewer for this advisor run against repository {project_dir}.

Read all findings, reports, fix plans, optimization plans, captures, and any other useful files under {advisor_dir}. Your job is to turn the advisor output into a clear way forward.

Do not blindly accept every finding. Merge duplicates, drop weak or unsupported claims, call out contradictions, and add important missing observations only when the evidence supports them.

Write exactly one self-contained HTML file:
- {advisor_dir}/final-review.html

If there are no errors or actionable findings, still write the HTML file and clearly say that nothing needs action. In that case, also output the complete absolute file path from the filesystem root:
{final_review_path}

The HTML should be simple to understand and good looking without external assets. Use readable typography, clear sections, subtle colors, and tables or callouts where they help. Explain major decisions and findings in very simple terms, as if the reader is busy and wants to know what matters, why it matters, and what to do next.

Include:
- a short executive summary
- the recommended order of work
- accepted findings with plain-language explanations
- dropped or downgraded findings with reasons
- any important missing items you added
- risks, dependencies, and quick wins
- direct references to source advisor files and code/config evidence where available

Keep it concise, practical, and evidence-based."""


@dataclass(frozen=True)
class Agent:
    name: str
    command: str
    command_name: str = ""
    model_label: str = ""
    model: str = ""
    completion_marker: str = ""
    completion_file: str = ""
    startup_keys: tuple[str, ...] = ()
    submit_keys: tuple[str, ...] = ("Enter",)
    prompt_delivery: str = "paste"


@dataclass(frozen=True)
class ModelOption:
    label: str
    model: str = ""
    args: tuple[str, ...] = ()
    env_file: str = ""
    env_key: str = ""


@dataclass(frozen=True)
class CommandConfig:
    name: str
    command: str
    prompt_delivery: str
    model_arg: str = "--model"
    completion_marker: str = ""
    completion_file: str = ""
    startup_keys: tuple[str, ...] = ()
    submit_keys: tuple[str, ...] = ("Enter",)
    models: tuple[ModelOption, ...] = ()


@dataclass(frozen=True)
class Config:
    project_dir: Path
    runtime_dir: Path
    topic: str
    topic_label: str
    work_dir_name: str
    num_runs: int
    run_seconds: int
    startup_wait_seconds: int
    post_startup_wait_seconds: int
    capture_lines: int
    ready_check_seconds: int
    ready_check_lines: int
    ready_markers: tuple[str, ...]
    ready_command_names: tuple[str, ...]
    history_limit: int
    session_name: str
    prompt: str
    required_artifacts: tuple[str, ...]
    required_artifact_marker: str
    required_artifact_min_bytes: int
    incomplete_artifact_markers: tuple[str, ...]
    max_incomplete_continuations: int
    reviewer_artifact_min_bytes: int
    agents: tuple[Agent, ...]
    reviewer_agent: Agent | None = None
    reviewer_prompt: str = ""
    reviewer_run_seconds: int = 1500


@dataclass(frozen=True)
class RunItem:
    index: int
    cycle: int
    agent_name: str
    command: str
    command_name: str = ""
    model_label: str = ""
    model: str = ""
    completion_marker: str = ""
    completion_file: str = ""
    startup_keys: tuple[str, ...] = ()
    submit_keys: tuple[str, ...] = ("Enter",)
    prompt_delivery: str = "paste"
    prompt_kind: str = "advisor"


def expand_project_path(value: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def parse_model_option(item: dict[str, Any]) -> ModelOption:
    utilities_dir = str(DEFAULT_CONFIG.parent.parent.resolve())
    advisor_dir = str(DEFAULT_CONFIG.parent.resolve())
    return ModelOption(
        label=str(item.get("label", item.get("model", "default"))),
        model=str(item.get("model", "")),
        args=tuple(str(arg).replace("{advisor_dir}", advisor_dir) for arg in item.get("args", [])),
        env_file=str(item.get("env_file", "")).replace("{utilities_dir}", utilities_dir),
        env_key=str(item.get("env_key", "")),
    )


def parse_command_config(item: dict[str, Any]) -> CommandConfig:
    return CommandConfig(
        name=str(item["name"]),
        command=str(item["command"]),
        prompt_delivery=str(item.get("prompt_delivery", "paste")),
        model_arg=str(item.get("model_arg", "--model")),
        completion_marker=str(item.get("completion_marker", "")),
        completion_file=str(item.get("completion_file", "")),
        startup_keys=tuple(str(key) for key in item.get("startup_keys", [])),
        submit_keys=tuple(str(key) for key in item.get("submit_keys", ["Enter"])),
        models=tuple(parse_model_option(model) for model in item.get("models", [])),
    )


def parse_agent(item: dict[str, Any]) -> Agent:
    return Agent(
        name=str(item["name"]),
        command=str(item["command"]),
        command_name=str(item.get("command_name", "")),
        model_label=str(item.get("model_label", item.get("command_label", ""))),
        model=str(item.get("model", "")),
        completion_marker=str(item.get("completion_marker", "")),
        completion_file=str(item.get("completion_file", "")),
        startup_keys=tuple(str(key) for key in item.get("startup_keys", [])),
        submit_keys=tuple(str(key) for key in item.get("submit_keys", ["Enter"])),
        prompt_delivery=str(item.get("prompt_delivery", "paste")),
    )


def escape_format_text(value: str) -> str:
    return value.replace("{", "{{").replace("}", "}}")


def base_commands(raw: dict[str, Any]) -> dict[str, CommandConfig]:
    return {parse_command_config(item).name: parse_command_config(item) for item in raw.get("commands", raw.get("agents", []))}


def topic_available_commands(raw: dict[str, Any], topic_raw: dict[str, Any]) -> dict[str, CommandConfig]:
    commands = base_commands(raw)
    for item in topic_raw.get("commands", []):
        command = parse_command_config(item)
        commands[command.name] = command
    return commands


def build_model_command(command: CommandConfig, model: ModelOption) -> str:
    if model.args:
        model_command = " ".join([command.command, *(shlex.quote(arg) for arg in model.args)])
    elif model.model:
        model_command = f"{command.command} {shlex.quote(command.model_arg)} {shlex.quote(model.model)}"
    else:
        model_command = command.command
    if model.env_file and model.env_key:
        return build_sourced_env_command(model_command, Path(model.env_file), model.env_key)
    return model_command


def build_sourced_env_command(command: str, env_file: Path, env_key: str) -> str:
    if not env_key or not (env_key[0].isalpha() or env_key[0] == "_") or not all(
        character.isalnum() or character == "_" for character in env_key
    ):
        raise ValueError(f"invalid environment key name: {env_key!r}")
    env_path = sh_quote(str(env_file.expanduser().resolve()))
    return (
        f"source -- {env_path}; "
        f': "${{{env_key}:?{env_key} is missing from {env_path}}}"; '
        f"export {env_key}; {command}"
    )


def sanitize_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-")
    return cleaned or "default"


def agent_from_model(command: CommandConfig, model: ModelOption) -> Agent:
    model_name = model.model or model.label
    return Agent(
        name=f"{sanitize_name(command.name)}-{sanitize_name(model_name)}",
        command=build_model_command(command, model),
        command_name=command.name,
        model_label=model.label,
        model=model.model,
        completion_marker=command.completion_marker,
        completion_file=command.completion_file,
        startup_keys=command.startup_keys,
        submit_keys=command.submit_keys,
        prompt_delivery=command.prompt_delivery,
    )


def agent_to_json(agent: Agent) -> dict[str, Any]:
    value: dict[str, Any] = {
        "name": agent.name,
        "command": agent.command,
        "prompt_delivery": agent.prompt_delivery,
    }
    if agent.command_name:
        value["command_name"] = agent.command_name
    if agent.model_label:
        value["model_label"] = agent.model_label
    if agent.model:
        value["model"] = agent.model
    if agent.completion_marker:
        value["completion_marker"] = agent.completion_marker
    if agent.completion_file:
        value["completion_file"] = agent.completion_file
    if agent.startup_keys:
        value["startup_keys"] = list(agent.startup_keys)
    if agent.submit_keys != ("Enter",):
        value["submit_keys"] = list(agent.submit_keys)
    return value


def load_config(
    path: Path = DEFAULT_CONFIG,
    project_dir_override: Path | None = None,
    runtime_dir_override: Path | None = None,
    topic_override: str | None = None,
) -> Config:
    path = path.expanduser().resolve()
    raw = json.loads(path.read_text())
    project_dir = (
        project_dir_override.expanduser().resolve()
        if project_dir_override is not None
        else expand_project_path(str(raw.get("project_dir", ".")))
    )

    if raw.get("resolved"):
        return config_from_resolved(raw, project_dir, runtime_dir_override)
    return resolve_config(raw, project_dir, topic_override=topic_override, runtime_dir_override=runtime_dir_override)


def config_from_resolved(
    raw: dict[str, Any],
    project_dir: Path,
    runtime_dir_override: Path | None = None,
) -> Config:
    agents = tuple(parse_agent(item) for item in raw.get("agents", []))
    reviewer_agent = parse_agent(raw["reviewer_agent"]) if raw.get("reviewer_agent") else None
    if not agents:
        raise ValueError("config must define at least one agent in agents")
    num_runs = int(raw.get("num_runs", 1))
    if num_runs < 1:
        raise ValueError("num_runs must be at least 1")
    runtime_dir = runtime_dir_override or Path(str(raw["runtime_dir"])).expanduser().resolve()
    return Config(
        project_dir=project_dir,
        runtime_dir=runtime_dir,
        topic=str(raw.get("topic", "custom")),
        topic_label=str(raw.get("topic_label", raw.get("topic", "custom"))),
        work_dir_name=str(raw.get("work_dir_name", "advisor")),
        num_runs=num_runs,
        run_seconds=int(raw.get("run_seconds", 1500)),
        startup_wait_seconds=int(raw.get("startup_wait_seconds", 10)),
        post_startup_wait_seconds=int(raw.get("post_startup_wait_seconds", 3)),
        capture_lines=int(raw.get("capture_lines", 500)),
        ready_check_seconds=int(raw.get("ready_check_seconds", 60)),
        ready_check_lines=int(raw.get("ready_check_lines", 1)),
        ready_markers=tuple(str(marker) for marker in raw.get("ready_markers", ["Ready"])),
        ready_command_names=tuple(str(name) for name in raw.get("ready_command_names", ["codex"])),
        history_limit=int(raw.get("history_limit", 200000)),
        session_name=str(raw.get("session_name", "advisor")),
        prompt=str(raw.get("prompt", "")),
        required_artifacts=tuple(str(path) for path in raw.get("required_artifacts", [])),
        required_artifact_marker=str(raw.get("required_artifact_marker", "")),
        required_artifact_min_bytes=int(raw.get("required_artifact_min_bytes", 1)),
        incomplete_artifact_markers=tuple(str(marker) for marker in raw.get("incomplete_artifact_markers", [])),
        max_incomplete_continuations=int(raw.get("max_incomplete_continuations", 4)),
        reviewer_artifact_min_bytes=int(raw.get("reviewer_artifact_min_bytes", 1000)),
        agents=agents,
        reviewer_agent=reviewer_agent,
        reviewer_prompt=str(raw.get("reviewer_prompt", "")),
        reviewer_run_seconds=int(raw.get("reviewer_run_seconds", raw.get("run_seconds", 1500))),
    )


def resolve_config(
    raw: dict[str, Any],
    project_dir: Path,
    topic_override: str | None = None,
    runtime_dir_override: Path | None = None,
    selected_agents: tuple[Agent, ...] | None = None,
    reviewer_agent: Agent | None = None,
    num_runs_override: int | None = None,
    run_seconds_override: int | None = None,
    custom_task_description: str = "",
) -> Config:
    topics = raw.get("topics", {})
    topic = topic_override or str(raw.get("default_topic", next(iter(topics), "custom")))
    if topic == "custom":
        topic_raw = dict(raw.get("custom_topic", {}))
        prompt_template = str(topic_raw.get("prompt_template", "{task_description}"))
        prompt = prompt_template.replace("{task_description}", escape_format_text(custom_task_description))
    else:
        if topic not in topics:
            raise ValueError(f"unknown topic: {topic}")
        topic_raw = dict(topics[topic])
        prompt = str(topic_raw.get("prompt", raw.get("prompt", "")))
    prompt_suffix = str(topic_raw.get("prompt_suffix", "")).strip()
    if prompt_suffix:
        prompt = f"{prompt.rstrip()}\n\n{prompt_suffix}"

    available_commands = topic_available_commands(raw, topic_raw)
    if not available_commands:
        raise ValueError("config must define at least one command in commands")

    command_names = topic_raw.get("command_names", topic_raw.get("agent_names"))
    if selected_agents is None:
        if topic_raw.get("agents"):
            agents = tuple(parse_agent(item) for item in topic_raw["agents"])
        else:
            default_models = {
                str(command_name): [str(model) for model in models]
                for command_name, models in topic_raw.get("default_models", {}).items()
            }
            if default_models:
                command_order = [str(name) for name in command_names] if command_names else list(available_commands)
                selected_command_names = [name for name in command_order if name in default_models]
            else:
                selected_command_names = [str(name) for name in command_names] if command_names else list(available_commands)
            selected: list[Agent] = []
            for command_name in selected_command_names:
                if command_name not in available_commands:
                    continue
                command = available_commands[command_name]
                wanted_models = default_models.get(command_name, [])
                models = [
                    model
                    for model in command.models
                    if not wanted_models or model.model in wanted_models or model.label in wanted_models
                ]
                if not models and command.models:
                    models = [command.models[0]]
                for model in models:
                    selected.append(agent_from_model(command, model))
            agents = tuple(selected)
    else:
        agents = selected_agents
    if not agents:
        raise ValueError("resolved config must define at least one selected agent")

    num_runs = int(num_runs_override if num_runs_override is not None else topic_raw.get("num_runs", raw.get("num_runs", 1)))
    if num_runs < 1:
        raise ValueError("num_runs must be at least 1")

    work_dir_name = str(topic_raw.get("work_dir_name", "advisor"))
    runtime_dir = runtime_dir_override or make_runtime_dir(project_dir, work_dir_name)

    return Config(
        project_dir=project_dir,
        runtime_dir=runtime_dir,
        topic=topic,
        topic_label=str(topic_raw.get("label", topic)),
        work_dir_name=work_dir_name,
        num_runs=num_runs,
        run_seconds=int(run_seconds_override if run_seconds_override is not None else topic_raw.get("run_seconds", raw.get("run_seconds", 1500))),
        startup_wait_seconds=int(raw.get("startup_wait_seconds", 10)),
        post_startup_wait_seconds=int(raw.get("post_startup_wait_seconds", 3)),
        capture_lines=int(raw.get("capture_lines", 500)),
        ready_check_seconds=int(raw.get("ready_check_seconds", 60)),
        ready_check_lines=int(raw.get("ready_check_lines", 1)),
        ready_markers=tuple(str(marker) for marker in raw.get("ready_markers", ["Ready"])),
        ready_command_names=tuple(str(name) for name in raw.get("ready_command_names", ["codex"])),
        history_limit=int(raw.get("history_limit", 200000)),
        session_name=str(topic_raw.get("session_name", raw.get("session_name", "advisor"))),
        prompt=prompt,
        required_artifacts=tuple(str(path) for path in topic_raw.get("required_artifacts", [])),
        required_artifact_marker=str(topic_raw.get("required_artifact_marker", "")),
        required_artifact_min_bytes=int(topic_raw.get("required_artifact_min_bytes", 1)),
        incomplete_artifact_markers=tuple(str(marker) for marker in topic_raw.get("incomplete_artifact_markers", [])),
        max_incomplete_continuations=int(raw.get("max_incomplete_continuations", 4)),
        reviewer_artifact_min_bytes=int(raw.get("reviewer_artifact_min_bytes", 1000)),
        agents=agents,
        reviewer_agent=reviewer_agent,
        reviewer_prompt=str(raw.get("reviewer_prompt", DEFAULT_REVIEWER_PROMPT)),
        reviewer_run_seconds=int(raw.get("reviewer_run_seconds", topic_raw.get("reviewer_run_seconds", raw.get("run_seconds", 1500)))),
    )


def config_to_resolved_json(config: Config) -> dict[str, Any]:
    return {
        "resolved": True,
        "project_dir": str(config.project_dir),
        "runtime_dir": str(config.runtime_dir),
        "topic": config.topic,
        "topic_label": config.topic_label,
        "work_dir_name": config.work_dir_name,
        "num_runs": config.num_runs,
        "run_seconds": config.run_seconds,
        "startup_wait_seconds": config.startup_wait_seconds,
        "post_startup_wait_seconds": config.post_startup_wait_seconds,
        "capture_lines": config.capture_lines,
        "ready_check_seconds": config.ready_check_seconds,
        "ready_check_lines": config.ready_check_lines,
        "ready_markers": list(config.ready_markers),
        "ready_command_names": list(config.ready_command_names),
        "history_limit": config.history_limit,
        "session_name": config.session_name,
        "prompt": config.prompt,
        "required_artifacts": list(config.required_artifacts),
        "required_artifact_marker": config.required_artifact_marker,
        "required_artifact_min_bytes": config.required_artifact_min_bytes,
        "incomplete_artifact_markers": list(config.incomplete_artifact_markers),
        "max_incomplete_continuations": config.max_incomplete_continuations,
        "reviewer_artifact_min_bytes": config.reviewer_artifact_min_bytes,
        "agents": [agent_to_json(agent) for agent in config.agents],
        "reviewer_agent": agent_to_json(config.reviewer_agent) if config.reviewer_agent else None,
        "reviewer_prompt": config.reviewer_prompt,
        "reviewer_run_seconds": config.reviewer_run_seconds,
    }


def build_run_queue(config: Config) -> list[RunItem]:
    queue: list[RunItem] = []
    index = 1
    for cycle in range(1, config.num_runs + 1):
        for agent in config.agents:
            queue.append(
                RunItem(
                    index=index,
                    cycle=cycle,
                    agent_name=agent.name,
                    command=agent.command,
                    command_name=agent.command_name,
                    model_label=agent.model_label,
                    model=agent.model,
                    completion_marker=agent.completion_marker,
                    completion_file=agent.completion_file,
                    startup_keys=agent.startup_keys,
                    submit_keys=agent.submit_keys,
                    prompt_delivery=agent.prompt_delivery,
                )
            )
            index += 1
    if config.reviewer_agent is not None:
        agent = config.reviewer_agent
        queue.append(
            RunItem(
                index=index,
                cycle=config.num_runs + 1,
                agent_name=agent.name,
                command=agent.command,
                command_name=agent.command_name,
                model_label=agent.model_label,
                model=agent.model,
                completion_marker=agent.completion_marker,
                completion_file=agent.completion_file,
                startup_keys=agent.startup_keys,
                submit_keys=agent.submit_keys,
                prompt_delivery=agent.prompt_delivery,
                prompt_kind="reviewer",
            )
        )
    return queue


def default_work_base_dir(project_dir: Path, work_dir_name: str = "advisor") -> Path:
    return project_dir / ".planning" / "work" / work_dir_name


def make_runtime_dir(project_dir: Path, work_dir_name: str = "advisor", timestamp: str | None = None) -> Path:
    stamp = timestamp or datetime.now().strftime(TIMESTAMP_FORMAT)
    return default_work_base_dir(project_dir, work_dir_name) / stamp


def configured_work_dir_names(raw: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for topic in raw.get("topics", {}).values():
        names.append(str(topic.get("work_dir_name", "advisor")))
    custom = raw.get("custom_topic", {})
    names.append(str(custom.get("work_dir_name", "advisor")))
    return sorted(set(names))


def latest_runtime_dir(project_dir: Path, work_dir_names: list[str] | None = None) -> Path:
    names = work_dir_names or ["advisor", "arch-advisor", "hygiene-advisor", "sec-advisor"]
    candidates: list[Path] = []
    for name in names:
        base_dir = default_work_base_dir(project_dir, name)
        if base_dir.exists():
            candidates.extend(path for path in base_dir.iterdir() if path.is_dir())
    candidates = [path for path in candidates if (path / "session.json").exists() or (path / "state.json").exists()]
    if not candidates:
        searched = ", ".join(str(default_work_base_dir(project_dir, name)) for name in names)
        raise SystemExit(f"no advisor runs found under {searched}")
    return sorted(candidates, key=lambda path: (path.name, str(path)))[-1]


def build_worker_cd_command(project_dir: Path) -> str:
    return f"cd -- {sh_quote(str(project_dir))}"


def should_stop_after_current(runtime_dir: Path) -> bool:
    return (runtime_dir / STOP_NEXT_FLAG).exists()


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
    title: str = "advisor",
) -> str:
    lines = [title, ""]
    for item in queue:
        if item.index in completed:
            marker = "V"
        elif current_index == item.index:
            marker = "in progress"
        else:
            marker = "queued"
        lines.append(f"{item.agent_name} #{item.index} ... {marker}")

    lines.extend(["", f"phase: {phase}"])
    if remaining_seconds is not None:
        lines.append(f"sleep remaining: {format_duration(remaining_seconds)}")
    lines.append(f"[S] stop after current: {'armed' if stop_after_current else 'off'}")
    lines.append("[F] finish current sleep")
    lines.append("[Q] kill now")
    return "\n".join(lines)


def format_duration(seconds: int) -> str:
    minutes, sec = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def tmux(*args: str, check: bool = True, capture: bool = False, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=check,
    )


def tmux_target_exists(target: str) -> bool:
    return tmux("display-message", "-p", "-t", target, "#{pane_id}", check=False, capture=True).returncode == 0


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def render_prompt(template: str, config: Config, item: RunItem) -> str:
    return template.format(
        agent_name=item.agent_name,
        run_index=item.index,
        cycle=item.cycle,
        project_dir=str(config.project_dir),
        runtime_dir=str(config.runtime_dir),
        audit_dir=str(config.runtime_dir),
        review_dir=str(config.runtime_dir),
        advisor_dir=str(config.runtime_dir),
        final_review_path=str(config.runtime_dir / "final-review.html"),
    )


def required_artifact_paths(config: Config, item: RunItem) -> tuple[Path, ...]:
    if item.prompt_kind == "reviewer":
        return (config.runtime_dir / "final-review.html",)
    if item.prompt_kind != "advisor":
        return ()
    paths: list[Path] = []
    for template in config.required_artifacts:
        rendered = render_prompt(template, config, item)
        path = Path(rendered).expanduser()
        paths.append(path if path.is_absolute() else config.runtime_dir / path)
    return tuple(paths)


def completion_file_path(config: Config, item: RunItem) -> Path | None:
    if not item.completion_file:
        return None
    rendered = render_prompt(item.completion_file, config, item)
    path = Path(rendered).expanduser()
    return path if path.is_absolute() else config.runtime_dir / path


def missing_required_artifacts(config: Config, item: RunItem) -> tuple[Path, ...]:
    missing: list[Path] = []
    marker = config.required_artifact_marker.strip()
    minimum_bytes = (
        config.reviewer_artifact_min_bytes
        if item.prompt_kind == "reviewer"
        else config.required_artifact_min_bytes
    )
    for path in required_artifact_paths(config, item):
        if not path.is_file() or path.stat().st_size < minimum_bytes:
            missing.append(path)
            continue
        content = path.read_text(errors="replace")
        folded = content.casefold()
        if marker and not content.rstrip().endswith(marker):
            missing.append(path)
            continue
        if any(incomplete.casefold() in folded for incomplete in config.incomplete_artifact_markers):
            missing.append(path)
            continue
        if item.prompt_kind == "reviewer" and ("<html" not in folded or "</html>" not in folded):
            missing.append(path)
    return tuple(missing)


def build_incomplete_continuation_prompt(paths: tuple[Path, ...]) -> str:
    missing = "\n".join(f"- {path}" for path in paths)
    return (
        "Advisor validation rejected the current output as incomplete. Continue the original assignment in this "
        "same Pi session now; do not stop at an outline, working draft, placeholder, or intake-only report.\n\n"
        f"Required artifacts still missing or incomplete:\n{missing}\n\n"
        "Return to repository analysis, gather concrete code evidence, replace every placeholder with substantive "
        "content, and fully complete each required artifact. If no vulnerability or recommendation is supported, "
        "document the surfaces examined and the evidence for that conclusion. Do not merely describe what remains "
        "to do. Finish the files themselves before becoming idle again."
    )


def build_prompt_argument_command(command: str, prompt_file: Path, completion_marker: str = "") -> str:
    prompt_command = f"{command} \"$(cat {sh_quote(str(prompt_file))})\""
    if completion_marker:
        return f"{prompt_command}; printf '\\n%s\\n' {sh_quote(completion_marker)}"
    return prompt_command


def build_prompt_at_file_command(command: str, prompt_file: Path, completion_marker: str = "") -> str:
    prompt_command = f"{command} {sh_quote(f'@{prompt_file}')}"
    if completion_marker:
        return f"{prompt_command}; printf '\\n%s\\n' {sh_quote(completion_marker)}"
    return prompt_command


def last_non_empty_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def ready_marker_match(line: str, markers: tuple[str, ...]) -> str:
    return next((marker for marker in markers if marker and marker in line), "")


def should_check_ready_for_item(item: RunItem, command_names: tuple[str, ...]) -> bool:
    return bool(item.command_name and item.command_name in command_names)


class Controller:
    def __init__(self, config: Config, session: str, worker_pane: str) -> None:
        self.config = config
        self.session = session
        self.worker_pane = worker_pane
        self.queue = build_run_queue(config)
        self.completed: set[int] = set()
        self.current_index: int | None = None
        self.phase = "starting"
        self.remaining_seconds: int | None = None
        self.last_ready_check_line: str | None = None
        self.last_ready_check_at: str | None = None
        self.ready_detected = False
        self.missing_required_artifacts: list[str] = []
        self.incomplete_continuations = 0
        self.capture_dir = config.runtime_dir / "captures"

    def run(self) -> None:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.config.runtime_dir / "controller.json", {"pid": os.getpid(), "session": self.session})
        self.render()

        for item in self.queue:
            if should_stop_after_current(self.config.runtime_dir):
                self.phase = "stopped before next run"
                self.current_index = None
                self.render()
                return
            self.run_one(item)

        self.current_index = None
        self.phase = "complete"
        self.remaining_seconds = None
        self.render()

    def run_one(self, item: RunItem) -> None:
        self.current_index = item.index
        self.phase = "launching"
        self.remaining_seconds = None
        self.ready_detected = False
        self.last_ready_check_line = None
        self.last_ready_check_at = None
        self.missing_required_artifacts = []
        self.incomplete_continuations = 0
        ready_file = completion_file_path(self.config, item)
        if ready_file is not None:
            ready_file.unlink(missing_ok=True)
        self.render()

        self.recycle_worker()
        self.cd_worker()
        prompt_template = self.config.reviewer_prompt if item.prompt_kind == "reviewer" else self.config.prompt
        prompt = render_prompt(prompt_template, self.config, item)
        command = item.command
        if ready_file is not None:
            command = f"export ADVISOR_READY_FILE={sh_quote(str(ready_file))}; {command}"
        if item.prompt_delivery in {"argument_file", "at_file"}:
            prompt_file = self.write_prompt_file(prompt)
            if item.prompt_delivery == "at_file":
                prompt_command = build_prompt_at_file_command(command, prompt_file, item.completion_marker)
            else:
                prompt_command = build_prompt_argument_command(command, prompt_file, item.completion_marker)
            self.send_shell_command(prompt_command)
        else:
            self.send_shell_command(command)
        self.sleep_with_controls(self.config.startup_wait_seconds, "startup wait")

        for key in item.startup_keys:
            self.send_key(key)
            self.sleep_with_controls(1, f"startup key {key}")
        if item.startup_keys:
            self.sleep_with_controls(self.config.post_startup_wait_seconds, "post startup wait")

        if item.prompt_delivery == "paste":
            self.phase = "sending prompt"
            self.render()
            self.paste_prompt(prompt)
            for key in item.submit_keys:
                self.send_key(key)
        elif item.prompt_delivery not in {"argument_file", "at_file"}:
            raise ValueError(f"unsupported prompt_delivery for {item.agent_name}: {item.prompt_delivery}")

        run_seconds = self.config.reviewer_run_seconds if item.prompt_kind == "reviewer" else self.config.run_seconds
        ready_item = item if should_check_ready_for_item(item, self.config.ready_command_names) else None
        wait_result = self.sleep_with_controls(run_seconds, "agent working", ready_item=ready_item)
        self.capture_run(item)
        if wait_result == "error":
            self.terminate_worker()
            self.phase = "failed: agent/provider error"
            self.remaining_seconds = None
            write_json(
                self.config.runtime_dir / "run-failure.json",
                {
                    "run_index": item.index,
                    "agent": item.agent_name,
                    "reason": "agent or provider error",
                },
            )
            self.render()
            raise RuntimeError(f"run {item.index} stopped after an agent or provider error")
        if wait_result == "elapsed":
            self.terminate_worker()
        missing = missing_required_artifacts(self.config, item)
        if missing:
            self.terminate_worker()
            self.missing_required_artifacts = [str(path) for path in missing]
            self.phase = "failed: missing required artifacts"
            self.remaining_seconds = None
            write_json(
                self.config.runtime_dir / "run-failure.json",
                {
                    "run_index": item.index,
                    "agent": item.agent_name,
                    "reason": "missing, empty, or incomplete required artifacts",
                    "missing_required_artifacts": self.missing_required_artifacts,
                },
            )
            self.render()
            names = ", ".join(path.name for path in missing)
            raise RuntimeError(f"run {item.index} exited without required artifacts: {names}")
        self.completed.add(item.index)
        self.current_index = None
        self.phase = "captured"
        self.remaining_seconds = None
        self.render()

    def recycle_worker(self) -> None:
        if tmux_target_exists(self.worker_pane):
            tmux("respawn-pane", "-k", "-t", self.worker_pane, "-c", str(self.config.project_dir))
            self.sleep_with_controls(1, "fresh shell")

    def terminate_worker(self) -> None:
        if tmux_target_exists(self.worker_pane):
            tmux("respawn-pane", "-k", "-t", self.worker_pane, "-c", str(self.config.project_dir))

    def cd_worker(self) -> None:
        self.phase = "cd project"
        self.render()
        self.send_shell_command(build_worker_cd_command(self.config.project_dir))
        self.sleep_with_controls(1, "cd project")

    def send_shell_command(self, command: str) -> None:
        tmux("send-keys", "-t", self.worker_pane, command, "Enter")

    def send_key(self, key: str) -> None:
        tmux("send-keys", "-t", self.worker_pane, key)

    def write_prompt_file(self, prompt: str) -> Path:
        prompt_file = self.config.runtime_dir / "last-prompt.txt"
        prompt_file.write_text(prompt)
        return prompt_file

    def paste_prompt(self, prompt: str, filename: str = "last-prompt.txt") -> None:
        prompt_file = self.config.runtime_dir / filename
        prompt_file.write_text(prompt)
        buffer_name = f"advisor-{os.getpid()}"
        tmux("load-buffer", "-b", buffer_name, str(prompt_file))
        # Preserve the follow-up as one multiline editor paste. Without these
        # flags tmux turns each newline into Enter and Pi submits many messages.
        tmux("paste-buffer", "-d", "-p", "-r", "-b", buffer_name, "-t", self.worker_pane)

    def continue_incomplete_run(self, item: RunItem, missing: tuple[Path, ...]) -> None:
        ready_file = completion_file_path(self.config, item)
        if ready_file is not None:
            ready_file.unlink(missing_ok=True)
        self.incomplete_continuations += 1
        self.ready_detected = False
        self.missing_required_artifacts = [str(path) for path in missing]
        self.phase = (
            f"continuing incomplete output "
            f"({self.incomplete_continuations}/{self.config.max_incomplete_continuations})"
        )
        write_json(
            self.config.runtime_dir / "last-incomplete-continuation.json",
            {
                "run_index": item.index,
                "agent": item.agent_name,
                "attempt": self.incomplete_continuations,
                "missing_or_incomplete": self.missing_required_artifacts,
                "continued_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        self.render()
        continuation = build_incomplete_continuation_prompt(missing)
        self.paste_prompt(continuation, f"continuation-prompt-{item.index}.txt")
        self.send_key("Enter")

    def capture_run(self, item: RunItem) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_agent = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in item.agent_name)
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
        path = self.capture_dir / f"{item.index:03d}-{safe_agent}-{stamp}.txt"
        path.write_text(output)
        write_json(
            self.config.runtime_dir / "last-capture.json",
            {"run_index": item.index, "agent": item.agent_name, "path": str(path)},
        )

    def capture_worker_tail(self, lines: int) -> str:
        return tmux(
            "capture-pane",
            "-p",
            "-J",
            "-S",
            f"-{max(1, lines)}",
            "-t",
            self.worker_pane,
            capture=True,
        ).stdout

    def check_ready_marker(self, item: RunItem) -> bool:
        ready_file = completion_file_path(self.config, item)
        if ready_file is not None and ready_file.is_file():
            last_line = last_non_empty_line(ready_file.read_text(errors="replace"))
        else:
            captured = self.capture_worker_tail(self.config.ready_check_lines)
            last_line = last_non_empty_line(captured)
        matched_marker = ready_marker_match(last_line, self.config.ready_markers)
        checked_at = datetime.now().isoformat(timespec="seconds")
        self.last_ready_check_line = last_line
        self.last_ready_check_at = checked_at
        self.ready_detected = bool(matched_marker)
        write_json(
            self.config.runtime_dir / "last-ready-check.json",
            {
                "checked_at": checked_at,
                "run_index": item.index,
                "agent": item.agent_name,
                "last_line": last_line,
                "matched": bool(matched_marker),
                "matched_marker": matched_marker,
            },
        )
        return bool(matched_marker)

    def check_error_marker(self, item: RunItem) -> bool:
        ready_file = completion_file_path(self.config, item)
        if ready_file is None or not ready_file.is_file():
            return False
        last_line = last_non_empty_line(ready_file.read_text(errors="replace"))
        if last_line != "Error":
            return False
        self.last_ready_check_line = last_line
        self.last_ready_check_at = datetime.now().isoformat(timespec="seconds")
        self.ready_detected = False
        self.render()
        return True

    def sleep_with_controls(self, seconds: int, phase: str, ready_item: RunItem | None = None) -> str:
        if ready_item is not None:
            return self.wait_for_ready_or_timeout(seconds, phase, ready_item)
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

    def wait_for_ready_or_timeout(self, seconds: int, phase: str, ready_item: RunItem) -> str:
        deadline = time.time() + max(0, seconds)
        next_ready_check = time.time() + max(1, self.config.ready_check_seconds)
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
            if time.time() >= next_ready_check:
                if self.check_error_marker(ready_item):
                    self.remaining_seconds = None
                    self.phase = f"{phase} agent/provider error"
                    self.render()
                    return "error"
                if self.check_ready_marker(ready_item):
                    missing = missing_required_artifacts(self.config, ready_item)
                    if missing and completion_file_path(self.config, ready_item) is not None:
                        if self.incomplete_continuations < self.config.max_incomplete_continuations:
                            self.continue_incomplete_run(ready_item, missing)
                            # A continuation is a real follow-up turn, so give it the same
                            # work window as the original prompt instead of inheriting a
                            # nearly-expired deadline.
                            deadline = time.time() + max(0, seconds)
                            next_ready_check = time.time() + max(1, self.config.ready_check_seconds)
                            continue
                        self.missing_required_artifacts = [str(path) for path in missing]
                        self.remaining_seconds = None
                        self.phase = f"{phase} incomplete after continuations"
                        self.render()
                        return "incomplete"
                    self.missing_required_artifacts = []
                    self.remaining_seconds = None
                    self.phase = f"{phase} ready"
                    self.render()
                    return "ready"
                next_ready_check = time.time() + max(1, self.config.ready_check_seconds)
            time.sleep(1)

    def handle_keyboard(self) -> None:
        while select.select([sys.stdin], [], [], 0)[0]:
            key = sys.stdin.read(1)
            if key in {"q", "Q"}:
                tmux("kill-session", "-t", self.session, check=False)
                raise SystemExit(0)
            if key in {"s", "S"}:
                (self.config.runtime_dir / STOP_NEXT_FLAG).write_text("1\n")
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
                "topic": self.config.topic,
                "topic_label": self.config.topic_label,
                "phase": self.phase,
                "current_index": self.current_index,
                "completed": sorted(self.completed),
                "stop_after_current": should_stop_after_current(self.config.runtime_dir),
                "finish_current_sleep": should_finish_current_sleep(self.config.runtime_dir),
                "remaining_seconds": self.remaining_seconds,
                "ready_detected": self.ready_detected,
                "last_ready_check_line": self.last_ready_check_line,
                "last_ready_check_at": self.last_ready_check_at,
                "missing_required_artifacts": self.missing_required_artifacts,
                "incomplete_continuations": self.incomplete_continuations,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        print("\033[2J\033[H", end="")
        print(
            render_status(
                self.queue,
                self.current_index,
                self.completed,
                should_stop_after_current(self.config.runtime_dir),
                self.phase,
                self.remaining_seconds,
                self.config.session_name,
            ),
            flush=True,
        )


def run_controller(args: argparse.Namespace) -> None:
    project_dir_override = Path(args.project_dir) if args.project_dir else None
    runtime_dir_override = Path(args.runtime_dir) if args.runtime_dir else None
    config = load_config(
        Path(args.config),
        project_dir_override=project_dir_override,
        runtime_dir_override=runtime_dir_override,
    )
    old_attrs = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        controller = Controller(config, args.session, args.worker_pane)
        controller.run()
        write_json(
            config.runtime_dir / "controller-idle.json",
            {
                "pid": os.getpid(),
                "session": args.session,
                "phase": controller.phase,
                "entered_idle_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        while True:
            select.select([sys.stdin], [], [], 1)
            controller.handle_keyboard()
    except BaseException as exc:
        write_json(
            config.runtime_dir / "controller-exit.json",
            {
                "pid": os.getpid(),
                "session": args.session,
                "exited_at": datetime.now().isoformat(timespec="seconds"),
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "traceback": "".join(traceback.format_exception(exc)),
            },
        )
        raise
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)


def fzf_available() -> bool:
    return shutil.which("fzf") is not None and sys.stdin.isatty()


def fzf_select(prompt: str, options: list[tuple[str, str]], multi: bool = False) -> list[str]:
    if not fzf_available():
        return fallback_select(prompt, options, multi)
    labels = [label for label, _ in options]
    command = ["fzf", "--height=80%", "--border", "--prompt", prompt]
    if multi:
        command.append("-m")
    result = subprocess.run(command, input="\n".join(labels), text=True, stdout=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise SystemExit("advisor: selection cancelled")
    selected_labels = [line for line in result.stdout.splitlines() if line]
    label_to_value = {label: value for label, value in options}
    return [label_to_value[label] for label in selected_labels]


def fallback_select(prompt: str, options: list[tuple[str, str]], multi: bool = False) -> list[str]:
    print(prompt)
    for index, (label, _) in enumerate(options, start=1):
        print(f"{index}. {label}")
    raw = input("Choose number(s): ").strip()
    if not raw:
        raise SystemExit("advisor: selection cancelled")
    indexes = [int(part) for part in raw.replace(",", " ").split()]
    if not multi and len(indexes) > 1:
        indexes = indexes[:1]
    return [options[index - 1][1] for index in indexes]


def prompt_int(label: str, default: int) -> int:
    raw = input(f"{label} [{default}]: ").strip()
    if not raw:
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{label} must be at least 1")
    return value


def read_multiline_task() -> str:
    print("Describe the custom advisor task. Finish with an empty line.")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line and lines:
            break
        lines.append(line)
    task = "\n".join(lines).strip()
    if not task:
        raise SystemExit("advisor: custom task description is required")
    return task


def build_review_screen(config: Config) -> str:
    queue = build_run_queue(config)
    first_agent = config.agents[0]
    prompt_preview = render_prompt(
        config.prompt,
        config,
        RunItem(
            index=1,
            cycle=1,
            agent_name=first_agent.name,
            command=first_agent.command,
            command_name=first_agent.command_name,
            model_label=first_agent.model_label,
            model=first_agent.model,
            completion_marker=first_agent.completion_marker,
            startup_keys=first_agent.startup_keys,
            submit_keys=first_agent.submit_keys,
            prompt_delivery=first_agent.prompt_delivery,
        ),
    )
    lines = [
        f"Topic: {config.topic} - {config.topic_label}",
        f"Project: {config.project_dir}",
        f"Output dir: {config.runtime_dir}",
        f"Cycles: {config.num_runs}",
        f"Agents per cycle: {len(config.agents)}",
        f"Total agent runs: {len(queue)}",
        f"Seconds per agent run: {config.run_seconds}",
        f"Startup wait seconds: {config.startup_wait_seconds}",
        "",
        "models:",
    ]
    for item in queue:
        prefix = "reviewer: " if item.prompt_kind == "reviewer" else ""
        lines.append(f"{prefix}{item.command}")
    lines.extend(["", "Prompt preview for run 1:", "", prompt_preview, ""])
    return "\n".join(lines)


def confirm_config(config: Config) -> None:
    print("\033[2J\033[H", end="")
    print(build_review_screen(config))
    answer = input("Start this advisor run? [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        raise SystemExit("advisor: run cancelled")


def topic_raw_for(raw: dict[str, Any], topic: str) -> dict[str, Any]:
    return dict(raw.get("custom_topic", {}) if topic == "custom" else raw.get("topics", {}).get(topic, {}))


def select_models(raw: dict[str, Any], topic_raw: dict[str, Any]) -> list[Agent]:
    available_commands = topic_available_commands(raw, topic_raw)
    command_names = [str(name) for name in topic_raw.get("command_names", topic_raw.get("agent_names", []))]
    if command_names:
        commands = [available_commands[name] for name in command_names if name in available_commands]
    else:
        commands = list(available_commands.values())
    default_models = {
        str(command_name): {str(model) for model in models}
        for command_name, models in topic_raw.get("default_models", {}).items()
    }
    options: list[tuple[str, str]] = []
    model_by_key: dict[str, tuple[CommandConfig, ModelOption]] = {}
    for command in commands:
        models = command.models or (ModelOption(label="default"),)
        for index, model in enumerate(models, start=1):
            key = f"{command.name}:{index}"
            model_by_key[key] = (command, model)
            default_marker = ""
            wanted = default_models.get(command.name, set())
            if model.model in wanted or model.label in wanted:
                default_marker = "[default] "
            label = model.label if not model.model else f"{model.label} ({model.model})"
            options.append((f"{default_marker}{label} | {build_model_command(command, model)}", key))
    selected_keys = fzf_select("Models (tab for multi)> ", options, multi=True)
    return [agent_from_model(*model_by_key[key]) for key in selected_keys]


def reviewer_agent_from_model(command: CommandConfig, model: ModelOption) -> Agent:
    base = agent_from_model(command, model)
    model_name = model.model or model.label
    return Agent(
        name=f"reviewer-{sanitize_name(command.name)}-{sanitize_name(model_name)}",
        command=base.command,
        command_name=base.command_name,
        model_label=base.model_label,
        model=base.model,
        completion_marker=base.completion_marker,
        completion_file=base.completion_file,
        startup_keys=base.startup_keys,
        submit_keys=base.submit_keys,
        prompt_delivery=base.prompt_delivery,
    )


def reviewer_agent_for_cli(
    raw: dict[str, Any], topic_raw: dict[str, Any], command_name: str, model_name: str
) -> Agent:
    commands = topic_available_commands(raw, topic_raw)
    if command_name not in commands:
        raise ValueError(f"reviewer command is unavailable for this topic: {command_name}")
    command = commands[command_name]
    models = command.models or (ModelOption(label="default"),)
    if model_name:
        matches = [model for model in models if model.model == model_name or model.label == model_name]
        if not matches:
            raise ValueError(f"unknown reviewer model for {command_name}: {model_name}")
        model = matches[0]
    elif len(models) == 1:
        model = models[0]
    else:
        model = next((candidate for candidate in models if not candidate.model), models[0])
    return reviewer_agent_from_model(command, model)


def select_reviewer_model(raw: dict[str, Any], topic_raw: dict[str, Any]) -> Agent | None:
    available_commands = topic_available_commands(raw, topic_raw)
    command_names = [str(name) for name in topic_raw.get("command_names", topic_raw.get("agent_names", []))]
    if command_names:
        commands = [available_commands[name] for name in command_names if name in available_commands]
    else:
        commands = list(available_commands.values())

    options: list[tuple[str, str]] = [("none", "none")]
    model_by_key: dict[str, tuple[CommandConfig, ModelOption]] = {}
    for command in commands:
        models = command.models or (ModelOption(label="default"),)
        for index, model in enumerate(models, start=1):
            key = f"{command.name}:{index}"
            model_by_key[key] = (command, model)
            label = model.label if not model.model else f"{model.label} ({model.model})"
            options.append((f"{label} | {build_model_command(command, model)}", key))

    selected = fzf_select("Reviewer model> ", options, multi=False)[0]
    if selected == "none":
        return None
    return reviewer_agent_from_model(*model_by_key[selected])


def run_wizard(raw: dict[str, Any], project_dir: Path) -> Config:
    topic_options = []
    for slug, topic in raw.get("topics", {}).items():
        topic_options.append((f"{slug} - {topic.get('label', slug)}", str(slug)))
    custom = raw.get("custom_topic", {})
    topic_options.append((f"custom - {custom.get('label', 'Custom advisor task')}", "custom"))
    topic = fzf_select("Topic> ", topic_options, multi=False)[0]

    custom_task = read_multiline_task() if topic == "custom" else ""
    preview = resolve_config(raw, project_dir, topic_override=topic, custom_task_description=custom_task)

    topic_raw = topic_raw_for(raw, topic)
    selected_agents = select_models(raw, topic_raw)
    reviewer_agent = select_reviewer_model(raw, topic_raw)

    num_runs = prompt_int("Number of cycles", preview.num_runs)
    run_seconds = prompt_int("Seconds per agent run", preview.run_seconds)
    runtime_dir = make_runtime_dir(project_dir, preview.work_dir_name)
    config = resolve_config(
        raw,
        project_dir,
        topic_override=topic,
        runtime_dir_override=runtime_dir,
        selected_agents=tuple(selected_agents),
        reviewer_agent=reviewer_agent,
        num_runs_override=num_runs,
        run_seconds_override=run_seconds,
        custom_task_description=custom_task,
    )
    confirm_config(config)
    return config


def sanitize_session_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-")
    return cleaned or "project"


def start_session(args: argparse.Namespace) -> None:
    if shutil.which("tmux") is None:
        raise SystemExit("advisor: tmux is required")

    config_path = Path(args.config).expanduser().resolve()
    raw = json.loads(config_path.read_text())
    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else expand_project_path(str(raw.get("project_dir", ".")))
    if not project_dir.is_dir():
        raise SystemExit(f"project_dir does not exist: {project_dir}")

    if args.no_tui:
        topic_override = args.topic or None
        initial = resolve_config(raw, project_dir, topic_override=topic_override)
        topic_raw = topic_raw_for(raw, initial.topic)
        reviewer_agent = (
            reviewer_agent_for_cli(raw, topic_raw, args.reviewer, args.reviewer_model)
            if args.reviewer
            else None
        )
        config = resolve_config(
            raw,
            project_dir,
            topic_override=topic_override,
            runtime_dir_override=make_runtime_dir(project_dir, initial.work_dir_name),
            reviewer_agent=reviewer_agent,
            num_runs_override=args.cycles,
            run_seconds_override=args.seconds,
        )
    else:
        config = run_wizard(raw, project_dir)

    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    (config.runtime_dir / STOP_NEXT_FLAG).unlink(missing_ok=True)
    (config.runtime_dir / FINISH_SLEEP_FLAG).unlink(missing_ok=True)
    resolved_config_path = config.runtime_dir / RESOLVED_CONFIG_NAME
    write_json(resolved_config_path, config_to_resolved_json(config))

    session = args.session or f"{config.session_name}-{sanitize_session_part(config.project_dir.name)}"
    if tmux("has-session", "-t", session, check=False, capture=True).returncode == 0:
        attach_session(session)
        return

    term_w = str(shutil.get_terminal_size((200, 50)).columns)
    term_h = str(shutil.get_terminal_size((200, 50)).lines)
    script = Path(__file__).resolve()

    controller_id = tmux(
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        "advisor",
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
    worker_id = tmux(
        "split-window",
        "-t",
        controller_id,
        "-v",
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
    tmux("set-option", "-t", session, "status-left", f"#[fg=cyan,bold] {config.session_name} {config.project_dir.name} ")
    tmux("set-option", "-t", session, "status-right", "")
    tmux("set-window-option", "-t", session, "remain-on-exit", "on")

    controller_cmd = (
        f"python3 {sh_quote(str(script))} "
        f"--config {sh_quote(str(resolved_config_path))} "
        f"__controller "
        f"--session {sh_quote(session)} "
        f"--worker-pane {sh_quote(worker_id)} "
        f"--runtime-dir {sh_quote(str(config.runtime_dir))} "
        f"--project-dir {sh_quote(str(config.project_dir))}"
    )
    tmux("send-keys", "-t", controller_id, controller_cmd, "Enter")
    tmux("select-pane", "-t", controller_id)

    write_json(
        config.runtime_dir / "session.json",
        {
            "session": session,
            "worker_pane": worker_id,
            "project_dir": str(config.project_dir),
            "runtime_dir": str(config.runtime_dir),
            "topic": config.topic,
            "topic_label": config.topic_label,
            "started_at": datetime.now().strftime(TIMESTAMP_FORMAT),
        },
    )
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


def load_project_and_work_dirs(args: argparse.Namespace) -> tuple[Path, list[str]]:
    raw = json.loads(Path(args.config).expanduser().resolve().read_text())
    project_dir = Path(args.project_dir).expanduser().resolve() if getattr(args, "project_dir", "") else expand_project_path(str(raw.get("project_dir", ".")))
    return project_dir, configured_work_dir_names(raw)


def stop_next(args: argparse.Namespace) -> None:
    project_dir, work_dirs = load_project_and_work_dirs(args)
    runtime_dir = latest_runtime_dir(project_dir, work_dirs)
    (runtime_dir / STOP_NEXT_FLAG).write_text("1\n")
    print(f"armed stop-after-current: {runtime_dir / STOP_NEXT_FLAG}")


def finish_sleep(args: argparse.Namespace) -> None:
    project_dir, work_dirs = load_project_and_work_dirs(args)
    runtime_dir = latest_runtime_dir(project_dir, work_dirs)
    (runtime_dir / FINISH_SLEEP_FLAG).write_text("1\n")
    print(f"armed finish-current-sleep: {runtime_dir / FINISH_SLEEP_FLAG}")


def kill(args: argparse.Namespace) -> None:
    project_dir, work_dirs = load_project_and_work_dirs(args)
    runtime_dir = latest_runtime_dir(project_dir, work_dirs)
    session = args.session or read_session(runtime_dir)
    tmux("kill-session", "-t", session, check=False)
    print(f"killed tmux session: {session}")


def status(args: argparse.Namespace) -> None:
    project_dir, work_dirs = load_project_and_work_dirs(args)
    runtime_dir = latest_runtime_dir(project_dir, work_dirs)
    state_file = runtime_dir / "state.json"
    if not state_file.exists():
        raise SystemExit(f"no state file at {state_file}")
    print(state_file.read_text(), end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="advisor")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start or attach to an advisor tmux session")
    run.add_argument("project_dir", nargs="?", default="", help="repo path to review; overrides config.json project_dir")
    run.add_argument("--session", default="", help="override tmux session name")
    run.add_argument("--topic", choices=["sec", "attack", "arch", "hygiene", "custom"], default="", help="topic for --no-tui runs")
    run.add_argument("--no-tui", action="store_true", help="skip fzf wizard and use config defaults")
    run.add_argument("--cycles", type=int, default=None, help="override cycle count for --no-tui runs")
    run.add_argument("--seconds", type=int, default=None, help="override seconds per agent for --no-tui runs")
    run.add_argument("--reviewer", default="", help="reviewer command name for --no-tui runs, for example pi")
    run.add_argument("--reviewer-model", default="", help="reviewer model ID for --no-tui runs")
    run.add_argument("--no-attach", action="store_true", help="create session without attaching")
    run.set_defaults(func=start_session)

    controller = sub.add_parser("__controller")
    controller.add_argument("--session", required=True)
    controller.add_argument("--worker-pane", required=True)
    controller.add_argument("--project-dir", default="")
    controller.add_argument("--runtime-dir", default="")
    controller.set_defaults(func=run_controller)

    attach = sub.add_parser("attach", help="attach to the last advisor session")
    attach.add_argument("project_dir", nargs="?", default="", help="repo path; overrides config.json project_dir")
    attach.set_defaults(func=lambda args: attach_session(read_session(latest_runtime_dir(*load_project_and_work_dirs(args)))))

    stop = sub.add_parser("stop-next", help="finish current run, then stop")
    stop.add_argument("project_dir", nargs="?", default="", help="repo path; overrides config.json project_dir")
    stop.set_defaults(func=stop_next)

    finish = sub.add_parser("finish-sleep", help="finish the current controller sleep immediately")
    finish.add_argument("project_dir", nargs="?", default="", help="repo path; overrides config.json project_dir")
    finish.set_defaults(func=finish_sleep)

    kill_cmd = sub.add_parser("kill", help="kill the tmux session now")
    kill_cmd.add_argument("project_dir", nargs="?", default="", help="repo path; overrides config.json project_dir")
    kill_cmd.add_argument("--session", default="", help="override tmux session name")
    kill_cmd.set_defaults(func=kill)

    stat = sub.add_parser("status", help="print JSON controller state")
    stat.add_argument("project_dir", nargs="?", default="", help="repo path; overrides config.json project_dir")
    stat.set_defaults(func=status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
