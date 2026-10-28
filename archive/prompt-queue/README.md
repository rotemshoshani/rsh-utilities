# prompt-queue

Autonomous tmux runner for feeding a queue of prompts to an agent one at a time.

It opens a tmux session with a controller pane and a worker pane. For each
prompt, the controller starts a fresh agent process, sends that prompt as the
initial CLI prompt, waits until the worker pane is `Ready`, captures the worker
pane, stops the agent by respawning the worker pane, and continues to the next
prompt.

When blocked recovery is enabled, it also opens a planner pane. If an executor
finishes with `DO-NOT-PROCEED`, the controller resumes the configured planner
Codex session in that pane, asks it to inspect the blocked run, and waits until
the planner pane is `Ready`. Only then does it inspect recent planner output for
an exact `PROCEED-ALLOWED` or `HUMAN-DECISION-REQUIRED` marker.

When completion notification is enabled, the controller also resumes the planner
session after every queued prompt has completed. It asks the planner to verify
the finished work, fix issues if needed, and waits until that planner pane is
`Ready` before marking the queue complete.

Run artifacts are isolated by queue tag under the target repo:

```bash
<project>/.planning/work/prompt-queue/<tag>/<YYYYMMDD-HHMMSS>/
```

## Usage

Each active queue is a self-contained folder whose name is its required tag:

```bash
queues/
  customer-portal-release/
    .env.local
    config.json
    prompts/
      001-schema.md
      002-ui.md
```

Run that specific queue with the tag:

```bash
./prompt-queue run customer-portal-release
```

If `pq` aliases this repository's `./prompt-queue run`, the equivalent is:

```bash
pq customer-portal-release
```

Different tags use different tmux session names and runtime directories, so
multiple queues can run at the same time, including against the same target
repository.

By default this runs Codex through `cdx`. To run the same queue through Claude
instead, using `cld` and interactive paste delivery:

```bash
./prompt-queue run customer-portal-release --cld
```

Starting a run shows a preflight summary before tmux launch. If a tmux session
for the same prompt queue already exists, prompt-queue asks whether to attach to
it, replace it with a new run, or quit. In non-interactive shells, the safe
default is to attach to the existing session instead of replacing it.

To start a new run without the interactive confirmation prompt:

```bash
./prompt-queue run customer-portal-release --no-confirm
```

With the local `pq` alias, pass the flag directly:

```bash
pq customer-portal-release --no-confirm
```

`-y` and `--yes` are accepted aliases for `--no-confirm`.

When a run starts, prompt-queue looks only at that tag's latest compatible run.
Completed prompts are skipped automatically, so if a tmux session exits or is
stopped after prompt 5, the next run of the same tag starts from prompt 6.
Compatibility is checked against the saved `queue.json`,
including prompt content hashes, so a changed prompt is not skipped by stale
progress.

After every prompt and the optional completion notification finish successfully,
the runner moves the whole active folder to:

```bash
archive/<YYYYMMDD-HHMMSS>-<tag>/
```

Stopped or blocked queues remain under `queues/<tag>/` so they can be resumed.

To build the prompt queue interactively:

```bash
./collect-prompts customer-portal-release
```

For each prompt, paste the full text and finish it with a line containing only
`::end`. When it asks for the next prompt, type `no more prompts`. The helper
writes the prompts inside that queue's `prompts/` directory and updates its
`config.json`.

`queues/<tag>/.env.local` defines the repo the agent should run in:

```bash
PROMPT_QUEUE_WORKDIR=/absolute/path/to/target-repo
```

`queues/<tag>/config.json` defines that queue and reads its working directory:

```jsonc
{
  "project_dir": "${PROMPT_QUEUE_WORKDIR}",
  "prompts": [
    {
      "name": "deployment",
      "lines": [
        "Review deployment config.",
        "Write findings to .planning/reports/deployment.md."
      ]
    },
    {
      "name": "database",
      "file": "prompts/database.md"
    }
  ]
}
```

Prompt file paths are resolved relative to the queue's config file:

```bash
prompt-queue/
  queues/
    customer-portal-release/
      .env.local
      config.json
      prompts/
        database.md
        frontend.md
```

Reattach:

```bash
./prompt-queue attach customer-portal-release
```

Finish the current wait immediately and continue to capture/next prompt:

```bash
./prompt-queue finish-sleep customer-portal-release
```

Toggle stopping after the current prompt. When armed, prompt-queue finishes the
current prompt, leaves the worker pane open on the completed agent, and stops
before the next prompt:

```bash
./prompt-queue stop-next customer-portal-release
```

Kill the tmux session immediately:

```bash
./prompt-queue kill customer-portal-release
```

Print controller state:

```bash
./prompt-queue status customer-portal-release
```

## Controller Keys

New runs split the tmux window into two columns:

- left: short `controller` pane above the resumed planner/recovery agent pane
- right: short `prompt list` pane above the executor/worker pane

