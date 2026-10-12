#!/usr/bin/env python3
"""ThinkPad CRM services: explicit modes and bounded scheduled work."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

HOME = Path.home()
STATE = HOME / '.local/state/thinkpad-modes'
REPO = HOME / 'projects/meniv/crm'
TARGET = 'thinkpad-server.target'
TIMERS = ['thinkpad-bug-worker.timer', 'thinkpad-morning-report.timer']
JOBS = ['thinkpad-bug-worker.service', 'thinkpad-morning-report.service']
SLEEP_TARGETS = ['sleep.target', 'suspend.target', 'hibernate.target', 'hybrid-sleep.target']


def run(args, capture=False):
    result = subprocess.run(args, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, text=True)
    if result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} failed (exit {result.returncode}); details suppressed')
    return result.stdout if capture else ''


def ctl(*args):
    return run(['systemctl', '--user', *args], capture=True)


def lock(wait=False):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = (STATE / 'operation.lock').open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        handle.close()
        raise RuntimeError('A fix, report, or mode change is active. Nothing was stopped; retry after it finishes.')
    return handle


def mode(name):
    with lock():
        # Jobs also acquire this lock, closing the admission race with timers.
        # Refuse unexpected externally-started services rather than killing them.
        for unit in JOBS:
            active = ctl('show', unit, '--property=ActiveState', '--value').strip()
            if active in ('active', 'activating', 'deactivating'):
                raise RuntimeError(f'{unit} is busy; no changes made.')
        run(['sudo', '-v'])
        if name == 'server':
            run(['sudo', 'loginctl', 'enable-linger', 'rshoshani'])
            run(['sudo', 'systemctl', 'mask', *SLEEP_TARGETS])
            (STATE / 'server-enabled').touch(mode=0o600)
            ctl('enable', TARGET)
            try:
                ctl('start', TARGET)
                ctl('start', 'thinkpad-error-collector.service', *TIMERS)
            except RuntimeError:
                (STATE / 'server-enabled').unlink(missing_ok=True)
                ctl('stop', TARGET)
                ctl('disable', TARGET)
                raise
            armed = (STATE / 'schedules-armed').exists()
            print('Server mode: collector running; sleep disabled; survives logout/reboot after disk unlock.')
            print('Overnight fix/report schedules: ' + ('armed' if armed else 'HELD for migration verification.'))
        else:
            # Stop timers first. The shared lock prevents a new job from entering.
            ctl('stop', *TIMERS)
            (STATE / 'server-enabled').unlink(missing_ok=True)
            ctl('stop', TARGET)
            ctl('disable', TARGET)
            run(['sudo', 'systemctl', 'unmask', *SLEEP_TARGETS])
            run(['sudo', 'loginctl', 'disable-linger', 'rshoshani'])
            print('Laptop mode: setup services stopped; normal sleep and original logout behavior restored.')


def in_window(now=None):
    now = now or datetime.datetime.now(ZoneInfo('Asia/Jerusalem'))
    return 4 <= now.hour < 8


def update_checkout():
    if run(['git', '-C', str(REPO), 'branch', '--show-current'], capture=True).strip() != 'dev':
        raise RuntimeError('CRM checkout is not on dev; left untouched.')
    run(['git', '-C', str(REPO), 'diff', '--quiet'])
    run(['git', '-C', str(REPO), 'diff', '--cached', '--quiet'])
    run(['git', '-C', str(REPO), 'fetch', 'origin'])
    run(['git', '-C', str(REPO), 'merge', '--ff-only', 'origin/dev'])
    manifest = (REPO / 'package-lock.json').read_bytes()
    installed = STATE / 'installed-package-lock.json'
    if not installed.exists() or installed.read_bytes() != manifest:
        run(['npm', '--prefix', str(REPO), 'ci', '--no-audit', '--no-fund'])
        installed.write_bytes(manifest)


def job(name):
    # The 08:00 report waits for an overrunning fix rather than being lost.
    # This is an OS lock wait, not a process-status polling loop.
    with lock(wait=name == 'report'):
        if not all((STATE / p).exists() for p in ('server-enabled', 'schedules-armed')):
            print('Scheduled work held: server mode and migration approval are required.')
            return
        if name == 'worker':
            if not in_window():
                print('Outside 04:00–08:00 Israel time; no bug claimed.')
                return
            update_checkout()
            os.chdir(REPO)
            # One real queue operation per iteration, not a status polling loop.
            # Finish the current fix past 08:00, but admit no further report.
            for _ in range(200):
                if not in_window():
                    break
                output = run(['node', 'scripts/bug-report-worker/index.mjs', 'run', '--limit', '1'], capture=True)
                if any('"queueEmpty":true' in line.replace(' ', '') for line in output.splitlines()):
                    break
            print('Nightly worker finished; raw report and model output withheld.')
        else:
            os.chdir(REPO)
            run(['node', 'scripts/bug-report-worker/morning-report.mjs'])
            print('Morning report completed; output withheld.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['server', 'laptop', 'status', 'worker', 'report', 'arm-schedules'])
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action in ('server', 'laptop'):
            mode(args.action)
        elif args.action in ('worker', 'report'):
            job(args.action)
        elif args.action == 'arm-schedules':
            with lock():
                (STATE / 'schedules-armed').touch(mode=0o600)
            print('Schedules armed for their next calendar occurrence; no job started now.')
        else:
            print('Server mode:', (STATE / 'server-enabled').exists())
            print('Schedules armed:', (STATE / 'schedules-armed').exists())
            for unit in [TARGET, 'thinkpad-error-collector.service', *TIMERS, *JOBS]:
                print(unit, ctl('show', unit, '--property=ActiveState', '--value').strip())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
