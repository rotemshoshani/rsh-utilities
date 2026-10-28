# agent-time

Local terminal reports of Codex and Claude Code turn time per project. Reads
local session records from both harnesses on the same machine. No keyboard monitoring,
prompting-time estimates, network service, daemon, or special agent launcher.
Requires Python 3.10+ and Git for repository/worktree grouping.

## Start tracking

From the utilities repository:

```bash
./agent-time/agent-time init
./agent-time/agent-time report
./agent-time/agent-time report --period week
./agent-time/agent-time report --from 2026-10-22 --to 2026-10-31
./agent-time/agent-time report --project /path/to/project
./agent-time/agent-time turns --project /path/to/project
```

Optionally link the executable into a directory already on your PATH, or
install this directory as a Python package. The checked-in executable works
without installing dependencies.

`init` records the current time. Repeating it preserves the original start
time. Only turns starting at or after that point qualify; existing sessions
can contribute new turns. Turns already running at initialization are excluded.
There is no historical-import option.

Each report imports newly appended records before printing results. You do not
need to leave anything running. Keep the source transcripts available until a
report has imported them; deleted records cannot be recovered. Already imported
timing records remain available if the original files disappear.

## What the numbers mean

- **Agent time** sums completed, failed, and interrupted turn durations.
- **Elapsed active time** combines their intervals, counting overlaps once.
  Two simultaneous ten-minute turns produce twenty agent-minutes and ten
  elapsed minutes. The overall elapsed total combines intervals across all
  selected projects; it does not add up the project elapsed totals.
- Tool execution and tests inside a turn count. Reading, prompting, gaps between
  turns, and deferred `herdr-delay` waits between turns do not count.
- Unfinished turns have no reliable endpoint and are excluded from both totals.
  They remain visible as incomplete; no arbitrary inactivity timeout closes them.
- Only top-level sessions count. Delegated subagents are excluded because parent
  turns already span that activity. These totals measure occupied turn time,
  not CPU time, human effort, or billable hours.

Dates are inclusive and use the machine's local timezone; weeks start Monday.
Intervals crossing date boundaries are clipped to the selected range. A turn
overlapping multiple daily reports appears in each day's count, with only that
day's duration. Incomplete turns appear in the range containing their start;
the coverage footer also reports unfinished turns across all dates and projects.

Git worktrees and nested directories group under their shared Git repository.
Separate clones are separate projects. Non-Git or unavailable directories use
their recorded absolute path. Attribution is established when a file is first
imported; retain repository locations for consistent grouping. The project
filter uses the same resolution rules. Reports display full paths so projects
with identical folder names remain distinguishable.

## Storage and compatibility

### Estimated UAT activity

`agent-time serve --project /path/to/project --port 4319` starts an optional
loopback-only collector. `dev-env` can manage it in an `agent-time-uat` tmux
window using `dev_env.uat_port` in the project's launcher config, or
`--uat-port 4319`. Each project needs a distinct port. For an already running
environment, `dev-env --attach-uat` starts/reuses its configured collector and
restarts only the bottom frontend worker to pass the port to it.

The app needs a development-only integration; ordinary request logs are not
activity signals. Meatbar has the first integration: trusted, focused clicks
and scrolls send at most one small signal every ten seconds through its same-origin
`/api/dev-activity` endpoint. The endpoint forwards only an anonymous tab ID,
sequence number, and `activity`/`hidden` kind to loopback. No keys, text, routes,
DOM targets, cookies, or application content enter the timing database. No
signals are sent on page load or by a periodic heartbeat. Production disables
both the component and endpoint. Collector failure drops signals without retries.

Each activity signal marks a 120-second interval from its receive time. New
activity extends that interval; hiding/unloading a tab shortens its remaining
tail. Overlapping tabs merge, as do overlapping projects in the overall UAT
total. Reports never count future seconds before they elapse. Missing hide
events leave at most a two-minute idle tail. The ten-second throttle can miss
up to ten seconds of activity near the final interaction; this is an estimate,
not a billable-time record. Passive reading, keyboard-only activity, and browser
automation are not reliably distinguished or measured. Agent time and UAT time
remain separate; they are not added together.

Run `agent-time report --project /path/to/project` to see **Estimated UAT**.
The collector adds `uat_tabs` (anonymous sequencing state) and `uat_intervals`
to the existing local SQLite database, preserving the agent tracking cutoff
and history. Its `/health` endpoint reports only service/project metadata.

### Claude Code

Claude Code logs are imported from `~/.claude/projects/*/*.jsonl`, or
`CLAUDE_CONFIG_DIR` / the global `--claude-home PATH` option. Both the CLI
reports and the private scheduled cloud sync import both harnesses.

Install the local lifecycle plugin once to count Claude Remote Control and
headless/SDK turns, which may omit transcript duration records:

```bash
claude plugin marketplace add /absolute/path/to/agent-time --scope user
claude plugin install agent-time@agent-time-local --scope user
```

