from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path

from .core import connect, ingest, initialize, project_for, report_rows, totals, tracking_start


def date_range(args: argparse.Namespace, today: date | None = None) -> tuple[float, float]:
    today = today or date.today()
    if args.date_from or args.date_to:
        if not args.date_from or not args.date_to or args.period:
            raise ValueError('Use --from and --to together, without --period.')
        first, last = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
        if last < first:
            raise ValueError('--to must be on or after --from.')
    else:
        first = today - timedelta(days=today.weekday()) if args.period == 'week' else today
        last = first + timedelta(days=6) if args.period == 'week' else today
    # Convert each local midnight independently, including DST transitions.
    return (datetime.combine(first, day_time()).timestamp(),
            datetime.combine(last + timedelta(days=1), day_time()).timestamp())


def duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours}h {minutes:02}m {seconds:02}s'


def safe(text: str) -> str:
    return ''.join(c if c.isprintable() else '?' for c in text)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='agent-time', description='Local Codex turn time by project; no prompting-time estimates.')
    p.add_argument('--codex-home', type=Path,
                   default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))))
    p.add_argument('--data-dir', type=Path,
                   default=Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'agent-time')
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Begin tracking future turns (safe to repeat).')
    for name in ('report', 'turns'):
        sub = commands.add_parser(name, help='Import new records and show ' + name + '.')
        sub.add_argument('--period', choices=('today', 'week'))
        sub.add_argument('--from', dest='date_from', metavar='YYYY-MM-DD')
        sub.add_argument('--to', dest='date_to', metavar='YYYY-MM-DD')
        sub.add_argument('--project', type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    db = None
    try:
        bounds = date_range(args) if args.command != 'init' else None
        db = connect(args.data_dir.expanduser() / 'timing.sqlite3')
        if args.command == 'init':
            since = initialize(db, time.time())
            print('Tracking turns starting from ' + datetime.fromtimestamp(since).astimezone().isoformat())
            return 0
        since = tracking_start(db)
        coverage = ingest(db, args.codex_home.expanduser())
        start, end = bounds
        selected = project_for(str(args.project)) if args.project else None
        rows = report_rows(db, start, end, selected)
        print(f'{datetime.fromtimestamp(start):%Y-%m-%d} through '
              f'{datetime.fromtimestamp(end - 1):%Y-%m-%d} (local time)')
        print('Tracking since ' + datetime.fromtimestamp(since).astimezone().isoformat())
        if args.command == 'turns':
            print('Start (local)         Status       Counted time    Project / session / turn')
            for row in rows:
                seconds = row['clip_end'] - row['clip_start'] if row['clip_end'] is not None else None
                elapsed = duration(seconds) if seconds is not None else 'excluded'
                print(f"{datetime.fromtimestamp(row['start']):%Y-%m-%d %H:%M:%S}  "
                      f"{row['status']:<12} {elapsed:<15} "
                      f"{safe(row['project'])} / {safe(row['session'])} / {safe(row['turn'])}")
        else:
            grouped = defaultdict(list)
            for row in rows:
                grouped[row['project']].append(row)
            print('Done  Failed  Interrupted  Incomplete     Agent time     Elapsed active  Project')
            for project, items in sorted(grouped.items()):
                counts = Counter(r['status'] for r in items)
                agent, active = totals(items)
                print(f"{counts['completed']:>4}  {counts['failed']:>6}  {counts['interrupted']:>11}  "
                      f"{counts['incomplete']:>10}  {duration(agent):>13}  {duration(active):>17}  {safe(project)}")
            agent, active = totals(rows)
            print(f'Total agent time: {duration(agent)}; elapsed active: {duration(active)}')
        if not rows:
            print('No tracked turns in this range.')
        incomplete = db.execute("SELECT COUNT(*) FROM turns WHERE end IS NULL").fetchone()[0]
        print(f"Coverage: {coverage['files']} session files; {coverage['excluded']} subagent files excluded; "
              f"{incomplete} unfinished turns across all dates (excluded from totals).")
        problems = {k: coverage[k] for k in ('unreadable', 'partial', 'invalid', 'unsupported', 'untimed') if coverage[k]}
        if problems or not coverage['files']:
            print('Coverage warning: ' + (', '.join(f'{k}={v}' for k, v in problems.items())
                                         or 'no session files found') + '.')
        print('Turn duration includes tools/tests; time between turns and prompting time are excluded.')
        return 0
    except (ValueError, OverflowError, RuntimeError, OSError, sqlite3.Error):
        # Do not echo untrusted records, paths from errors, or provider configuration.
        print('agent-time: unable to complete request. Check date options, run init, and verify local file access.',
              file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()
