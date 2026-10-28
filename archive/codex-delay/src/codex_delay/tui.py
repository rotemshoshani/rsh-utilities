from __future__ import annotations

import curses
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

from .duration import format_duration, parse_duration
from .launcher import ScheduleRequest
from .sessions import CodexSession


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class _Choice:
    session: CodexSession | None


@dataclass
class _Dashboard:
    choices: list[_Choice]
    prompt: str = ""
    duration: str = ""
    selected: int = 0
    focus: int = 0
    status: str = "Prompt saved. Tab between sections; press s when ready."


PROMPT_FOCUS = 0
DELAY_FOCUS = 1
SESSION_FOCUS = 2


def run_tui(sessions: list[CodexSession]) -> ScheduleRequest | None:
    with tempfile.TemporaryDirectory(prefix="codex-delay-") as temporary:
        prompt_path = Path(temporary) / "prompt.md"
        prompt_path.touch(mode=0o600)
        try:
            return curses.wrapper(_run, sessions, prompt_path)
        except (Cancelled, KeyboardInterrupt):
            return None


def _run(
    stdscr: curses.window,
    sessions: list[CodexSession],
    prompt_path: Path,
) -> ScheduleRequest:
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)

    state = _Dashboard(choices=[_Choice(None), *(_Choice(session) for session in sessions)])
    state.prompt = _edit_prompt_in_micro(stdscr, prompt_path)
    if not state.prompt:
        state.status = "Prompt is empty. Press Enter on Prompt to reopen micro."

    while True:
        _draw_dashboard(stdscr, state)
        key = stdscr.get_wch()

        if key in ("q", "Q", "\x1b"):
            raise Cancelled
        if key == "\t":
            state.focus = (state.focus + 1) % 3
            continue
        if key == curses.KEY_BTAB:
            state.focus = (state.focus - 1) % 3
            continue
        if key in ("p", "P"):
            state.focus = PROMPT_FOCUS
            continue
        if key in ("d", "D"):
            state.focus = DELAY_FOCUS
            continue
        if key in ("t", "T"):
            state.focus = SESSION_FOCUS
            continue
        if key in ("s", "S", curses.KEY_F5):
            request = _build_request(state)
            if request is not None:
                return request
            continue

        if state.focus == PROMPT_FOCUS:
            if key in ("\n", "\r", curses.KEY_ENTER, "e", "E"):
                state.prompt = _edit_prompt_in_micro(stdscr, prompt_path)
                state.status = "Prompt saved." if state.prompt else "Prompt is empty. Reopen micro to add it."
        elif state.focus == DELAY_FOCUS:
            _handle_delay_key(state, key)
        else:
            _handle_session_key(state, key)


