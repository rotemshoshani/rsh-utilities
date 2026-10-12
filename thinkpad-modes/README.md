# ThinkPad modes

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
