"""Explicit experimental retry for proven unsent, fresh admission deferrals."""
from datetime import datetime, timedelta, timezone
import os
import sqlite3
from threading import Lock

from edge.database import db

replay_lock = Lock()


def enabled():
    policy = os.getenv('METAGUARD_RETRY_POLICY', 'failure-backoff')
    if policy not in ('failure-backoff', 'bounded-deferral'):
        raise ValueError('Unknown MetaGuard retry policy')
    if policy == 'bounded-deferral' and (os.getenv('CRISISGUARD_EXPERIMENTAL_POLICIES') != '1'
                                        or os.getenv('METAGUARD_ENABLED') != '1'):
        raise ValueError('Bounded deferral requires experimental opt-in and MetaGuard')
    return policy == 'bounded-deferral'


def hint(value):
    if (not isinstance(value, dict) or value.get('reason') not in ('rate_limit', 'inflight_limit')
            or type(value.get('retry_after_seconds')) is not int
            or not 1 <= value['retry_after_seconds'] <= 30):
        return None
    return {k: value[k] for k in ('reason', 'retry_after_seconds')}


def waiting():
    """A durable per-hospital gate prevents cycling through other ready events."""
    with sqlite3.connect(db.DB_PATH, timeout=10) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metaguard_deferrals'").fetchone()
        if not exists:
            return False
        row = conn.execute('SELECT MAX(retry_after) FROM metaguard_deferrals').fetchone()
    return bool(row and row[0] and datetime.fromisoformat(row[0]) > datetime.now(timezone.utc))


def record(event_id, claim_token, proof):
    """Release only an unsent matching claim; never reset genuine failure history."""
    proof = hint(proof)
    if not proof:
        raise ValueError('Missing definite admission proof')
    now = datetime.now(timezone.utc)
    deadline = (now+timedelta(seconds=proof['retry_after_seconds'])).isoformat()
    with sqlite3.connect(db.DB_PATH, timeout=10) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("""SELECT r.retry_after FROM recovery_state r
            JOIN events e ON e.event_id=r.event_id
            JOIN metaguard_attempts a ON a.event_id=r.event_id
            WHERE r.event_id=? AND r.claim_token=? AND r.in_flight=1
              AND r.quarantined=0 AND e.status='pending' AND a.phase='registered'""",
            (event_id, claim_token)).fetchone()
        if row is None:
            return None
        deadline = max(deadline, row[0] or deadline)
        conn.execute("""UPDATE recovery_state SET in_flight=0, claim_token=NULL,
            lease_expires_at=NULL, retry_after=? WHERE event_id=? AND claim_token=?""",
            (deadline, event_id, claim_token))
        # Created only when the opt-in path is actually used; legacy DB schema
        # and retry semantics remain unchanged. Gate survives crashes/restarts.
        conn.execute("""CREATE TABLE IF NOT EXISTS metaguard_deferrals (
            id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, reason TEXT NOT NULL,
            deferred_at TEXT NOT NULL, retry_after TEXT NOT NULL)""")
        conn.execute("""INSERT INTO metaguard_deferrals(event_id,reason,deferred_at,retry_after)
            VALUES (?,?,?,?)""", (event_id, proof['reason'], now.isoformat(), deadline))
    return deadline
