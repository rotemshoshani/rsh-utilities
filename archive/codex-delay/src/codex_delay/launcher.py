from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScheduleRequest:
    duration: str
    prompt: str
    session_id: str | None
    session_name: str


def bundled_slp() -> Path:
    return Path(__file__).resolve().parents[3] / "slp" / "slp"


def codex_arguments(request: ScheduleRequest) -> list[str]:
    arguments = ["cdx"]
    if request.session_id is not None:
        arguments.extend(("resume", request.session_id))
    arguments.append(request.prompt)
    return arguments


def launch(request: ScheduleRequest, workdir: Path, *, dry_run: bool = False) -> int:
    slp_path = bundled_slp()
    if not slp_path.is_file():
        raise RuntimeError(f"bundled slp command not found: {slp_path}")

    command = shlex.join(codex_arguments(request))
    if dry_run:
        print(f"Would run: {shlex.join([str(slp_path), request.duration])}")
        print(f"Then run: bash -ic {shlex.quote(command)}")
        return 0

    delayed = subprocess.run([str(slp_path), request.duration], cwd=workdir, check=False)
    if delayed.returncode != 0:
        return delayed.returncode
    return subprocess.run(["bash", "-ic", command], cwd=workdir, check=False).returncode
