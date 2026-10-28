from __future__ import annotations

import argparse
from pathlib import Path

from .launcher import launch
from .sessions import find_repo_sessions
from .tui import run_tui


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-delay",
        description="Schedule a prompt for a fresh or existing Codex session.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect the schedule, then print the commands without waiting or starting Codex",
    )
    args = parser.parse_args(argv)

    workdir = Path.cwd().resolve()
    sessions = find_repo_sessions(workdir)
    request = run_tui(sessions)
    if request is None:
        print("Cancelled.")
        return 130

    target = request.session_name
    print(f"Scheduled for {request.duration} from now → {target}")
    return launch(request, workdir, dry_run=args.dry_run)