Start new Claude sessions after installation (restart Remote Control servers).
The plugin is cached by Claude; after changing its code, update/reinstall it.
Tracking must already be initialized. `UserPromptSubmit` starts an interval;
`Stop` closes it, and `StopFailure` marks API failures. The hooks write only
session IDs, generated timing IDs, project paths, timestamps, and status into
the local database. Prompt text, responses, tool arguments, and credentials
are never persisted. Hooks fail open and never change the model's response.

A session belongs to hooks from its first observed prompt onward. Transcript
`turn_duration` records ending after that point are ignored, preventing two
collectors from counting the same session's work twice. Earlier history keeps
its original IDs. Repeated Stop events do not extend a completed turn, except
an explicit `stop_hook_active` continuation, which updates the same record.
A new prompt cannot close an unobserved interruption at the later time: the
old incomplete interval stays excluded. This avoids counting intervening idle
time, but interrupted/crashed or hook-disabled turns may be undercounted.
Subagent hooks are excluded. Concurrent prompt steering can leave the earlier
segment incomplete; this collector does not estimate its missing endpoint.
Other Stop hooks that block completion can briefly publish a provisional end;
a subsequent continuation Stop updates it under the same ID.

Without hooks, only explicit `system` / `turn_duration` records count.
`durationMs` is authoritative; start is reconstructed as timestamp minus duration,
so midnight splitting and overlap placement are approximate. Subagent logs and
sidechains are excluded; partial lines retry and duplicate IDs deduplicate.
Missing durations are never inferred from message gaps. The original tracking
cutoff remains in force. The older Remote Control test with no duration or hooks
cannot be recovered as an exact timing record.

`turns` labels provider and launcher, and `report` includes per-project source
subtotals. `--source codex` and `--source claude` filter by launcher; `t3` is
reserved, not integrated yet. Provider and launcher are separate dimensions:
a future Claude-through-T3 turn should have provider `claude`, launcher `t3`,
and count once, not once under each dimension. UAT is independent of this filter.
Local `turn_sources` stores hook provenance; legacy records infer provider from
the session namespace. Cloud timing records explicitly carry `provider`,
`launcher`, and `timing`, with unchanged record keys and combined daily totals.
The dashboard still shows combined agent totals; no UI source selector is added.
Future T3 integration must correlate its provider-native identity with these
records before ingestion; it must not simply add a second set of intervals.

Verified on Claude Code 2.1.312: interactive transcript timing (10.592 seconds),
and a headless SDK-mode live test with hooks (10.890 seconds vs SDK 11.004).
The latter exercises the previously missing headless timing path. Acceptance
from the user's Remote Control client is a separate final check.
Official lifecycle reference: https://code.claude.com/docs/en/hooks

### Agent sources

Sources are `$CODEX_HOME/sessions` and `$CODEX_HOME/archived_sessions`, defaulting
to `~/.codex`. Data is stored in
`${XDG_DATA_HOME:-~/.local/share}/agent-time/timing.sqlite3`. The database contains
only session/turn IDs, project paths, boundary timestamps, statuses, the tracking
start time, file offsets, and coverage counters. Conversation text and tool
output are never persisted or printed. Source transcripts are never modified.

Global path overrides precede the subcommand:

```bash
./agent-time/agent-time --codex-home /path/to/codex --data-dir /path/to/data report
```

The reader recognizes `session_meta` and `event_msg` lifecycle records:
`task_started`, `task_complete`, and `turn_aborted`. It also accepts explicit
`task_failed`/`turn_failed` terminal records or a failed status on completion.
An ordinary error message is not assumed to end a turn, because errors may be
recoverable. Failure paths without an explicit terminal boundary stay incomplete.

Boundaries use the envelope's precise timestamp, falling back to the explicit
`started_at`/`completed_at` fields. A terminal event with an explicit start can
recover a missing start event. Unsupported timing formats are not estimated.

The installed Codex 0.163.0 session format was checked using real CLI and
ChatGPT desktop records, and durations were compared against their paired
boundaries using isolated sanitized fixtures. No new desktop conversation was
launched as an acceptance test. This is a local transcript format, not a stable
public API: future client changes can require reader updates. A client using a
different Codex home needs the appropriate source override.

Reports show coverage warnings for unreadable files, malformed records,
unsupported timing, files with user input but no timing boundaries (`untimed`),
and partially written trailing lines. Partial lines are
retried next time. Session/turn IDs prevent duplicate counts across repeated
imports and archive moves. Replaced or observed-truncated files are reread;
previously imported turns are retained. The reader assumes append-only logs
between imports, not arbitrary in-place editing of past transcript contents.

## Verification

```bash
PYTHONPATH=agent-time/src python3 -m unittest discover -s agent-time/tests -v
```

Run from the utilities repository. Tests use temporary synthetic transcripts
and repositories; they do not read personal session content or change tracking
state. Coverage includes concurrency, interruptions, incomplete turns, start
cutoffs, deduplication, archive moves, partial records, worktrees, date clipping,
daylight-saving transitions, and CLI output.
