from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, device INTEGER, inode INTEGER, offset INTEGER,
    mtime INTEGER, session TEXT, project TEXT, excluded INTEGER,
    invalid INTEGER DEFAULT 0, unsupported INTEGER DEFAULT 0,
    activity INTEGER DEFAULT 0, boundaries INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS turn_sources (
    session TEXT, turn TEXT, provider TEXT NOT NULL, launcher TEXT NOT NULL,
    timing TEXT NOT NULL, PRIMARY KEY(session,turn)
);
CREATE TABLE IF NOT EXISTS turn_aliases (
    session TEXT, turn TEXT, canonical_session TEXT, canonical_turn TEXT, reason TEXT,
    PRIMARY KEY(session,turn)
);
CREATE TABLE IF NOT EXISTS turns (
    session TEXT, turn TEXT, project TEXT NOT NULL,
    start REAL, end REAL, status TEXT NOT NULL DEFAULT 'incomplete',
    PRIMARY KEY (session, turn)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Create private storage before sqlite opens it; never touch Codex files.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def initialize(db: sqlite3.Connection, now: float) -> float:
    with db:
        db.execute("INSERT OR IGNORE INTO settings VALUES ('since', ?)", (str(now),))
    return tracking_start(db)


def tracking_start(db: sqlite3.Connection) -> float:
    row = db.execute("SELECT value FROM settings WHERE key='since'").fetchone()
    if row is None:
        raise ValueError("Run agent-time init before requesting a report.")
    return float(row[0])


def timestamp(value: object) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            result = float(value)
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                return None
            result = dt.timestamp()
        else:
            return None
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def project_for(cwd: str) -> str:
    path = Path(cwd).expanduser().resolve()
    try:
        result = subprocess.run(
            ['git', '-C', str(path), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5,
            check=False,
        )
        if result.returncode == 0:
            common = Path(result.stdout.strip()).resolve()
            return str(common.parent if common.name == '.git' else common)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return str(path)


def _metadata(payload: dict, projects: dict[str, str]) -> tuple[str, str, bool] | None:
    sid = payload.get('session_id') or payload.get('id')
    cwd = payload.get('cwd')
    if not isinstance(sid, str) or not sid or not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return None
    source = payload.get('source')
    excluded = payload.get('thread_source', 'user') not in ('user', None, '')
    excluded |= isinstance(source, dict) and 'subagent' in source
    if cwd not in projects:
        try:
            projects[cwd] = project_for(cwd)
        except (ValueError, OSError, RuntimeError):
            return None
    return sid, projects[cwd], excluded


def native_source(db: sqlite3.Connection, session: str, turn: str, provider: str) -> None:
    # A late native event refines the measurement, without erasing T3 attribution.
    db.execute("""INSERT INTO turn_sources VALUES (?,?,?,?, 'transcript')
        ON CONFLICT(session,turn) DO UPDATE SET timing='transcript'""",
               (session, turn, provider, provider))


def _event(db: sqlite3.Connection, record: dict, session: str, project: str,
           since: float) -> bool:
    """Return False for unusable timing, never infer endpoints from message text."""
    if record.get('type') != 'event_msg':
        return True
    payload = record.get('payload')
    if not isinstance(payload, dict):
        return False
    kind = payload.get('type')
    if not isinstance(kind, str):
        return False
    boundary = timestamp(record.get('timestamp'))
    if boundary is not None and boundary < since:
        return True
    statuses = {'task_complete': 'completed', 'turn_aborted': 'interrupted',
                'task_failed': 'failed', 'turn_failed': 'failed'}
    if kind != 'task_started' and kind not in statuses:
        # An error event need not end the turn (the agent may recover).
        return True
    tid = payload.get('turn_id')
    if not isinstance(tid, str) or not tid:
        return False
    if kind == 'task_started':
        start = boundary if boundary is not None else timestamp(payload.get('started_at'))
        if start is None:
            return False
        if start >= since:
            previous = db.execute('SELECT end FROM turns WHERE session=? AND turn=?', (session, tid)).fetchone()
            if previous and previous['end'] is not None and start > previous['end']:
                return False
            db.execute('''INSERT INTO turns(session,turn,project,start) VALUES (?,?,?,?)
                ON CONFLICT(session,turn) DO UPDATE SET start=excluded.start, project=excluded.project''',
                       (session, tid, project, start))
            native_source(db, session, tid, 'codex')
        return True
    old = db.execute('SELECT start FROM turns WHERE session=? AND turn=?', (session, tid)).fetchone()
    start = old['start'] if old else timestamp(payload.get('started_at'))
    end = boundary if boundary is not None else timestamp(payload.get('completed_at'))
    if start is not None and start < since:
        return True
    if start is None or end is None or end < start:
        return False
    status = statuses[kind]
    if kind == 'task_complete' and payload.get('status') == 'failed':
        status = 'failed'
    db.execute('''INSERT INTO turns(session,turn,project,start,end,status) VALUES (?,?,?,?,?,?)
        ON CONFLICT(session,turn) DO UPDATE SET end=excluded.end, status=excluded.status''',
               (session, tid, project, start, end, status))
    native_source(db, session, tid, 'codex')
    return True


def ingest(db: sqlite3.Connection, codex_home: Path) -> dict[str, int]:
    since = tracking_start(db)
    projects: dict[str, str] = {}
    counts = {'unreadable': 0, 'partial': 0, 'invalid': 0, 'unsupported': 0,
              'files': 0, 'excluded': 0, 'untimed': 0}
    for directory in ('sessions', 'archived_sessions'):
        root = codex_home / directory
        if not root.exists():
            continue
        for path in root.rglob('*.jsonl'):
            counts['files'] += 1
            try:
                with path.open('rb') as stream, db:
                    stat = os.fstat(stream.fileno())
                    row = db.execute('SELECT * FROM files WHERE path=?', (str(path),)).fetchone()
                    unchanged_prefix = (row is not None and row['device'] == stat.st_dev
                                        and row['inode'] == stat.st_ino and stat.st_size >= row['offset']
                                        and (stat.st_size > row['offset'] or stat.st_mtime_ns == row['mtime']))
                    if unchanged_prefix:
                        offset = row['offset']
                        session, project, excluded = row['session'], row['project'], row['excluded']
                        invalid, unsupported = row['invalid'], row['unsupported']
                        activity, boundaries = row['activity'], row['boundaries']
                        stream.seek(offset)
                    else:
                        offset = 0
                        session = project = None
                        excluded = False
                        invalid = unsupported = 0
                        activity = boundaries = 0
                    while True:
                        line = stream.readline()
                        if not line:
                            break
                        if not line.endswith(b'\n'):
                            counts['partial'] += 1
                            break
                        offset = stream.tell()
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            invalid += 1
                            continue
                        if not isinstance(record, dict):
                            invalid += 1
                            continue
                        if record.get('type') == 'session_meta':
                            payload = record.get('payload')
                            meta = _metadata(payload, projects) if isinstance(payload, dict) else None
                            if meta:
                                session, project, excluded = meta
                                existing = db.execute('SELECT project FROM turns WHERE session=? LIMIT 1',
                                                      (session,)).fetchone()
                                if existing:
                                    project = existing['project']
                            else:
                                unsupported += 1
                                session = project = None
                            continue
                        if excluded:
                            continue
                        if session is None or project is None:
                            unsupported += 1
                            continue
                        event_time = timestamp(record.get('timestamp'))
                        payload = record.get('payload')
                        if event_time is not None and event_time >= since and isinstance(payload, dict):
                            # Background token/status events can belong to a turn that
                            # started before initialization. Only user input is evidence
                            # of new work when no supported boundaries were recorded.
                            activity += ((record.get('type') == 'event_msg' and payload.get('type') == 'user_message')
                                         or (record.get('type') == 'response_item'
                                             and payload.get('type') == 'message' and payload.get('role') == 'user'))
                            boundaries += (record.get('type') == 'event_msg' and payload.get('type') in (
                                'task_started', 'task_complete', 'turn_aborted', 'task_failed', 'turn_failed'))
                        if not _event(db, record, session, project, since):
                            unsupported += 1
                    db.execute('''INSERT OR REPLACE INTO files
                        (path,device,inode,offset,mtime,session,project,excluded,invalid,unsupported,activity,boundaries)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                               (str(path), stat.st_dev, stat.st_ino, offset, stat.st_mtime_ns,
                                session, project, int(excluded), invalid, unsupported, activity, boundaries))
                    counts['invalid'] += invalid
                    counts['unsupported'] += unsupported
                    counts['excluded'] += bool(excluded)
                    counts['untimed'] += bool(activity and not boundaries and not excluded)
            except OSError:
                counts['unreadable'] += 1
    return counts


def union_seconds(intervals: list[tuple[float, float]]) -> float:
    end = float('-inf')
    total = 0.0
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end))
        end = max(end, stop)
    return total


def report_rows(db: sqlite3.Connection, start: float, end: float,
                project: str | None = None) -> list[dict]:
    rows = db.execute('''SELECT * FROM turns WHERE start < ?
        AND NOT EXISTS (SELECT 1 FROM turn_aliases a WHERE a.session=turns.session AND a.turn=turns.turn)
        AND ((end IS NOT NULL AND end > ?) OR (end IS NULL AND start >= ?))
        ORDER BY start,session,turn''', (end, start, start))
    result = []
    for row in rows:
        if project is not None and row['project'] != project:
            continue
        item = dict(row)
        item['clip_start'] = max(start, row['start'])
        item['clip_end'] = min(end, row['end']) if row['end'] is not None else None
        result.append(item)
    return result


def totals(rows: list[dict]) -> tuple[float, float]:
    intervals = [(r['clip_start'], r['clip_end']) for r in rows if r['clip_end'] is not None]
    return sum(stop - start for start, stop in intervals), union_seconds(intervals)
