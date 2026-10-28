"""Import Claude Code duration metadata, never infer time from message gaps."""
from __future__ import annotations
import json
import os
import sqlite3
from pathlib import Path
from .core import ingest, project_for, timestamp, tracking_start
from .claude_hooks import setup

SCHEMA = '''CREATE TABLE IF NOT EXISTS claude_files (
 path TEXT PRIMARY KEY, device INTEGER, inode INTEGER, offset INTEGER,
 mtime INTEGER, invalid INTEGER, unsupported INTEGER, activity INTEGER,
 boundaries INTEGER, excluded INTEGER);'''


def ingest_claude(db: sqlite3.Connection, home: Path) -> dict[str, int]:
    db.executescript(SCHEMA)
    setup(db)
    since = tracking_start(db)
    counts = dict.fromkeys(('files', 'unreadable', 'partial', 'invalid', 'unsupported',
                           'activity', 'boundaries', 'excluded', 'untimed'), 0)
    projects: dict[str, str] = {}
    # Subagents are nested under session/subagents, not top-level session logs.
    for path in (home / 'projects').glob('*/*.jsonl'):
        counts['files'] += 1
        try:
            with path.open('rb') as stream, db:
                stat = os.fstat(stream.fileno())
                old = db.execute('SELECT * FROM claude_files WHERE path=?', (str(path),)).fetchone()
                resume = (old is not None and old['device'] == stat.st_dev and old['inode'] == stat.st_ino
                          and stat.st_size >= old['offset'] and
                          (stat.st_size > old['offset'] or stat.st_mtime_ns == old['mtime']))
                offset = old['offset'] if resume else 0
                values = {key: old[key] if resume else 0 for key in
                          ('invalid', 'unsupported', 'activity', 'boundaries', 'excluded')}
                stream.seek(offset)
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b'\n'):
                        counts['partial'] += 1
                        break
                    offset = stream.tell()
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        values['invalid'] += 1
                        continue
                    if not isinstance(row, dict):
                        values['invalid'] += 1
                        continue
                    if row.get('isSidechain') is True:
                        values['excluded'] += 1
                        continue
                    end = timestamp(row.get('timestamp'))
                    if end is None or end < since:
                        continue
                    if row.get('type') == 'user' and not row.get('isMeta'):
                        message = row.get('message')
                        content = message.get('content') if isinstance(message, dict) else None
                        values['activity'] += (isinstance(content, str) or
                            (isinstance(content, list) and any(isinstance(x, dict) and x.get('type') == 'text'
                                                               for x in content)))
                    if row.get('type') != 'system' or row.get('subtype') != 'turn_duration':
                        continue
                    ms, session, turn, cwd = (row.get(k) for k in ('durationMs', 'sessionId', 'uuid', 'cwd'))
                    if (type(ms) not in (int, float) or timestamp(ms) is None
                            or row.get('isSidechain') is not False
                            or any(not isinstance(x, str) or not x or len(x) > 256 for x in (session, turn))
                            or not isinstance(cwd, str) or not Path(cwd).is_absolute()):
                        values['unsupported'] += 1
                        continue
                    # Duration is authoritative; interval placement is reconstructed.
                    start = end - ms / 1000
                    if start < since:
                        continue
                    sid = 'claude:' + session
                    owner = db.execute('SELECT since FROM claude_hook_sessions WHERE session=?', (sid,)).fetchone()
                    if owner and end >= owner['since']:
                        # Hooks own this session from activation onward, including missing ends.
                        # Never count a transcript fallback on top of the live lifecycle record.
                        continue
                    previous = db.execute('SELECT project FROM turns WHERE session=? LIMIT 1', (sid,)).fetchone()
                    if previous:
                        project = previous['project']
                    else:
                        if cwd not in projects:
                            projects[cwd] = project_for(cwd)
                        project = projects[cwd]
                    db.execute('''INSERT OR IGNORE INTO turns(session,turn,project,start,end,status)
                                  VALUES (?,?,?,?,?,'completed')''', (sid, turn, project, start, end))
                    values['boundaries'] += 1
                db.execute('INSERT OR REPLACE INTO claude_files VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (str(path), stat.st_dev, stat.st_ino, offset, stat.st_mtime_ns,
                            *(values[k] for k in ('invalid', 'unsupported', 'activity', 'boundaries', 'excluded'))))
                for key in values:
                    counts[key] += values[key]
                counts['untimed'] += bool(values['activity'] and not values['boundaries'])
        except (OSError, ValueError, RuntimeError):
            counts['unreadable'] += 1
    return counts


def ingest_all(db: sqlite3.Connection, codex_home: Path, claude_home: Path) -> dict[str, int]:
    counts = ingest(db, codex_home)
    other = ingest_claude(db, claude_home)
    counts['claude_files'] = other['files']
    counts['claude_durations'] = other['boundaries']
    counts['claude_excluded'] = other['excluded']
    for key in ('unreadable', 'partial', 'invalid', 'unsupported', 'untimed'):
        counts[key] += other[key]
    return counts
