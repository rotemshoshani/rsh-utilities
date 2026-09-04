# herdr-delay — design

Date: 2026-09-04
Status: approved for implementation

## Problem

Coding agents that need to wait on something slow — CI, a preflight script, a
deploy, a health check — have only one native option: poll. An agent that
re-runs `gh run list` every thirty seconds burns a turn per poll, and each turn
carries the whole conversation context. A thirty-minute CI wait costs sixty
turns to produce one bit of information.

The waiting itself is free. Only the agent's participation in it is expensive.
`herdr-delay` moves the waiting out of the model and into a detached shell
process, and wakes the agent once, with an answer.

## Non-goals

- Scheduling work for a *future* agent session. `codex-delay` already does
  that, with a human at the keyboard.
- Surviving a Herdr server restart. A waiter whose pane is gone is a waiter
  with nothing to wake.
- A TUI. This tool is invoked by agents, not people.
- Parallel waits. See "One waiter per pane" below.

## Interface

```
herdr-delay 10m [REASON]                       # sugar for --in 10m --reason
herdr-delay --in DUR [--then CMD] [--reason R]
herdr-delay --until CMD [--every DUR] [--deadline DUR] [--in DUR]
herdr-delay cancel
herdr-delay status
```

Durations accept `90`, `90s`, `10m`, `2h`, `1h30m`, and `slp`-compatible
`MM:SS` / `HH:MM:SS`. Defaults: `--every 30s`, `--deadline 30m`. Hard ceiling
of 24h on any wait, so nothing outlives the session it was scheduled from.

The agent never passes a pane ID; the front works out which pane to wake. See
"Resolving the pane" below — the obvious approach does not survive contact with
Codex.

Outside Herdr (`HERDR_ENV != 1`) the tool exits 2 with a clear message rather
than degrading to a blocking sleep. A silent fallback that pins the agent's
turn is worse than a refusal.

### Two check modes

`--until CMD` **polls**: exit 0 resolves, any nonzero means "not yet, retry
until the deadline". This fits health checks, marker files, and any status
command piped through `grep -q`.

`--then CMD` **runs once** after the delay: any exit resolves, exit 0 is
`RESOLVED` and nonzero is `FAILED`. This fits builds, test suites, and
blocking watchers like `gh run watch --exit-status`.

Neither mode requires the agent to author a wrapper or map exit codes onto a
protocol. Choosing between them is a property of the job, not a decision the
agent has to get right.

`--deadline` applies to `--until` only. A `--then` command is the job itself
and is bounded only by the 24h ceiling.

### One waiter per pane

A second invocation from the same pane cancels the first and says so on
stdout. An agent reasons about one thing it is waiting for; a forgotten waiter
surfacing three prompts later with news about an abandoned task is a bug, not
a feature. `cancel` kills the current waiter, `status` reports it.

## Architecture

Two processes.

**Front** — foreground, returns in milliseconds. Validates the environment,
parses arguments, records the pane's current foreground agent pid, replaces
any existing waiter for this pane, writes the state file, spawns the waiter,
prints, exits 0. It must never block; that is its entire contract with the
agent's turn.

**Waiter** — detached via `setsid` so it survives the agent's tool call, its
shell, and the process-group teardown that ends the turn. Two phases:

*Resolve.* Sleep `--in`; or poll `--until` every interval until exit 0 or the
deadline; or sleep then run `--then` once. Records outcome, exit code, elapsed
time, poll count, and the last 20 lines of combined stdout/stderr.

*Deliver.* Described below.

State lives in `${XDG_RUNTIME_DIR:-/tmp}/herdr-delay/<sanitized-pane>.json`
with a sibling `.log`. Pane IDs contain `:` and are sanitized for filenames.
`cancel` signals the waiter's process group, so a check command running at
that moment dies with its waiter. A state file whose pid is dead counts as
absent.

## Resolving the pane

`$HERDR_PANE_ID` is the obvious answer and it is wrong often enough to matter.

Codex delegates shell execution to a long-lived app-server process rather than
running commands as a child of the pane's TUI. That process is started once and
reused, so its environment belongs to whichever pane first launched it. On the
machine this was developed against, three such processes were carrying
`HERDR_PANE_ID=wN:pB` — a pane that no longer existed. A `herdr-delay`
invoked from a Codex pane read that value and failed with `pane_not_found`,
while the Codex agent was alive and visible in `wN:pP`.

The process ancestry of that shell command was `bash -> codex -> codex`, none
of which belong to any live pane, and it had no controlling tty. So there is
nothing in the calling process that points at the right pane. Claude and Pi are
unaffected: their tool commands run under the pane's own process and the
variable is correct.

Resolution is therefore a ladder, and reports which rung it used:

1. `--pane ID` if given, validated against the live agent list.
2. `$HERDR_PANE_ID`, if a live agent occupies that pane.
3. If the variable names a live pane with no agent, stop — there is nothing to
   wake, and guessing would wake somebody else.
4. Otherwise identify the caller by what it is: read the agent kind off our own
   process ancestry (`argv[0]` basenames up the ppid chain, matched against the
   kinds Herdr reports), then narrow by working directory and `working` status.
   The first filter that yields exactly one candidate wins.
