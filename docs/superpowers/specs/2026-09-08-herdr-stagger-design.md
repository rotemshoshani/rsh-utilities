# herdr-stagger — design

Date: 2026-09-08
Status: approved for implementation

## Problem

A planning session ends with a Codex agent holding a concrete, approved plan
and waiting for one word: go. Ten panes of those accumulate over a morning.

Releasing them all at once is not an option — they touch the same repository
and would collide. So the operator releases one, watches for it to finish,
releases the next, and spends the rest of the day being a timer with a
keyboard. The judgement in that loop is real but thin: it is almost entirely
"has enough time passed", and the operator has already decided how much time
each one needs before starting.

`herdr-stagger` runs that loop. The operator picks the panes in fire order,
says how long to wait before the first and between the rest, and walks away.

## Non-goals

- **Knowing when an agent is actually finished.** No transcript scraping, no
  completion heuristics. The operator's estimate of the gap is the input. The
  one exception is refusing to prompt an agent that cannot receive a prompt —
  see "Readiness" — which is about delivery, not completion.
- **Composing the plans.** Every selected pane must already hold a plan
  concrete enough that "go ahead" is unambiguous. That is the operator's
  precondition, and the tool cannot check it.
- **Detached operation.** The runner is foreground and dies with Ctrl-C. An
  operator who wants it to outlive the pane runs it in a spare Herdr pane;
  that is a split, not a feature.
- **Surviving a Herdr restart.** Panes are gone; there is nothing to prompt.
- **Being used by an agent.** `herdr-delay` is the agent-facing tool. This one
  is driven by a human at a keyboard and is interactive by default.

## Interface

```
herdr-stagger                                   # interactive picker
herdr-stagger --pane wQ:p5Z --pane wQ:p64 \     # same run, no questions asked
              --start 5m --gap 20m --yes
herdr-stagger --workspace wQ                    # picker, candidates filtered
```

| Flag | Meaning |
|---|---|
| `--pane ID` | repeatable; fire order is argument order. Skips pane selection. |
| `--start DUR` | delay before the first send. Skips that question. |
| `--gap DUR` | repeatable; one value applies to every gap, or pass exactly *n-1* values for per-gap control. |
| `--prompt TEXT` | text sent to every pane. Skips that question. |
| `--hold-cap DUR` | how long to hold for an unready pane before giving up (default 30m). |
| `--workspace ID` | restrict picker candidates to one workspace. |
| `--yes` | skip the final confirmation. |
| `--no-notify` | suppress the desktop notification at the end. |

Durations use the `herdr-delay` grammar: `90`, `90s`, `10m`, `2h`, `1h30m`,
`MM:SS`, `HH:MM:SS`, with a 24h ceiling.

Every flag suppresses exactly the question it answers, so a fully-flagged
invocation with `--yes` is non-interactive and a partially-flagged one still
asks for the rest.

Exit codes: `0` every step sent, `1` any step failed, gave up, or was
cancelled, `2` usage error, `3` not running under Herdr (`HERDR_ENV != 1`).

### Picker

`herdr agent list` becomes a numbered table sorted by workspace, tab, and
pane:

```
  #  pane     kind   status   title
  1  wQ:p64   codex  idle     Resume Priority 2 recovery
  2  wQ:p5Z   codex  idle     Execute CRM ownership cutover
  3  wQ:p6M   codex  idle     Document template tags guide
  4  wQ:p6S   codex  blocked  Add company meeting recording
  5  wN:pV    claude working  (this pane)

Pick panes in fire order: 2 1 3
```

Titles come from `terminal_title_stripped`, which is what the operator
recognises; `wQ:p6M` is not. Selection is numbers in fire order, space- or
comma-separated. A repeated number is rejected as a typo — two sends to one
agent is never the intent. The calling pane is listed but refuses selection.

A `blocked` pane is selectable: by its fire time the overlay will probably be
gone, and if it is not, the hold logic covers it.

Then the schedule, and one confirmation:

```
  t+5m00s   wQ:p5Z  Execute CRM ownership cutover
  t+25m00s  wQ:p64  Resume Priority 2 recovery
  t+45m00s  wQ:p6M  Document template tags guide

Start? [y/N]
```

Those times are labelled *earliest*: a hold pushes everything after it back.

## Architecture

One process, four modules, no state files. A run lives and dies with its
terminal, so there is nothing to reconcile across invocations and no `status`
or `cancel` subcommand to write.

