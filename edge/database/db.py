import sqlite3
import json
import os
import random
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "hospital.db"

DB_PATH = Path(
    os.getenv("HOSPITAL_DB_PATH", str(DEFAULT_DB_PATH))
).resolve()

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # Create the hospital events table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                site_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                base_priority INTEGER NOT NULL,
                prerequisite_ids TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT
            )
        """)

        # Migrate older databases that lack created_at
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(events)")
        }

        if "created_at" not in columns:
            conn.execute(
                "ALTER TABLE events ADD COLUMN created_at TEXT"
            )

        # Create the recovery-state table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recovery_state (
                event_id TEXT PRIMARY KEY,
                in_flight INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                retry_after TEXT,
                lease_expires_at TEXT,
                claim_token TEXT,
                FOREIGN KEY (event_id)
                    REFERENCES events(event_id)
            )
        """)

        # Migrate existing recovery-state tables
        recovery_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(recovery_state)"
            )
        }

        if "claim_token" not in recovery_columns:
            conn.execute(
                "ALTER TABLE recovery_state ADD COLUMN claim_token TEXT"
            )

        # Add quarantine fields to existing databases
        if "quarantined" not in recovery_columns:
            conn.execute("""
                ALTER TABLE recovery_state
                ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0
            """)

        if "quarantine_reason" not in recovery_columns:
            conn.execute("""
                ALTER TABLE recovery_state
                ADD COLUMN quarantine_reason TEXT
            """)

        # Initialise recovery state for existing events
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metaguard_attempts (
                event_id TEXT PRIMARY KEY, grant_id TEXT, phase TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO recovery_state (event_id)
            SELECT event_id FROM events
        """)

def save_event(event):
    created_at = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO events
            (event_id, site_id, event_type, base_priority,
             prerequisite_ids, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            event["event_id"],
            event["site_id"],
            event["event_type"],
            event["base_priority"],
            json.dumps(event["prerequisite_ids"]),
            json.dumps(event["payload"]),
            created_at
        ))

        # Create recovery-state record for the new event
        conn.execute("""
            INSERT INTO recovery_state (event_id)
            VALUES (?)
        """, (event["event_id"],))

def get_events():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM events").fetchall()

    events = []
    for row in rows:
        event = dict(row)
        event["prerequisite_ids"] = json.loads(event["prerequisite_ids"])
        event["payload"] = json.loads(event["payload"])
        events.append(event)

    return events

def get_pending_events():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM events WHERE status = 'pending'"
        ).fetchall()

    events = []
    for row in rows:
        event = dict(row)
        event["prerequisite_ids"] = json.loads(
            event["prerequisite_ids"]
        )
        event["payload"] = json.loads(event["payload"])
        events.append(event)

    return events

def mark_event_applied(event_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            UPDATE events
            SET status = 'applied'
            WHERE event_id = ? AND status = 'pending'
            """,
            (event_id,)
        )
        return cursor.rowcount == 1

def claim_event(event_id, lease_seconds=30):
    """
    Atomically claim a pending event for transmission.

    Returns a unique claim token if successful,
    or None if the event cannot be claimed.
    """
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    now = datetime.now(timezone.utc)
    expiry = now + timedelta(seconds=lease_seconds)
    token = str(uuid.uuid4())

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.execute("""
            UPDATE recovery_state
            SET in_flight = 1,
                lease_expires_at = ?,
                claim_token = ?
            WHERE event_id = ?
              AND quarantined = 0
              AND EXISTS (
                  SELECT 1 FROM events
                  WHERE events.event_id = recovery_state.event_id
                    AND events.status = 'pending'
              )
              AND (
                  in_flight = 0
                  OR (
                      in_flight = 1
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                  )
              )
              AND (
                  retry_after IS NULL
                  OR retry_after <= ?
              )
        """, (
            expiry.isoformat(),
            token,
            event_id,
            now.isoformat(),
            now.isoformat()
        ))

        if cursor.rowcount == 1:
            return token

        return None

def release_event(event_id, claim_token):
    """
    Release an event only if the provided claim token
    matches its current claim.

    Returns True if released, False otherwise.
    """
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.execute("""
            UPDATE recovery_state
            SET in_flight = 0,
                lease_expires_at = NULL,
                claim_token = NULL
            WHERE event_id = ?
              AND claim_token = ?
              AND in_flight = 1
        """, (event_id, claim_token))

        return cursor.rowcount == 1

def acknowledge_event(event_id, claim_token):
    """
    Atomically mark an event as applied and clear its
    recovery claim, provided the claim token matches.

    Returns True on success, False otherwise.
    """
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.execute("""
            UPDATE events
            SET status = 'applied'
            WHERE event_id = ?
              AND status = 'pending'
              AND EXISTS (
                  SELECT 1
                  FROM recovery_state
                  WHERE recovery_state.event_id = events.event_id
                    AND claim_token = ?
                    AND in_flight = 1
              )
        """, (event_id, claim_token))

        if cursor.rowcount != 1:
            return False

        conn.execute("""
            UPDATE recovery_state
            SET in_flight = 0,
                lease_expires_at = NULL,
                claim_token = NULL,
                retry_after = NULL
            WHERE event_id = ?
              AND claim_token = ?
        """, (event_id, claim_token))

        return True