5. If several candidates survive, refuse and print them. Waking the wrong
   agent puts a message in somebody else's conversation; failing loudly does
   not.

The ancestry check is what makes step 4 work in a shared repo. Four agents in
one directory is ordinary; four agents of the *same kind*, all `working`, in
one directory is not.

## Delivery

This section is the one that was settled empirically rather than by reading
docs. Three findings from a live probe against a scratch pane drove it.

**Finding 1 — injecting into a `blocked` agent is destructive, and the CLI
reports success.** A Claude Code agent sitting on a selector with `No, exit`
highlighted was sent `herdr agent prompt`. The call returned a success
response. The wake text never appeared. The Enter that `agent prompt` submits
atomically with the text answered the overlay, selecting the highlighted
default, and the agent exited three seconds later. Injecting during an
approval prompt does not steer the agent; it answers the prompt.

**Finding 2 — `agent prompt` can silently drop text and still return
success.** The first prompt sent to a freshly started Codex, after
`agent start` reported `interactive_ready: true`, returned success and never
appeared on screen. The identical prompt sent again landed. Success responses
are not evidence of delivery.

**Finding 3 — Codex does not report `agent_session`.** Claude and Pi populate
it with a conversation ID; Codex reports null. Session identity therefore
cannot come from `agent_session`. `herdr pane process-info` returns the
foreground process pid uniformly across agent kinds, and same-pid means
same-conversation, so pid is the identity check.

The resulting path:

```
1. pid check     recorded pid still in the pane's foreground processes?
                 no  -> abandon, log, herdr notification show
2. status        blocked -> herdr agent wait --until idle --until done
                            --until working, then re-check pid
                 otherwise -> proceed immediately
3. send          herdr agent prompt "$pane" "[herdr-delay:$marker] ..."
                 (no --wait; its stall detection is not what we need here)
4. verify        agent read for the marker, or an observed status change
                 miss -> resend once -> miss -> log undelivered + notify
5. never         fall back to pane send-text / send-keys
```

Step 5 matters. The raw pane surface ignores agent state and would "work" in
every failure case, but `pane run` appends Enter — so a fallback down that
path turns an undeliverable wake message into a shell command executed in the
user's pane. Undeliverable is an acceptable outcome. Executing the wake text
is not.

Sending into a `working` agent is deliberate: both Claude Code and Codex
accept queued input mid-turn, and it arrives as a steer. Only `blocked` is
carved out, and it holds indefinitely rather than on a timer — a blocked agent
is waiting on a human, and the pid check bounds the wait naturally if that
human kills the agent instead of clearing the prompt.

## Messages

The two strings are the product; everything above is plumbing.

At scheduling time stdout has exactly one job — stopping the agent:

```
[herdr-delay] Waiter started (pid 41022) for pane wN:pK.
  mode     : poll every 60s until exit 0
  command  : gh run list -q '.[0].status' | grep -qx completed
  deadline : 30m (expires 03:44:07)

END YOUR TURN NOW. Do not poll, sleep, or re-run this command.
You will be prompted automatically with the result.
```

At wake time:

```
[herdr-delay:a3f9] Your scheduled wait finished.
  waited for : gh run list -q '.[0].status' | grep -qx completed
  outcome    : RESOLVED (exit 0) after 24m12s
  polls      : 25

  --- last 20 lines of output ---
  completed
```

Outcomes: `RESOLVED` (exit 0, or a plain `--in` elapsing), `FAILED` (`--then`
exited nonzero), `TIMED_OUT` (deadline reached with no resolution), `ERROR`
(the command could not be executed at all).

## Implementation

Python 3 standard library with a bash wrapper, mirroring `codex-delay`'s
layout. The duration grammar, the exit-code protocol, the poll loop, and the
message formatting are where bugs will live, and they are worth testing in
isolation. `herdr-codex-auto`'s bash+jq approach would work but puts that
logic out of reach of unit tests.

```
herdr-delay/
  herdr-delay            bash wrapper -> python3 -m herdr_delay
  src/herdr_delay/
    cli.py               argument parsing, front command, cancel, status
    duration.py          duration grammar
    state.py             state dir, state file, waiter lifecycle
    herdr.py             typed wrapper over the herdr CLI
    waiter.py            resolve + deliver
    message.py           both message strings
  tests/
```

## Testing

Unit tests inject a fake clock and a fake command runner, so the poll loop and
the deadline arithmetic run in microseconds. Delivery is tested against a
**`herdr` stub placed on `PATH`** that records its argv to a file, which lets
the tests assert the ordering that Finding 1 demands — that a blocked agent is
waited on before any prompt is sent — without touching the real socket. No
test may contact the live Herdr server.

A `--in 1s` end-to-end test exercises front, detachment, resolve, and delivery
against that stub.

## Risk note for the README

The waiter runs `--until` and `--then` commands repeatedly, detached, outside
the agent's approval loop. Codex approves the `herdr-delay` invocation once,
with the command visible in argv, so it is transparent — but check commands
must be read-only and idempotent, and the README must say so plainly.
