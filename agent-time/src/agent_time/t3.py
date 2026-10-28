"""Read T3 v2 timing projections; never read messages, settings, or auth tables."""
from pathlib import Path
import sqlite3
from .core import project_for, timestamp, tracking_start
from .claude_hooks import setup

SCHEMA = '''CREATE TABLE IF NOT EXISTS t3_bindings (
 provider_turn TEXT PRIMARY KEY, session TEXT NOT NULL, turn TEXT NOT NULL,
 UNIQUE(session,turn));'''
QUERY = '''SELECT t.provider_turn_id, t.status, t.started_at, t.completed_at,
 json_extract(t.payload_json,'$.nativeTurnRef.nativeId') native_turn,
 json_extract(t.payload_json,'$.nativeTurnRef.strength') strength,
 json_extract(p.payload_json,'$.nativeThreadRef.nativeId') native_session,
 p.driver, p.owner_node_id, n.parent_node_id, n.kind,
 COALESCE(json_extract(s.payload_json,'$.cwd'),
          json_extract(a.payload_json,'$.worktreePath'), j.workspace_root) cwd
 FROM orchestration_v2_projection_provider_turns t
 JOIN orchestration_v2_projection_provider_threads p USING(provider_thread_id)
 JOIN orchestration_v2_projection_nodes n ON n.node_id=t.node_id
 JOIN orchestration_v2_projection_threads a ON a.thread_id=t.thread_id
 LEFT JOIN projection_projects j ON j.project_id=a.project_id
 LEFT JOIN orchestration_v2_projection_provider_sessions s
 ON s.provider_session_id=p.provider_session_id
 ORDER BY t.started_at,t.provider_turn_id'''


def candidates(db, session, start, end, exclude=''):
    # Claude's native turn ref is a message cursor, not the hook's turn ID.
    # Correlate only inside the same native session and actual overlapping interval.
    return db.execute('''SELECT * FROM turns r WHERE session=? AND turn!=?
        AND turn NOT LIKE 't3:%' AND start<=?
        AND ((end IS NULL AND start>=?) OR (end IS NOT NULL AND end>?))
        AND NOT EXISTS (SELECT 1 FROM turn_aliases a WHERE a.session=r.session AND a.turn=r.turn)
        ORDER BY start''', (session, exclude, end if end is not None else start, start, start)).fetchall()


