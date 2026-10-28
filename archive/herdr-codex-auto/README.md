# herdr-codex-auto

`herdr-codex-auto` is an event-driven permission watcher for Codex panes inside
Herdr. It waits for Herdr to classify a Codex pane as `blocked`, verifies that
the visible screen contains Codex's exact command-permission prompt, and sends
`p`. It does not answer ordinary Codex questions.

There is no polling interval and no `sleep` loop. The script uses Herdr's agent
status wait, so it stays dormant until the pane changes state.

## Install

Put the executable on `PATH`, for example:

```bash
ln -s "$PWD/herdr-codex-auto/herdr-codex-auto" ~/.local/bin/herdr-codex-auto
```

The script requires Herdr 0.7.4 or newer, Bash, and `jq`. It must be launched
from a Herdr-managed pane.

## Watch existing panes

From a shell pane in the workspace:

```bash
herdr-codex-auto
```

This watches all Codex panes that are already running in that workspace and
continues until you stop it with `Ctrl-C`. To target just one pane:

```bash
herdr-codex-auto --pane w1:p2
```

The watcher deliberately does not adopt Codex panes created after it starts.
Restart it to take a fresh workspace snapshot. It never launches Codex and does
not modify or replace your `cdx` alias.

## Safety boundary

Pressing `p` accepts the command and may also tell Codex not to ask again for a
matching command prefix. This tool therefore has nearly the same trust boundary
as Codex's bypass mode. It only distinguishes command-permission prompts from
other blocked UI; it does not inspect or allowlist the command itself.

Use `--once` to inspect the currently selected panes and exit, which is also
useful for testing:

```bash
herdr-codex-auto --once
```
