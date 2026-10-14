# herdr-delay

Lets a coding agent wait on something slow — CI, a preflight script, a deploy,
a health check — without burning a turn per poll.

An agent that re-runs `gh run list` every thirty seconds spends a turn per
poll, and each turn carries the whole conversation context. A thirty-minute
wait costs sixty turns to produce one bit of information. The waiting itself is
free; only the agent's participation in it is expensive.

`herdr-delay` returns in milliseconds, keeps waiting in a detached process
after the agent's turn ends, and prompts the agent exactly once — with an
answer.

## Usage

Run `./herdr-delay/herdr-delay` from the repository, or add a symlink to a
directory on `PATH`, for example:

```bash
ln -s /path/to/rsh-utilities/herdr-delay/herdr-delay ~/.local/bin/herdr-delay
```

```bash
herdr-delay 10m 'check the preflight run'          # plain delay
herdr-delay --in 10m --then 'npm run preflight'    # sleep, run once, report
herdr-delay --until 'curl -sf localhost:3000/health' --every 30s --deadline 10m
herdr-delay cancel
herdr-delay status
```

Durations accept `90`, `90s`, `10m`, `2h`, `1h30m`, and `slp`-style `MM:SS` /
`HH:MM:SS`. Defaults are `--every 30s` and `--deadline 30m`, with a 24h ceiling
on any wait.

Must run inside a Herdr-managed pane. It works out which pane to wake on its
own, so the agent never passes a pane ID. Outside Herdr it exits 3 rather than
falling back to a blocking sleep — a fallback that pins the turn defeats the
whole point.

### Which pane gets woken

`$HERDR_PANE_ID` is used when a live agent occupies that pane. It often is not.

Codex runs shell commands through a long-lived app-server process instead of a
child of the pane, so the variable it exposes belongs to whichever pane first
started that process — frequently one that has since been closed. When that
happens, `herdr-delay` identifies the caller by reading the agent kind off its
own process ancestry, then narrowing by working directory and `working` status,
and prints which rung it used:

```
[herdr-delay] resolved pane wN:pP by kind+cwd+working (HERDR_PANE_ID was wN:pB).
```

If that is still ambiguous it refuses and lists the candidates, because waking
the wrong agent drops a message into somebody else's conversation. Pass
`--pane <id>` to settle it.

### Two check modes

**`--until CMD` polls.** Exit 0 resolves; any nonzero means "not yet, retry
until the deadline". Fits health checks, marker files, and any status command
piped through `grep -q`:

```bash
herdr-delay --until 'gh run list -q ".[0].status" | grep -qx completed' \
            --every 60s --deadline 30m
```

**`--then CMD` runs once** after the delay. Any exit resolves: exit 0 is
`RESOLVED`, nonzero is `FAILED`. Fits builds, test suites, and blocking
watchers:

```bash
herdr-delay --in 5m --then 'npm run preflight'
herdr-delay --then 'gh run watch --exit-status'
```

`--deadline` applies to `--until` only. A `--then` command is the job itself.

### One waiter per pane

A second call from the same pane replaces the first and says so. An agent
reasons about one thing it is waiting for; a forgotten waiter surfacing three
prompts later with news about an abandoned task is a bug. Use `cancel` and
`status` to manage it, and `herdr-delay status` prints the waiter's log path.

## What the agent sees

On scheduling:

```
[herdr-delay] Waiter started (pid 41022) for pane wN:pK.
  mode     : poll every 1m00s until exit 0
  command  : gh run list -q '.[0].status' | grep -qx completed
  deadline : 30m00s (expires 03:44:07)

END YOUR TURN NOW. Do not poll, sleep, or re-run this command.
You will be prompted automatically with the result.
```

On wake, injected into the agent's composer:

```
[herdr-delay:a3f9] Your scheduled wait finished.
  waited for : gh run list -q '.[0].status' | grep -qx completed
  outcome    : RESOLVED (exit 0) after 24m12s
  polls      : 25

  --- last 20 lines of output ---
  completed
```

Outcomes are `RESOLVED`, `FAILED`, `TIMED_OUT`, and `ERROR`.

## Safety

**Check commands must be read-only and idempotent.** The waiter runs them
repeatedly, detached, outside the agent's approval loop. The invocation itself
is approved once with the command visible in argv, so nothing is hidden — but
`--until 'deploy.sh'` would deploy once a minute.

Delivery waits for the agent to become idle, checks its foreground process
and status, then uses `herdr pane run`. That command frames the text as a
bracketed paste when supported and appends Enter in the same API request.
`agent send` writes raw text and is unsuitable for this multiline submission.
The guarded submission never runs when the scheduled agent is missing or
replaced. Herdr does not offer an atomic conditional submit, so there is still
a small race between the final identity check and submission.

The waiter checks whether the same agent starts working after submission.
This is a status-based acknowledgement, not proof that it read the callback.
Text visible in the composer is not evidence of submission. An unconfirmed submission
raises a notification; it is not pasted again, to avoid duplicate callbacks.

A wake is not delivered to a `blocked` agent. Prompting one does not steer it —
the Enter answers the overlay's highlighted default and the wake text is
discarded. The waiter uses `agent wait --status idle` before trying delivery;
Herdr treats `done` as a UI attention state, not a CLI wait target.
The waiter accepts both the immediate agent result and the status-change event
returned when Herdr actually waits for an idle transition.

## For AGENTS.md / CLAUDE.md

Paste this into projects where agents should use it:

```markdown
## Waiting on slow things

Never poll a long-running check in a loop — it costs a turn per poll. Use
`herdr-delay`, which returns immediately and prompts you once with the result:

- Fixed wait:   `herdr-delay 10m 'reason'`
- Run it later: `herdr-delay --in 5m --then 'npm run preflight'`
- Poll for it:  `herdr-delay --until 'CMD' --every 60s --deadline 30m`
  (`--until` resolves when CMD exits 0; pipe status output through `grep -q`.)

After running it, END YOUR TURN. Do not sleep, poll, or re-run the check.
You will be prompted automatically when it resolves.
```

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Unit tests use a fake clock and a fake command runner. Delivery is tested
against a recording `herdr` stub placed on `PATH`; no test contacts the live
Herdr socket.
