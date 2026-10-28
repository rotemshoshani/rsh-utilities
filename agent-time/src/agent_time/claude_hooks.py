"""Explicit Claude lifecycle timing. Payload text is never persisted."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from .core import connect, project_for, timestamp, tracking_start

SCHEMA = '''
CREATE TABLE IF NOT EXISTS claude_hook_sessions (
 session TEXT PRIMARY KEY, since REAL NOT NULL, active TEXT, last_turn TEXT);
CREATE TABLE IF NOT EXISTS turn_sources (
 session TEXT, turn TEXT, provider TEXT NOT NULL, launcher TEXT NOT NULL,
 timing TEXT NOT NULL, PRIMARY KEY(session,turn));
'''


def setup(db):
    db.executescript(SCHEMA)


def source_for(db, row):
    meta = db.execute('SELECT provider,launcher,timing FROM turn_sources WHERE session=? AND turn=?',
                      (row['session'], row['turn'])).fetchone()
    if meta:
        return dict(meta)
    provider = 'claude' if row['session'].startswith('claude:') else 'codex'
    return dict(provider=provider, launcher=provider, timing='transcript')


def record(db, payload, now):
    """Unclosed/interrupted turns stay excluded; a new prompt never counts idle gaps."""
    if not isinstance(payload, dict) or payload.get('agent_id'):
        return
    event = payload.get('hook_event_name')
    if event not in ('UserPromptSubmit', 'Stop', 'StopFailure'):
        return
    sid, cwd = payload.get('session_id'), payload.get('cwd')
    if (not isinstance(sid, str) or not sid or len(sid) > 256 or
            not isinstance(cwd, str) or not Path(cwd).is_absolute() or
            timestamp(now) is None or now < tracking_start(db)):
        return
    session = 'claude:' + sid
    setup(db)
    project = project_for(cwd)
    db.execute('BEGIN IMMEDIATE')
    try:
        state = db.execute('SELECT * FROM claude_hook_sessions WHERE session=?', (session,)).fetchone()
        if event == 'UserPromptSubmit':
            turn = 'hook:' + str(uuid.uuid4())
            db.execute('INSERT INTO turns(session,turn,project,start) VALUES (?,?,?,?)',
                       (session, turn, project, now))
            db.execute('INSERT INTO turn_sources VALUES (?,?,?,?,?)',
                       (session, turn, 'claude', 'claude', 'hooks'))
            db.execute('''INSERT INTO claude_hook_sessions VALUES (?,?,?,?)
                ON CONFLICT(session) DO UPDATE SET active=excluded.active,last_turn=excluded.last_turn''',
                       (session, now, turn, turn))
        elif state:
            turn = state['active'] or (state['last_turn'] if payload.get('stop_hook_active') is True else None)
            if turn:
                db.execute('UPDATE turns SET end=?,status=? WHERE session=? AND turn=? AND start<=?',
                           (now, 'failed' if event == 'StopFailure' else 'completed', session, turn, now))
                db.execute('UPDATE claude_hook_sessions SET active=NULL WHERE session=?', (session,))
        db.commit()
    except Exception:
        db.rollback()
        raise


def main():
    # Hooks must never block a user's work or emit payloads/errors into their context.
    try:
        raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            return
        payload = json.loads(raw)
        data = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'agent-time'
        with connect(data / 'timing.sqlite3') as db:
            record(db, payload, time.time())
    except (ValueError, OSError, RuntimeError, sqlite3.Error):
        pass

if __name__ == '__main__':
    main()