- **`duration.py`** — the parser and formatter, vendored from `herdr-delay`
  unchanged. ~70 lines duplicated in exchange for two independently
  installable tools; promoting it to `shared/` would mean editing a working
  tool and its tests to serve a new one.
- **`herdr.py`** — JSON-over-CLI wrapper, trimmed to what this tool needs:
  `agent_list`, `agent_status`, `agent_prompt`, `notification_show`. Errors
  surface as `HerdrError` carrying the server's own code.
- **`picker.py`** — candidate table, selection parsing, gap parsing, plan
  construction. Pure functions plus one thin `collect()` that reads a stream,
  so every parsing rule is testable without a terminal.
- **`runner.py`** — the chain executor. Clock, sleeper, Herdr client, and
  observer are all injected.
- **`display.py`** — renders the plan, the live table, and the summary.
- **`cli.py`** — argument parsing, the `HERDR_ENV` gate, wiring, exit codes.

### The schedule is a chain

Each step carries `after_seconds`: how long to wait after the *previous step
resolved*, not a wall-clock time computed up front. The first step's
`after_seconds` is the start delay.

This is the whole reason "hold, then shift the rest" needs no special case. A
six-minute hold on step 2 delays its resolution by six minutes, and step 3 is
scheduled from that resolution, so the operator's intended gap between two
implementations is preserved automatically. A step that gives up after the
hold cap chains identically — the next gap is measured from the moment it gave
up.

```
next_at = now + step.after_seconds
sleep until next_at, in one-second slices, redrawing between
readiness loop (below)
send
resolved = now
```

### Readiness

At fire time the agent's status is re-read, because the picker's snapshot is
by then minutes or hours old.

`idle` and `done` are promptable. `done` matters as much as `idle`: an agent
whose tab has not been looked at in the focused UI settles into `done`, and
every pane in a stagger run is by definition unwatched.

`working` and `blocked` hold. Polling every 5s, up to `--hold-cap`, then
`GAVE_UP`. Holding on `blocked` is the important one: `herdr agent prompt`
submits text plus Enter, and against an approval overlay the Enter answers the
overlay's highlighted default while the text is discarded. That is not a
delayed send, it is a wrong answer to a question the operator never saw.

A pane that has vanished from `agent list` is `FAILED` immediately; there is
nothing to hold for.

### Sending

`herdr agent prompt <pane> <text>`, without `--wait`. The runner has a
schedule to keep and does not care what the agent does next.

Herdr requires an observed lifecycle change within five seconds of a prompt
sent from a non-working state and returns `agent_prompt_stalled` otherwise.
That error becomes `FAILED` for the step; the run continues to the next.

There is no fallback to `herdr pane run`. `pane run` appends Enter to a raw
shell, so a fallback would turn an undeliverable prompt into a shell command
executing in the operator's pane. Undeliverable is an acceptable outcome.

### Display

On a TTY, a redrawing block: sent steps with their timestamps, the current
step's countdown or hold, pending steps with their gaps. Redraw is
cursor-up plus line-erase over a fixed number of lines, once a second.

Off a TTY (piped, logged), one line per event instead. A tool that emits ANSI
cursor motion into a log file is a tool whose logs cannot be read.

Ctrl-C marks every unstarted step `CANCELLED`, prints the summary, exits 1. A
`herdr notification show` at the end reports the tally, because a run that
takes three hours ends when the operator is elsewhere.

## Testing

Mirrors `herdr-delay`: a fake clock and fake Herdr client for logic, a
recording `herdr` stub on `PATH` for delivery. No test contacts the live
socket.

- **Chain** — a hold shifts every later step by exactly the hold; a step that
  gives up still chains the next gap from its give-up moment; gaps are
  measured from resolution, never from the original plan.
- **Readiness** — `idle` and `done` send; `working` and `blocked` hold then
  send once promptable; hold cap produces `GAVE_UP`; a vanished pane is
  `FAILED` without waiting.
- **Sending** — `agent_prompt_stalled` is `FAILED` and the run continues; the
  stub records the exact text delivered to each pane; no `pane run` call
  appears in the stub's recording, ever.
- **Picker** — order is preserved, duplicates and out-of-range numbers are
  rejected, the calling pane refuses selection, one gap value fans out to all
  gaps, *n-1* values map positionally, any other count is an error.
- **Cancellation** — an interrupt mid-wait marks the remainder `CANCELLED`
  and exits 1.
