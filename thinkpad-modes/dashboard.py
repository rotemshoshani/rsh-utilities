#!/usr/bin/env python3
"""Live local operational metadata. No provider calls, logs or report content."""
import argparse
from contextlib import closing
import curses
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time

HOME = Path.home()
STATE = HOME / '.local/state/thinkpad-modes'
UNITS = ['thinkpad-server.target', 'thinkpad-error-collector.service',
         'thinkpad-bug-worker.service', 'thinkpad-morning-report.service',
         'thinkpad-bug-worker.timer', 'thinkpad-morning-report.timer']
PROPERTIES = ['Id', 'ActiveState', 'SubState', 'Result', 'NRestarts',
              'NextElapseUSecRealtime', 'ExecMainStatus']


def properties(text):
    result = {}
    for block in text.strip().split('\n\n'):
        item = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        if item.get('Id') in UNITS:
            result[item['Id']] = {k: v for k, v in item.items() if k in PROPERTIES}
    return result


def units():
    try:
        result = subprocess.run(['systemctl', '--user', 'show', *UNITS,
                                 '--property=' + ','.join(PROPERTIES)],
                                capture_output=True, text=True, timeout=3)
        return properties(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired):
        return {}


def archive_health():
    path = HOME / '.local/state/meniv-bug-worker/error-archive/errors.sqlite'
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
            # Only collector health counters: never read the events table.
            row = db.execute('SELECT lastPageAt, running, reconnects FROM health '
                             'WHERE deployment=?', ('tremendous-avocet-15',)).fetchone()
        if row:
            return {'age': max(0, time.time() - (row[0] or 0) / 1000),
                    'running': bool(row[1]), 'reconnects': int(row[2])}
    except (sqlite3.Error, OSError, ValueError):
        pass
    return None


def record(name):
    try:
        data = json.loads((STATE / f'{name}-latest.json').read_text())
        allowed = ('pr-open', 'pr-recovered', 'needs-clarification', 'failed-retryable', 'other')
        return {'status': data.get('status') if data.get('status') in ('running', 'success', 'issues', 'failed') else 'unknown',
                'started': float(data.get('started', 0)), 'finished': float(data.get('finished', 0)),
                'counts': {k: int(data.get('counts', {}).get(k, 0)) for k in allowed}}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def laptop():
    info = {}
    try:
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
                memory[key] = int(value.split()[0]) / 1024**2
        info['ram'] = (memory['MemTotal'] - memory['MemAvailable'], memory['MemTotal'])
        info['swap'] = (memory['SwapTotal'] - memory['SwapFree'], memory['SwapTotal'])
        info['uptime'] = float(Path('/proc/uptime').read_text().split()[0])
    except (OSError, ValueError, KeyError):
        pass
    info['load'] = os.getloadavg()[0]
    info['cores'] = os.cpu_count() or 1
    disk = shutil.disk_usage(HOME)
    info['disk'] = (disk.used / 1024**3, disk.total / 1024**3)
    temps = []
    for path in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
        try:
            temp = int(path.read_text()) / 1000
            if 0 < temp < 150:
                temps.append(temp)
        except (OSError, ValueError):
            pass
    info['temperature'] = max(temps) if temps else None
    info['batteries'] = []
    for path in Path('/sys/class/power_supply').glob('BAT*'):
        try:
            capacity = int((path / 'capacity').read_text())
            status = (path / 'status').read_text().strip()
            status = status if status in ('Charging', 'Discharging', 'Full', 'Not charging', 'Unknown') else 'Unknown'
            info['batteries'].append((path.name, capacity, status))
        except (OSError, ValueError):
            pass
    info['ac'] = None
    for path in Path('/sys/class/power_supply').glob('*/online'):
        try:
            if (path.parent / 'type').read_text().strip() == 'Mains':
                info['ac'] = path.read_text().strip() == '1'
        except OSError:
            pass
    return info


def collect():
    return {'server': (STATE / 'server-enabled').exists(),
            'armed': (STATE / 'schedules-armed').exists(),
            'units': units(), 'archive': archive_health(), 'laptop': laptop(),
            'worker': record('worker'), 'report': record('report')}


def time_label(stamp):
    return datetime.datetime.fromtimestamp(stamp).strftime('%d %b %H:%M') if stamp else '--'