def _edit_prompt_in_micro(stdscr: curses.window, prompt_path: Path) -> str:
    curses.def_prog_mode()
    curses.endwin()
    try:
        result = subprocess.run(["micro", str(prompt_path)], check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("micro is required but was not found in PATH") from exc
    finally:
        curses.reset_prog_mode()
        stdscr.clear()
        stdscr.refresh()
        curses.curs_set(0)

    if result.returncode != 0:
        return prompt_path.read_text(encoding="utf-8").strip()
    return prompt_path.read_text(encoding="utf-8").strip()


def _handle_delay_key(state: _Dashboard, key: object) -> None:
    if key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
        state.duration = state.duration[:-1]
        state.status = "Editing delay."
    elif key in ("\n", "\r", curses.KEY_ENTER):
        try:
            state.duration = format_duration(parse_duration(state.duration))
            state.focus = SESSION_FOCUS
            state.status = "Delay set."
        except ValueError as exc:
            state.status = f"Invalid delay: {exc}"
    elif isinstance(key, str) and (key.isdigit() or key == ":"):
        state.duration += key
        state.status = "Editing delay. Enter normalizes it; s schedules."


def _handle_session_key(state: _Dashboard, key: object) -> None:
    if key in (curses.KEY_UP, "k", "K"):
        state.selected = (state.selected - 1) % len(state.choices)
    elif key in (curses.KEY_DOWN, "j", "J"):
        state.selected = (state.selected + 1) % len(state.choices)
    elif key == curses.KEY_HOME:
        state.selected = 0
    elif key == curses.KEY_END:
        state.selected = len(state.choices) - 1
    elif key in ("\n", "\r", curses.KEY_ENTER):
        state.focus = PROMPT_FOCUS
        state.status = "Session selected."


def _build_request(state: _Dashboard) -> ScheduleRequest | None:
    if not state.prompt.strip():
        state.focus = PROMPT_FOCUS
        state.status = "A prompt is required. Press Enter to write it in micro."
        return None
    try:
        duration = format_duration(parse_duration(state.duration))
    except ValueError as exc:
        state.focus = DELAY_FOCUS
        state.status = f"Invalid delay: {exc}"
        return None

    choice = state.choices[state.selected]
    session_name = choice.session.name if choice.session else "Fresh session"
    return ScheduleRequest(
        duration=duration,
        prompt=state.prompt.strip(),
        session_id=choice.session.id if choice.session else None,
        session_name=session_name,
    )


def _draw_dashboard(stdscr: curses.window, state: _Dashboard) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    _put(stdscr, 1, 3, "CODEX DELAY", curses.color_pair(1) | curses.A_BOLD)
    _put(stdscr, 1, max(3, width - 18), " EDIT SCHEDULE ", curses.color_pair(3))

    _section(stdscr, 3, "1  PROMPT", state.focus == PROMPT_FOCUS)
    preview_width = max(10, width - 10)
    preview_lines = _prompt_preview(state.prompt, preview_width, 3)
    for offset, line in enumerate(preview_lines, start=4):
        _put(stdscr, offset, 5, line, curses.A_DIM if not state.prompt else curses.A_NORMAL)
    _put(stdscr, 7, 5, "Enter/e opens micro  ·  Ctrl+S save, Ctrl+Q quit micro", curses.A_DIM)

    _section(stdscr, 9, "2  DELAY", state.focus == DELAY_FOCUS)
    duration = state.duration or "not set"
    duration_attr = curses.color_pair(2) | curses.A_BOLD if state.focus == DELAY_FOCUS else curses.A_BOLD
    _put(stdscr, 10, 5, f"{duration:<12}  examples: 90 · 01:30 · 00:01:30", duration_attr)

    _section(stdscr, 12, "3  SESSION", state.focus == SESSION_FOCUS)
    _put(stdscr, 13, 5, "UPDATED           THREAD NAME          TITLE                     ID", curses.A_DIM)

    list_top = 14
    list_bottom = max(list_top + 1, height - 4)
    visible_count = max(1, list_bottom - list_top)
    start = min(
        max(0, state.selected - visible_count + 1),
        max(0, len(state.choices) - visible_count),
    )
    for row, choice in enumerate(state.choices[start : start + visible_count], start=list_top):
        index = start + row - list_top
        line = _choice_line(choice)
        if index == state.selected and state.focus == SESSION_FOCUS:
            attr = curses.color_pair(2) | curses.A_BOLD
        elif index == state.selected:
            attr = curses.color_pair(1) | curses.A_BOLD
        else:
            attr = curses.A_NORMAL
        _put(stdscr, row, 5, line, attr)

    chosen = state.choices[state.selected].session
    if chosen is None:
        detail = "Selected: Fresh session"
    else:
        detail = f"Selected: {chosen.thread_name or chosen.title or chosen.id}  ·  {chosen.id}"
    _put(stdscr, height - 4, 5, detail, curses.A_DIM)
    status_attr = curses.color_pair(4) if state.status.startswith(("Invalid", "A prompt")) else curses.A_DIM
    _put(stdscr, height - 2, 3, state.status, status_attr)
    _put(
        stdscr,
        height - 1,
        3,
        "Tab/Shift-Tab sections  p/d/t jump  ↑/↓ session  s/F5 schedule  q/Esc cancel",
        curses.A_DIM,
    )

    if state.focus == DELAY_FOCUS:
        curses.curs_set(1)
        stdscr.move(10, min(width - 2, 5 + len(duration)))
    else:
        curses.curs_set(0)
    stdscr.refresh()


def _prompt_preview(prompt: str, width: int, limit: int) -> list[str]:
    if not prompt:
        return ["(empty — press Enter to write in micro)"]
    lines: list[str] = []
    for source_line in prompt.splitlines() or [prompt]:
        lines.extend(textwrap.wrap(source_line, width=width) or [""])
        if len(lines) >= limit:
            break
    shown = lines[:limit]
    if len(lines) > limit or len(prompt.splitlines()) > len(shown):
        shown[-1] = f"{shown[-1][: max(0, width - 2)]} …"
    return shown


def _choice_line(choice: _Choice) -> str:
    if choice.session is None:
        return "NEW               Fresh session        —                         —"
    thread_name = " ".join(choice.session.thread_name.split()) or "—"
    title = " ".join(choice.session.title.split()) or "—"
    return (
        f"{choice.session.updated_display:<17} "
        f"{thread_name:<20.20} {title:<25.25} {choice.session.id[:8]}"
    )


def _section(stdscr: curses.window, y: int, title: str, focused: bool) -> None:
    marker = "▶" if focused else " "
    attr = curses.color_pair(1) | curses.A_BOLD if focused else curses.A_BOLD
    _put(stdscr, y, 3, f"{marker} {title}", attr)


def _put(stdscr: curses.window, y: int, x: int, value: str, attr: int = curses.A_NORMAL) -> None:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    try:
        stdscr.addnstr(y, x, value, max(0, width - x - 1), attr)
    except curses.error:
        pass
