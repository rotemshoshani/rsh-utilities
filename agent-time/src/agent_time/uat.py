"""Loopback-only collector of anonymous development activity intervals."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .core import connect, initialize, project_for, union_seconds

IDLE_SECONDS = 120
SCHEMA = """
CREATE TABLE IF NOT EXISTS uat_tabs (
    project TEXT, tab TEXT, sequence INTEGER NOT NULL,
    interval_id INTEGER, PRIMARY KEY(project, tab)
);
CREATE TABLE IF NOT EXISTS uat_intervals (
    id INTEGER PRIMARY KEY, project TEXT NOT NULL,
    start REAL NOT NULL, end REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS uat_project_time ON uat_intervals(project,start,end);
"""


def setup(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def valid_event(event: object) -> bool:
    return (isinstance(event, dict) and set(event) == {'tab', 'seq', 'kind'}
            and isinstance(event['tab'], str)
            and re.fullmatch(r'[a-f0-9-]{36}', event['tab']) is not None
            and type(event['seq']) is int and 0 <= event['seq'] < 2**53
            and event['kind'] in ('activity', 'hidden'))


def record_event(db: sqlite3.Connection, project: str, event: dict, now: float) -> None:
    """Server receive time is authoritative; sequence numbers discard late duplicates."""
    if not valid_event(event):
        raise ValueError('Invalid activity event')
    with db:
        # Serialize a read/modify/write against simultaneous reporters/collectors.
        db.execute('BEGIN IMMEDIATE')
        tab = db.execute('SELECT * FROM uat_tabs WHERE project=? AND tab=?',
                         (project, event['tab'])).fetchone()
        if tab and event['seq'] <= tab['sequence']:
            return
        interval_id = tab['interval_id'] if tab else None
        interval = db.execute('SELECT * FROM uat_intervals WHERE id=?', (interval_id,)).fetchone()
        if event['kind'] == 'hidden':
            if interval:
                db.execute('UPDATE uat_intervals SET end=MAX(start,MIN(end,?)) WHERE id=?', (now, interval_id))
            interval_id = None
        elif interval and interval['end'] >= now:
            db.execute('UPDATE uat_intervals SET end=? WHERE id=?', (max(interval['end'], now + IDLE_SECONDS), interval_id))
        else:
            interval_id = db.execute('INSERT INTO uat_intervals(project,start,end) VALUES (?,?,?)',
                                     (project, now, now + IDLE_SECONDS)).lastrowid
        db.execute('INSERT OR REPLACE INTO uat_tabs VALUES (?,?,?,?)',
                   (project, event['tab'], event['seq'], interval_id))


def report(db: sqlite3.Connection, start: float, end: float,
           project: str | None = None, now: float | None = None) -> tuple[dict[str, float], float]:
    # Reporting must never count a future tail before that time has elapsed.
    end = min(end, time.time() if now is None else now)
    groups: dict[str, list[tuple[float, float]]] = {}
    if end <= start:
        return {}, 0.0
    for row in db.execute('SELECT project,start,end FROM uat_intervals WHERE start < ? AND end > ?', (end, start)):
        if project is None or row['project'] == project:
            groups.setdefault(row['project'], []).append((max(start, row['start']), min(end, row['end'])))
    return ({key: union_seconds(value) for key, value in groups.items()},
            union_seconds([interval for values in groups.values() for interval in values]))


def make_server(database: Path, project: str, port: int) -> HTTPServer:
    project = project_for(project)
    with connect(database) as db:
        setup(db)
        initialize(db, time.time())
    db.close()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(1)

        def log_message(self, *_args):
            pass  # Never log request headers or bodies.

        def reply(self, status: int, payload: dict | None = None):
            body = json.dumps(payload).encode() if payload is not None else b''
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/health':
                self.reply(200, {'service': 'agent-time-uat', 'project': project, 'idle_seconds': IDLE_SECONDS})
            else:
                self.reply(404)

        def do_POST(self):
            if self.path != '/activity':
                self.reply(404)
                return
            # Only the server-side forwarder should call us; browsers have no CORS access.
            if self.headers.get('Origin') or self.headers.get('Transfer-Encoding'):
                self.reply(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 256:
                    self.reply(413)
                    return
                event = json.loads(self.rfile.read(size))
                if not valid_event(event):
                    self.reply(400)
                    return
                db = connect(database)
                try:
                    record_event(db, project, event, time.time())
                finally:
                    db.close()
                self.reply(204)
            except (ValueError, UnicodeDecodeError):
                self.reply(400)
            except (OSError, sqlite3.Error):
                self.reply(503)

    class Server(HTTPServer):
        def handle_error(self, *_args):
            pass  # Client disconnects must not dump request details or tracebacks.

    return Server(('127.0.0.1', port), Handler)
