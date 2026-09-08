# herdr-stagger

Releases a queue of plan-ready agents into their panes, one every N minutes.

A morning of planning sessions ends with ten Codex panes each holding an
approved plan and waiting for one word: go. They touch the same repository, so
they cannot all go at once. Releasing them by hand means sitting there being a
timer with a keyboard.

Pick the panes in fire order, set the delay before the first and the gap
between the rest, walk away.

## Usage

```bash
herdr-stagger                                   # interactive picker
herdr-stagger --workspace wQ                    # picker, one workspace only
herdr-stagger --pane wQ:p5Z --pane wQ:p64 \     # same run, no questions asked
              --start 5m --gap 20m --yes
```

```
   #  pane    kind   status   title
   1  wQ:p64  codex  idle     crm | Resume Priority 2 recovery
   2  wQ:p5Z  codex  idle     crm | Execute CRM ownership cutover
   3  wQ:p6M  codex  idle     crm | Document template tags guide
   4  wQ:p6S  codex  blocked  crm | Add company meeting recording

Pick panes in fire order: 2 1 3
First send in [5m]:
Gap between sends [20m]:
Prompt to send [Go ahead and implement the approved plan.]:

  t+5m00s     wQ:p5Z  crm | Execute CRM ownership cutover
  t+25m00s    wQ:p64  crm | Resume Priority 2 recovery
  t+45m00s    wQ:p6M  crm | Document template tags guide

  Times are the earliest: a pane held at its turn shifts the rest.

Start? [y/N]: y
```

Then it counts down in place:

```
herdr-stagger — 3 steps, started 14:02:11

  ✓ wQ:p5Z  SENT 14:07:11                 Execute CRM ownership cutover
  ▶ wQ:p64  next in 12m38s                Resume Priority 2 recovery
    wQ:p6M  +20m00s                       Document template tags guide

  Ctrl-C cancels the remaining steps.
```

Every flag suppresses exactly the question it answers. `--yes` answers all of
them with their defaults, so `--pane ... --yes` is fully non-interactive.

| Flag | Meaning |
|---|---|
| `--pane ID` | repeatable; fire order is argument order |
| `--start DUR` | delay before the first send (default 5m) |
| `--gap DUR` | repeatable; one value for every gap, or exactly *n-1* values |
| `--prompt TEXT` | text sent to every pane |
| `--hold-cap DUR` | give up on an unready pane after this (default 30m) |
| `--workspace ID` | restrict the picker to one workspace |
| `--yes` | ask nothing; take the defaults |
| `--no-notify` | no desktop notification at the end |

Durations accept `90`, `90s`, `10m`, `2h`, `1h30m`, and `slp`-style `MM:SS` /
`HH:MM:SS`. `0` means no delay.

Exit codes: `0` everything sent, `1` something did not, `2` usage, `3` not
running under Herdr.

## What it does not do

It does not know when an agent has finished. There is no transcript scraping
and no completion heuristic — the gap you set is the estimate, and it is the
only one. Every selected pane must already hold a plan concrete enough that
"go ahead" is unambiguous; nothing here can check that for you.

## The schedule is a chain

Each step waits its own delay measured from the moment the **previous step
resolved**, not from a clock computed at launch. So a pane held for six
minutes pushes everything after it back by six minutes, and the gap you chose
between two implementations survives:

```
t+0:05  wQ:p5Z  SENT
t+0:25  wQ:p64  blocked — holding...
t+0:31  wQ:p64  SENT (held 6m)
t+0:51  wQ:p6M  SENT          <- shifted, still 20m after the one before it
```

## Not ready at its turn

At fire time the agent's status is re-read, because the picker's snapshot is
by then hours old.

`idle` and `done` are promptable. `done` matters as much as `idle`: an agent
whose tab has not been looked at settles there, and every pane in a stagger
run is unwatched by definition.

`working` and `blocked` hold, polled every 5s until `--hold-cap` runs out and
the step is recorded as `GAVE_UP`. Holding on `blocked` is the important one:
`herdr agent prompt` submits text plus Enter, so against an approval overlay
the Enter answers the overlay's highlighted default and the text is discarded.
That is not a delayed send, it is a wrong answer to a question you never saw.

A pane whose agent has vanished is `FAILED` immediately. A prompt Herdr
reports as `agent_prompt_stalled` is `FAILED` too, and the run carries on to
the next pane.

Delivery never falls back to `herdr pane run`, which appends Enter to a raw
shell — a fallback would execute the prompt text as a shell command in your
pane. Undeliverable is an acceptable outcome; executing the prompt is not.

## Off a terminal

Piped or logged, the live block degrades to one line per event and emits no
cursor motion. Ctrl-C marks the remaining steps `CANCELLED`, prints the
summary, and exits 1. A desktop notification reports the tally at the end,
since a three-hour run finishes while you are somewhere else.

## Relationship to herdr-delay

Opposite audiences. [`herdr-delay`](../herdr-delay/) is agent-facing: an agent
schedules its own wake so it stops burning a turn per poll. `herdr-stagger` is
operator-facing: you release other agents on a timer. They share the duration
grammar and the JSON-over-`herdr` wrapper, vendored rather than shared so
either can be installed alone.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Unit tests use a fake clock and a fake Herdr client, so a three-hour schedule
runs in microseconds. Delivery is tested against a recording `herdr` stub
placed on `PATH`; no test contacts the live Herdr socket.
