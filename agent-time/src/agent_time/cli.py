from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path

from .core import connect, initialize, project_for, report_rows, totals, tracking_start
from . import uat
from .claude import ingest_all
from .claude_hooks import source_for


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
    p = argparse.ArgumentParser(prog='agent-time', description='Local Codex and Claude Code turn time by project; no prompting-time estimates.')
    p.add_argument('--codex-home', type=Path,
                   default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))))
    p.add_argument('--claude-home', type=Path,
                   default=Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))))
    p.add_argument('--t3-db', type=Path, default=Path(os.environ.get('T3CODE_HOME', str(Path.home() / '.t3'))) / 'userdata/statev2.sqlite')
    p.add_argument('--data-dir', type=Path,
                   default=Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'agent-time')
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Begin tracking future turns (safe to repeat).')
    listener = commands.add_parser('serve', help='Collect development activity on loopback.')
    listener.add_argument('--project', type=Path, required=True)
    listener.add_argument('--port', type=int, required=True)
    for name in ('report', 'turns'):
        sub = commands.add_parser(name, help='Import new records and show ' + name + '.')
        sub.add_argument('--period', choices=('today', 'week'))
        sub.add_argument('--from', dest='date_from', metavar='YYYY-MM-DD')
        sub.add_argument('--to', dest='date_to', metavar='YYYY-MM-DD')
        sub.add_argument('--project', type=Path)
        sub.add_argument('--source', choices=('codex', 'claude', 't3'), help='Filter by launcher (Codex, Claude, or T3).')
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    db = None
    try:
        if args.command == 'serve':
            if not 1024 <= args.port <= 65535:
                raise ValueError('Invalid port')
            with uat.make_server(args.data_dir.expanduser() / 'timing.sqlite3', str(args.project), args.port) as server:
                print(f'UAT collector on 127.0.0.1:{args.port}; idle threshold: 120s', flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
            return 0
        bounds = date_range(args) if args.command != 'init' else None
        db = connect(args.data_dir.expanduser() / 'timing.sqlite3')
        uat.setup(db)
        if args.command == 'init':
            since = initialize(db, time.time())
            print('Tracking turns starting from ' + datetime.fromtimestamp(since).astimezone().isoformat())
            return 0
        since = tracking_start(db)
        coverage = ingest_all(db, args.codex_home.expanduser(), args.claude_home.expanduser(), args.t3_db.expanduser())
        start, end = bounds
        selected = project_for(str(args.project)) if args.project else None
        rows = report_rows(db, start, end, selected)
        sources = {(r['session'], r['turn']): source_for(db, r) for r in rows}
        if args.source:
            rows = [r for r in rows if sources[(r['session'], r['turn'])]['launcher'] == args.source]
        print(f'{datetime.fromtimestamp(start):%Y-%m-%d} through '
              f'{datetime.fromtimestamp(end - 1):%Y-%m-%d} (local time)')
        print('Tracking since ' + datetime.fromtimestamp(since).astimezone().isoformat())
        if args.command == 'turns':
            print('Start (local)         Status       Counted time    Provider / launcher / Project / session / turn')
            for row in rows:
                seconds = row['clip_end'] - row['clip_start'] if row['clip_end'] is not None else None
                elapsed = duration(seconds) if seconds is not None else 'excluded'
                print(f"{datetime.fromtimestamp(row['start']):%Y-%m-%d %H:%M:%S}  "
                      f"{row['status']:<12} {elapsed:<15} "
                      f"{sources[(row['session'], row['turn'])]['provider']} / "
                      f"{sources[(row['session'], row['turn'])]['launcher']} / {safe(row['project'])} / {safe(row['session'])} / {safe(row['turn'])}")
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
            by_source = defaultdict(list)
            for row in rows:
                meta = sources[(row['session'], row['turn'])]
                by_source[(row['project'], meta['provider'], meta['launcher'])].append(row)
            print('\nAgent time by provider / launcher:')
            for (project, provider, launcher), items in sorted(by_source.items()):
                print(f'{duration(totals(items)[0]):>13}  {provider} / {launcher}  {safe(project)}')
            estimates, overall = uat.report(db, start, end, selected)
            print('\nEstimated UAT (120s idle tail; overlapping tabs counted once):')
            for project, seconds in sorted(estimates.items()):
                print(f'{duration(seconds):>13}  {safe(project)}')
            print(f'Total estimated UAT: {duration(overall)}')
        if not rows:
            print('No tracked agent turns in this range.')
        incomplete = db.execute("SELECT COUNT(*) FROM turns WHERE end IS NULL AND NOT EXISTS (SELECT 1 FROM turn_aliases a WHERE a.session=turns.session AND a.turn=turns.turn)").fetchone()[0]
        print(f"Coverage: {coverage['files']} session files; {coverage['excluded']} subagent files excluded; "
              f"{incomplete} unfinished turns across all dates (excluded from totals).")
        print(f"Claude: {coverage['claude_files']} session files; {coverage['claude_durations']} duration records; "
              f"{coverage['claude_excluded']} sidechain records excluded. Missing durations are not estimated.")
        print(f"T3: {coverage.get('t3_linked', 0)} native turns linked; {coverage.get('t3_imported', 0)} T3 intervals; "
              f"{coverage.get('t3_ambiguous', 0)} ambiguous matches; {coverage.get('t3_unsupported', 0)} unsupported.")
        problems = {k: coverage[k] for k in ('unreadable', 'partial', 'invalid', 'unsupported', 'untimed') if coverage[k]}
        if problems or not (coverage['files'] + coverage['claude_files']):
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