def record_failed_attempt(event_id, claim_token):
    """
    Record a failed transmission and schedule a retry.

    Only the current claim holder can update recovery state.

    Returns the retry delay in seconds, or None if the
    claim token is invalid.
    """
    now = datetime.now(timezone.utc)

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        # Acquire the SQLite write lock before reading state
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("""
            SELECT retry_count
            FROM recovery_state
            WHERE event_id = ?
              AND claim_token = ?
              AND in_flight = 1
        """, (event_id, claim_token)).fetchone()

        if row is None:
            return None

        retry_count = row[0] + 1

        max_delay = min(2 ** retry_count, 30)
        delay_seconds = random.uniform(max_delay / 2, max_delay)

        retry_time = now + timedelta(seconds=delay_seconds)

        conn.execute("""
            UPDATE recovery_state
            SET in_flight = 0,
                claim_token = NULL,
                lease_expires_at = NULL,
                retry_count = ?,
                retry_after = ?
            WHERE event_id = ?
              AND claim_token = ?
        """, (
            retry_count,
            retry_time.isoformat(),
            event_id,
            claim_token
        ))

        return delay_seconds

def get_blocked_recovery_ids():
    """
    Return event IDs blocked by quarantine, active claims,
    or retry delays.
    """
    now = datetime.now(timezone.utc)

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT event_id, in_flight,
                   lease_expires_at, retry_after, quarantined
            FROM recovery_state
        """).fetchall()

    blocked_ids = set()

    for event_id, in_flight, lease_expires_at, retry_after, quarantined in rows:
        # Quarantined events cannot be replayed.
        if quarantined:
            blocked_ids.add(event_id)
            continue

        # Events with active, unexpired claims cannot be replayed.
        if in_flight:
            if lease_expires_at is None:
                blocked_ids.add(event_id)
                continue

            lease_expiry = datetime.fromisoformat(
                lease_expires_at
            )

            if lease_expiry > now:
                blocked_ids.add(event_id)
                continue

        # Events waiting for their retry deadline cannot be replayed.
        if retry_after is not None:
            retry_time = datetime.fromisoformat(
                retry_after
            )

            if retry_time > now:
                blocked_ids.add(event_id)

    return blocked_ids

def get_quarantined_recovery_ids():
    """
    Return only permanently quarantined event IDs.

    Unlike get_blocked_recovery_ids(), this excludes
    temporary retry delays and active claims.
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT event_id
            FROM recovery_state
            WHERE quarantined = 1
        """).fetchall()

    return {row[0] for row in rows}

def quarantine_event(event_id, claim_token, reason):
    """
    Quarantine an event after a permanent failure.

    Only the current claim holder can quarantine it.

    Returns True if successful, False otherwise.
    """
    if not reason or not reason.strip():
        raise ValueError("Quarantine reason cannot be empty")

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.execute("""
            UPDATE recovery_state
            SET quarantined = 1,
                quarantine_reason = ?,
                in_flight = 0,
                lease_expires_at = NULL,
                claim_token = NULL,
                retry_after = NULL
            WHERE event_id = ?
              AND claim_token = ?
              AND in_flight = 1
              AND EXISTS (
                  SELECT 1 FROM events
                  WHERE events.event_id = recovery_state.event_id
                    AND events.status = 'pending'
              )
        """, (reason, event_id, claim_token))

        return cursor.rowcount == 1

# ---------------------------------------------------------------
# Automatic recovery helpers
# ---------------------------------------------------------------

def count_replayable_events():
    """
    Count pending events that are not quarantined.

    Quarantined events stay 'pending' forever, so they are
    excluded; otherwise the backlog would never look drained.
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT COUNT(*)
            FROM events e
            LEFT JOIN recovery_state r
                ON r.event_id = e.event_id
            WHERE e.status = 'pending'
              AND COALESCE(r.quarantined, 0) = 0
        """).fetchone()

    return row[0]


def get_in_flight_events():
    """
    Return pending events still marked in flight.

    After a hospital restart these are events whose send
    outcome is unknown: the cloud may or may not have applied them.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT e.*
            FROM events e
            JOIN recovery_state r
                ON r.event_id = e.event_id
            WHERE r.in_flight = 1
              AND e.status = 'pending'
        """).fetchall()

    events = []
    for row in rows:
        event = dict(row)
        event["prerequisite_ids"] = json.loads(event["prerequisite_ids"])
        event["payload"] = json.loads(event["payload"])
        events.append(event)

    return events


def mark_reconciled_applied(event_id):
    """
    Mark an event applied because the cloud confirmed it,
    and clear any leftover claim. Returns True if updated.
    """
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.execute("""
            UPDATE events
            SET status = 'applied'
            WHERE event_id = ?
              AND status = 'pending'
        """, (event_id,))

        conn.execute("""
            UPDATE recovery_state
            SET in_flight = 0,
                lease_expires_at = NULL,
                claim_token = NULL,
                retry_after = NULL
            WHERE event_id = ?
        """, (event_id,))

        return cursor.rowcount == 1


def clear_stale_claim(event_id):
    """
    Release a claim left behind by a crashed process.

    Only call this at startup, before any replay runs,
    because it ignores the claim token.
    """
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.execute("""
            UPDATE recovery_state
            SET in_flight = 0,
                lease_expires_at = NULL,
                claim_token = NULL
            WHERE event_id = ?
              AND in_flight = 1
        """, (event_id,))

        return cursor.rowcount == 1

def get_cloud_event(event_id: str) -> dict | None:
    """Return one event's applied status, or None if it does not exist."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_id, site_id, event_type, status, applied_at
                FROM events
                WHERE event_id = %s
                """,
                (event_id,)
            )
            row = cursor.fetchone()

    if row is None:
        return None

    return {
        "event_id": row[0],
        "site_id": row[1],
        "event_type": row[2],
        "status": row[3],
        "applied_at": row[4].isoformat() if row[4] else None,
    }