def ingest_t3(db, path: Path):
    counts = dict(available=0, imported=0, linked=0, excluded=0, ambiguous=0, unsupported=0)
    if not path.is_file():
        return counts
    setup(db)
    db.executescript(SCHEMA)
    since = tracking_start(db)
    source = None
    try:
        source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        source.row_factory = sqlite3.Row
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        # Validate the schema/query before mutating the collector.
        rows = source.execute(QUERY)
        counts['available'] = 1
        with db:
            for row in rows:
                start, end = timestamp(row['started_at']), timestamp(row['completed_at'])
                if row['owner_node_id'] is not None or row['parent_node_id'] is not None or row['kind'] != 'root_turn':
                    counts['excluded'] += 1
                    continue
                if start is not None and start < since:
                    continue
                provider = {'codex': 'codex', 'claudeAgent': 'claude'}.get(row['driver'])
                if (provider is None or start is None or (row['completed_at'] is not None and end is None)
                    or (end is not None and end < start)
                    or any(not isinstance(row[k], str) or not row[k] for k in ('native_session','provider_turn_id','cwd'))
                    or not Path(row['cwd']).is_absolute()):
                    counts['unsupported'] += 1
                    continue
                statuses = {'completed':'completed','failed':'failed','interrupted':'interrupted',
                            'cancelled':'interrupted','running':'incomplete','pending':'incomplete'}
                status = statuses.get(row['status'])
                if status is None or (status != 'incomplete' and end is None):
                    counts['unsupported'] += 1
                    continue
                if status == 'incomplete':
                    end = None
                session = ('claude:' if provider == 'claude' else '') + row['native_session']
                binding = db.execute('SELECT * FROM t3_bindings WHERE provider_turn=?', (row['provider_turn_id'],)).fetchone()
                if binding and binding['session'] != session:
                    counts['ambiguous'] += 1
                    continue
                existing = None
                if binding:
                    turn = binding['turn']
                    existing = db.execute('SELECT * FROM turns WHERE session=? AND turn=?', (session,turn)).fetchone()
                elif provider == 'codex':
                    if row['strength'] != 'strong' or not isinstance(row['native_turn'], str) or not row['native_turn']:
                        counts['unsupported'] += 1
                        continue
                    turn = row['native_turn']
                    existing = db.execute('SELECT * FROM turns WHERE session=? AND turn=?', (session,turn)).fetchone()
                else:
                    found = candidates(db, session, start, end)
                    if len(found) > 1:
                        counts['ambiguous'] += 1
                        continue
                    existing = found[0] if found else None
                    turn = existing['turn'] if existing else 't3:' + row['provider_turn_id']
                claimed = db.execute('SELECT provider_turn FROM t3_bindings WHERE session=? AND turn=?', (session,turn)).fetchone()
                if claimed and claimed[0] != row['provider_turn_id']:
                    counts['ambiguous'] += 1
                    continue
                if turn.startswith('t3:'):
                    late = candidates(db, session, start, end)
                    late_claim = db.execute('SELECT provider_turn FROM t3_bindings WHERE session=? AND turn=?',
                                            (session, late[0]['turn'])).fetchone() if len(late) == 1 else None
                    if len(late) > 1 or (late_claim and late_claim[0] != row['provider_turn_id']):
                        # A provisional T3 row must not add to ambiguous native history.
                        db.execute('INSERT OR REPLACE INTO turn_aliases VALUES (?,?,?,?,?)',
                                   (session,turn,session,turn,'ambiguous-t3-native-match'))
                        counts['ambiguous'] += 1
                        continue
                    db.execute("DELETE FROM turn_aliases WHERE session=? AND turn=? AND reason='ambiguous-t3-native-match'", (session,turn))
                    if late:
                        # Preserve the already-published canonical key. The duplicate is
                        # exported as a zero-duration tombstone if it was already synced.
                        duplicate = late[0]
                        db.execute('INSERT OR REPLACE INTO turn_aliases VALUES (?,?,?,?,?)',
                                   (session,duplicate['turn'],session,turn,'same-t3-native-turn'))
                if existing is None or turn.startswith('t3:'):
                    project = existing['project'] if existing else project_for(row['cwd'])
                    db.execute('''INSERT INTO turns VALUES (?,?,?,?,?,?)
                      ON CONFLICT(session,turn) DO UPDATE SET start=excluded.start,end=excluded.end,status=excluded.status''',
                               (session,turn,project,start,end,status))
                    timing = 't3'
                    counts['imported'] += 1
                else:
                    # Existing native measurements and cloud keys stay stable.
                    meta = db.execute('SELECT timing FROM turn_sources WHERE session=? AND turn=?', (session,turn)).fetchone()
                    timing = meta[0] if meta else 'transcript'
                    if existing['end'] is None and end is not None:
                        db.execute('UPDATE turns SET end=?,status=? WHERE session=? AND turn=? AND start<=?',
                                   (end,status,session,turn,end))
                        timing = 'native-start+t3-end'
                    counts['linked'] += 1
                db.execute('INSERT OR IGNORE INTO t3_bindings VALUES (?,?,?)', (row['provider_turn_id'],session,turn))
                db.execute('INSERT OR REPLACE INTO turn_sources VALUES (?,?,?,?,?)', (session,turn,provider,'t3',timing))
    except (sqlite3.Error, OSError, ValueError):
        counts['unsupported'] += 1
    finally:
        if source is not None:
            source.close()
    return counts
