# ThinkPad modes

## Live status

Server Mode.sh opens the live dashboard in tmux when run from an interactive
terminal. Open it independently with `~/"ThinkPad Modes/Live Status.sh"`.
It reuses the `thinkpad-status` tmux session. Press Ctrl+b then d to detach,
or q to close the view; neither stops services. Arrow keys scroll small screens.
From another computer, use:

```sh
ssh -t rshoshani@thinkpad.tail909cbd.ts.net python3 /home/rshoshani/projects/rsh-utilities/thinkpad-modes/dashboard.py --tmux
```

The dashboard refreshes local systemd state, archive heartbeat, memory, swap,
disk, load, temperature, battery and AC power every five seconds. It does not
query provider APIs or read report bodies or logs. Scheduled jobs now record
private, bounded metadata summaries (timestamps and outcome counts), including
failures. Latest results are shown; the last 20 summaries are retained under
`~/.local/state/thinkpad-modes/`. Earlier runs cannot be reconstructed. A mail
job's success means its process completed, not that a recipient read the mail.

## Mode controls

Run these as your normal user in a terminal. Sudo authentication is requested
only when system power settings need changing; repeated runs need no sudo.

```sh
~/"ThinkPad Modes/Server Mode.sh"
~/"ThinkPad Modes/Laptop Mode.sh"
```

Server mode keeps the machine awake, enables user-service persistence after
logout, and starts the error collector. After migration approval, the scheduled
worker runs at 04:00 and the morning report at 08:00, Israel time. No dev server
or Convex watcher is installed: fix worktrees use automated checks and record
browser acceptance steps for later integration/UAT on dev.

Laptop mode stops only these setup services, disables their boot target,
unmasks the four sleep/hibernate targets masked during setup, and restores the
original disabled linger setting. Installed tools and repositories remain.
Both mode switches refuse while a fix, report, or another mode switch is active.
Wait for the job to finish and run the script again; work is never force-killed.

Server mode persists across reboot, but disk encryption may require local
unlock first. Keep the laptop ventilated and connected to power in server mode.
Existing desktop power preferences are left untouched.

Timers do not catch up missed runs on entering server mode. The worker stops
claiming new bugs at 08:00; an already-running fix finishes normally. The morning
report waits if a fix is still active at 08:00. No automatic merge occurs.
Before a nightly run, the checkout must be on dev without tracked edits. It is
fast-forwarded to origin/dev; dependency changes trigger npm ci. Existing
untracked files remain. A divergence or tracked edit stops the run safely.

For status:

```sh
python3 ~/projects/rsh-utilities/thinkpad-modes/modes.py status
```

Schedules are initially held for migration testing. Only after the old PC's
nightly and mailer loops are stopped, arm future runs:

```sh
python3 ~/projects/rsh-utilities/thinkpad-modes/modes.py arm-schedules
```

This does not send mail or process a bug immediately. Raw process output is
discarded to avoid storing credentials or customer data in service journals.
The collector maintains its own protected error archive. Use the worker's
doctor command and systemd unit state for operational checks.

Development: `python3 -m unittest discover -s thinkpad-modes -p 'test_*.py'`.