def render(data):
    lines = []
    def add(text='', tone=0):
        lines.append((text, tone))
    add('THINKPAD  /  SERVER HEALTH', 4)
    add(datetime.datetime.now().strftime('%a %d %b %Y  %H:%M:%S %Z') + '   |   refresh 5s')
    add('Mode: ' + ('SERVER' if data['server'] else 'LAPTOP') + '    Schedules: ' + ('ARMED' if data['armed'] else 'HELD'))
    add()
    add('SERVICES', 4)
    u = data['units']
    collector = u.get('thinkpad-error-collector.service', {})
    health = data['archive']
    good = collector.get('ActiveState') == 'active' and health and health['running'] and health['age'] < 90
    label = 'HEALTHY' if good else ('STOPPED' if not data['server'] else 'CHECK')
    age = f"last heartbeat {health['age']:.0f}s ago, reconnects {health['reconnects']}" if health else 'no archive heartbeat'
    add(f'  Collector    {label:10} {age}', 1 if good else (2 if data['server'] else 0))
    for name, label in [('bug-worker', 'Bug worker'), ('morning-report', 'Morning mail')]:
        item = u.get(f'thinkpad-{name}.service', {})
        timer = u.get(f'thinkpad-{name}.timer', {})
        active = item.get('ActiveState', 'unknown')
        failed = active == 'failed' or item.get('Result', 'success') != 'success'
        status = 'FAILED' if failed else ('RUNNING' if active in ('active', 'activating') else 'WAITING')
        if not data['server']:
            status = 'STOPPED'
        elif not data['armed'] and status == 'WAITING':
            status = 'HELD'
        elif timer.get('ActiveState') != 'active' and status == 'WAITING':
            status = 'NO TIMER'
        add(f'  {label:12} {status:10} next {timer.get("NextElapseUSecRealtime") or "--"}', 3 if failed else 0)
    if not u:
        add('  Systemd status unavailable', 3)
    add()
    add('LATEST RUNS  /  SUMMARY ONLY', 4)
    for key, label in [('worker', 'Bug worker'), ('report', 'Morning mail')]:
        item = data[key]
        if not item:
            add(f'  {label}: no recorded run yet')
            continue
        duration = max(0, (item['finished'] or time.time()) - item['started'])
        status = item['status']
        unit = u.get('thinkpad-bug-worker.service' if key == 'worker' else 'thinkpad-morning-report.service', {})
        if status == 'running' and unit.get('ActiveState') not in ('active', 'activating'):
            status = 'interrupted / check service'
        add(f'  {label}: {status.upper()}  {time_label(item["started"])}  {duration / 60:.1f} min', 3 if status.startswith(('failed', 'interrupted')) else 0)
        if key == 'worker':
            c = item['counts']
            add(f'    Processed {sum(c.values())}  PRs {c["pr-open"]}  recovered {c["pr-recovered"]}  clarification {c["needs-clarification"]}  failed {c["failed-retryable"]}  other {c["other"]}')
    add()
    add('LAPTOP', 4)
    hw = data['laptop']
    add(f'  Load {hw["load"]:.2f} / {hw["cores"]} CPUs    Uptime {hw.get("uptime", 0) / 3600:.1f}h', 2 if hw['load'] > hw['cores'] else 0)
    for key, label in [('ram', 'RAM'), ('swap', 'Swap'), ('disk', 'Disk')]:
        if key in hw:
            used, total = hw[key]
            add(f'  {label:5} {used:.1f} / {total:.1f} GiB', 2 if total and used / total > .9 else 0)
    if hw['temperature'] is not None:
        add(f'  Temperature {hw["temperature"]:.0f} C', 2 if hw['temperature'] >= 85 else 0)
    add('  Power: ' + ('AC connected' if hw['ac'] else 'BATTERY' if hw['ac'] is False else 'unknown'), 2 if hw['ac'] is False else 0)
    for name, capacity, status in hw['batteries']:
        add(f'  {name}: {capacity}%  {status}', 2 if capacity < 20 else 0)
    add()
    add('q close view  |  r refresh  |  arrows scroll  |  tmux: Ctrl+b, d detach')
    add('Closing this view does not stop services. Laptop Mode.sh stops them safely.')
    return lines


def screen(stdscr):
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for i, color in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_CYAN), 1):
            curses.init_pair(i, color, -1)
    stdscr.timeout(5000)
    offset = 0
    while True:
        lines = render(collect())
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        offset = min(offset, max(0, len(lines) - height))
        for y, (text, tone) in enumerate(lines[offset:offset + height]):
            try:
                stdscr.addnstr(y, 0, text, max(0, width - 1), curses.color_pair(tone) if curses.has_colors() else 0)
            except curses.error:
                pass
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord('q'), 27):
            return
        if key == curses.KEY_DOWN:
            offset += 1
        elif key == curses.KEY_UP:
            offset = max(0, offset - 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--tmux', action='store_true')
    args = parser.parse_args()
    if args.once or not sys.stdout.isatty():
        print('\n'.join(text for text, _ in render(collect())))
    elif args.tmux and not os.environ.get('TMUX') and shutil.which('tmux'):
        command = shlex.join([sys.executable, str(Path(__file__).resolve())])
        os.execvp('tmux', ['tmux', 'new-session', '-A', '-s', 'thinkpad-status', command])
    else:
        curses.wrapper(screen)


if __name__ == '__main__':
    main()