Focus the controller pane and press:

| Key | Action |
| --- | --- |
| `S` | Toggle stop after the current prompt finishes |
| `F` | Finish the current wait immediately |
| `Q` | Kill the tmux session now |

## Config

Each `queues/<tag>/` folder is self-contained. Its source-of-truth files are:

- `.env.local` `PROMPT_QUEUE_WORKDIR`: repo the agent should run in
- `config.json` `project_dir`: normally `${PROMPT_QUEUE_WORKDIR}`
- `prompts`: ordered queue of inline prompt objects
- `prompt_files`: ordered queue of prompt files, resolved relative to the selected config file

The default command is `cdx`, which is expected to resolve through your shell
alias. `./prompt-queue run <tag> --cld` overrides the command to `cld` and uses
tmux bracketed paste followed by Enter to submit each prompt. The worker pane
starts an interactive Bash shell, so aliases from `~/.bashrc` are available.
Before worker and planner panes are created, prompt-queue refreshes tmux's
`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` from the invoking shell so
Codex can reach the user-scoped services even when the tmux server is older.
If a matching tmux session remains after its recorded controller process has
exited, the next `pq <tag>` replaces that stale session automatically and
resumes from the last compatible completed prompt. This also works when the
command is entered from the failed session's own worker pane.

Readiness settings:

- `ready_check_seconds`: seconds between worker-pane tail checks, default `60`
- `ready_check_lines`: number of bottom rows to capture for each check, default `1`
- `ready_markers`: marker text that means the agent is done, default `["Ready"]`
- `block_marker`: exact output line that stops the queue, default `DO-NOT-PROCEED`
- `block_check_lines`: recent non-empty rows to inspect after `Ready`, default `10`

JSON does not allow raw multi-line string literals. Use one of these instead:

```json
{
  "prompts": [
    {
      "name": "multi-line",
      "lines": [
        "First line.",
        "Second line.",
        "Third line."
      ]
    },
    {
      "name": "from-file",
      "file": "prompts/from-file.md"
    }
  ],
  "prompt_files": ["prompts/another-file.md"]
}
```

During the agent working phase, the controller samples the worker pane tail
once per minute by default. When the last captured row contains `Ready`, it
captures the run and advances. There is no automatic prompt timeout.

After `Ready` is detected, the controller also checks recent output for an
exact line matching `DO-NOT-PROCEED`. If found, it writes the capture, records
`blocked.json`, leaves the worker pane intact, and stops the queue before the
next prompt.

Blocked recovery settings:

- `blocked_recovery`: enable planner-assisted recovery, default `false`
- `blocked_recovery_session_id`: Codex session id to resume for recovery
- `blocked_recovery_command`: command used for recovery, default `cdx`
- `blocked_recovery_success_marker`: exact proceed line, default `PROCEED-ALLOWED`
- `blocked_recovery_human_marker`: exact human-needed line, default `HUMAN-DECISION-REQUIRED`
- `blocked_recovery_action`: `retry` or `continue`, default `retry`
- `blocked_recovery_max_attempts`: recovery attempts per prompt, default `1`
- `blocked_recovery_check_lines`: recent planner rows to scan after `Ready`, default `20`

With the default `retry` action, a successful recovery retries the blocked
prompt once. The controller never proceeds from a recovery marker alone; the
bottom of the planner pane must first match a configured ready marker, normally
`Ready`.

Completion notification settings:

- `completion_notify`: resume the planner after all prompts finish, default `false`
- `completion_notify_session_id`: Codex session id to resume; defaults to `blocked_recovery_session_id`
- `completion_notify_command`: command used for completion verification; defaults to `blocked_recovery_command` or `cdx`
- `completion_notify_check_lines`: reserved recent planner rows setting, default `20`
- `completion_prompt_file`: optional path relative to `config.json`; replaces the generic verification instructions while preserving runtime paths and queue evidence in the resumed prompt

Completion notification has no proceed marker. The controller waits for the
planner pane bottom line to match a ready marker, normally `Ready`.

Readiness samples are written to:

```bash
<project>/.planning/work/prompt-queue/<tag>/<YYYYMMDD-HHMMSS>/ready-checks/
```

Progress and timing files are written to each run directory:

- `queue.json`: the queue snapshot, including queue and prompt content hashes
- `progress.json`: completed prompt indices and the last finished prompt
- `timings.jsonl`: one JSON record per finished prompt with start time, finish
  time, duration, and status

The controller pane also shows total running time and the current prompt's
elapsed time while a prompt is active.

`prompt_delivery: "argument_file"` writes each prompt to:

```bash
<project>/.planning/work/prompt-queue/<tag>/<YYYYMMDD-HHMMSS>/prompts/
```

and launches Codex as:

```bash
cdx "$(cat <prompt-file>)"
```

Captures are written to:

```bash
<project>/.planning/work/prompt-queue/<tag>/<YYYYMMDD-HHMMSS>/captures/
```
