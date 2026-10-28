# utilities

A grab bag of small personal tools — terminal launchers, timers, and automation around Claude Code / GSD workflows.

## Project-local launcher config

`dev-env`, `council`, and `prod-env` can read [`.rsh-utils.example.json`](.rsh-utils.example.json)
as a project-local configuration template. Copy it to `.rsh-utils.json` in the
directory where you launch a tool, then keep only the fields you need. The file
is not searched in parent directories or Git roots: the launch directory is the
scope. For `dev-env` and `council`, command-line options override environment
variables, which override project config and built-in defaults. `prod-env` gets
its monitor list directly from project config.

The example is the canonical schema and is validated in tests. Update it with
every supported config change. Do not place secrets in `.rsh-utils.json`.

## Projects

### [agent-time](agent-time/)
Local terminal reports of Codex CLI and ChatGPT desktop turn time per project.
Tracks future turns, combines worktrees, and shows summed agent time alongside
elapsed active time with overlaps counted once. No prompting-time monitoring.
An optional loopback collector also reports estimated UAT time from development
click/scroll signals, with `dev-env` managing its lifecycle.

### [arch-advisor](arch-advisor/)
Autonomous tmux runner for repeated AI architecture and performance review passes. It writes code-referenced findings and optimization plans around deployment, database/query patterns, caching, runtime hot paths, observability, and cost/performance tradeoffs.

### [cc-commands](cc-commands/)
Source of truth for custom Claude Code slash commands (`/0-done`, `/0-sync`, `/0-teach`, etc.). Files here get copied into `~/.claude/commands/` via the sync command.

### [herdr-delay](herdr-delay/)
Lets a coding agent wait on CI, a preflight script, or a health check without burning a turn per poll. It returns immediately, keeps waiting in a detached process after the agent's turn ends, and prompts the agent once with the result.

### [herdr-stagger](herdr-stagger/)
Releases a queue of plan-ready agents into their panes, one every N minutes. Pick the Herdr panes in fire order, set the delay before the first send and the gaps between the rest, and walk away; a pane that is blocked or still working at its turn is held for, and everything after it shifts by the same amount.

### [herdr-space](herdr-space/)
fzf picker over Herdr workspace presets. Each `*.json` beside the script defines a workspace label and its tabs; picking one opens the workspace and starts each tab's command, or focuses the workspace when it is already running.

### [council](council/)
A tmux launcher for brainstorming with two AI CLIs side-by-side — Claude Code on the left, Codex on the right — with a controller bar that relays one model's last reply to the other on a single keypress.

### [dev-env](dev-env/)
Tiny tmux launcher for projects with two long-running dev processes (default: `npx convex dev` and `npm run dev`). Single-keypress controller for restarting either pane or tearing the session down.

### [gsd-auto](gsd-auto/)
Terminal-native automation controller for GSD workflows. Watches a tmux pane running Claude Code and auto-injects `/gsd-plan-phase` / `/gsd-execute-phase` so phase-based projects can run unattended overnight. Multiple iterations live here; `v4` is current.

### [interval-timer](interval-timer/)
Browser-based interval/HIIT timer. Open `index.html`, import a CSV of exercises (name, work, rest, repeat), and the runner cues each transition with audio clips from `Audio/` or browser TTS.

### [pomodoro-tui](pomodoro-tui/)
Terminal Pomodoro timer (`pomopp`) that splits work into smaller chunks — `--work 10x5 --rest 10` runs five 10-minute cuts before a rest, so you only think about the next cut rather than a full session.

### [prod-env](prod-env/)
Config-driven tmux dashboard for production monitors. It has structured Convex error filtering, Vercel deployment status and failed-build logs, arbitrary command panes, and automatic layouts for one to nine monitors.

### [statusline](statusline/)
Custom Claude Code statusline (Node script) showing model, current task or GSD phase state, working directory, and context usage. `install.sh` wires it into Claude Code's settings.

### [slp](slp/)
Repository copy of the `slp` countdown command, accepting seconds, `MM:SS`, or `HH:MM:SS`.

## Archived

`codex-delay`, `herdr-codex-auto`, and `prompt-queue` are retired under
[`archive/`](archive/). Their code and local queue data are retained for reference;
they are no longer active launchers or installed skills.
