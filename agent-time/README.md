# agent-time

Local terminal reports of Codex turn time per project. Reads Codex CLI and
ChatGPT desktop session records on the same machine. No keyboard monitoring,
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
