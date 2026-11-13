
import os
import psycopg
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://crisisguard:crisisguard_dev@127.0.0.1:5433/crisisguard"
)




def get_connection():
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=3
    )


def test_connection():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            return cursor.fetchone()[0]


def init_db():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    base_priority INTEGER NOT NULL,
                    prerequisite_ids JSONB NOT NULL,
                    payload JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'applied',
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)



class MissingPrerequisitesError(Exception):
    def __init__(self, missing_ids):
        self.missing_ids = missing_ids
        super().__init__(
            f"Missing prerequisites: {missing_ids}"
        )

class ConflictingEventError(Exception):
    pass



def save_cloud_event(event, *, grant_id=None):
    with get_connection() as conn:
        with conn.cursor() as cursor:

            if grant_id is not None:
                from cloud.metaguard import fence_cloud_write
                fence_cloud_write(cursor, event, grant_id)

            # Prevent concurrent requests with the same event ID
            # from interfering with each other.
            cursor.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended(%s, 0)
                )
                """,
                (event["event_id"],)
            )

            # Step 1: Check whether this event already exists.
            cursor.execute(
                """
                SELECT
                    site_id,
                    event_type,
                    base_priority,
                    prerequisite_ids,
                    payload,
                    status
                FROM events
                WHERE event_id = %s
                """,
                (event["event_id"],)
            )

            existing = cursor.fetchone()

            if existing is not None:
                stored_event = {
                    "site_id": existing[0],
                    "event_type": existing[1],
                    "base_priority": existing[2],
                    "prerequisite_ids": existing[3],
                    "payload": existing[4],
                }

                incoming_event = {
                    "site_id": event["site_id"],
                    "event_type": event["event_type"],
                    "base_priority": event["base_priority"],
                    "prerequisite_ids": event["prerequisite_ids"],
                    "payload": event["payload"],
                }

                if (
                    stored_event != incoming_event
                    or existing[5] != "applied"
                ):
                    raise ConflictingEventError(
                        "Event ID already exists with different data or status"
                    )

                return "already_applied"

            # Step 2: Check prerequisites for a new event.
            prerequisite_ids = event["prerequisite_ids"]

            if prerequisite_ids:
                cursor.execute(
                    """
                    SELECT event_id
                    FROM events
                    WHERE site_id = %s
                      AND status = 'applied'
                      AND event_id = ANY(%s)
                    """,
                    (event["site_id"], prerequisite_ids)
                )

                applied_ids = {
                    row[0] for row in cursor.fetchall()
                }

                missing_ids = [
                    event_id
                    for event_id in prerequisite_ids
                    if event_id not in applied_ids
                ]

                if missing_ids:
                    raise MissingPrerequisitesError(missing_ids)

            # Step 3: Insert the event.
            cursor.execute(
                """
                INSERT INTO events (
                    event_id,
                    site_id,
                    event_type,
                    base_priority,
                    prerequisite_ids,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                (
                    event["event_id"],
                    event["site_id"],
                    event["event_type"],
                    event["base_priority"],
                    Jsonb(event["prerequisite_ids"]),
                    Jsonb(event["payload"])
                )
            )

            inserted = cursor.fetchone()

            if inserted is not None:
                return "applied"

            # Safety check if another writer inserted this ID.
            # Do not acknowledge an unverified duplicate.
            raise ConflictingEventError(
                "Event ID was concurrently inserted; retry to verify it"
            )



def get_missing_prerequisites(site_id: str, prerequisite_ids: list[str]) -> list[str]:
    """Find prerequisite events not yet applied at this hospital."""
    if not prerequisite_ids:
        return []

    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_id
                FROM events
                WHERE site_id = %s
                  AND status = 'applied'
                  AND event_id = ANY(%s)
                """,
                (site_id, prerequisite_ids)
            )

            applied_ids = {
                row[0] for row in cursor.fetchall()
            }

    return [
        event_id
        for event_id in prerequisite_ids
        if event_id not in applied_ids
    ]


def get_recent_cloud_events(limit: int = 50) -> list[dict]:
    """Retrieve recent applied cloud events without modifying them."""
    limit = max(1, min(limit, 100))

    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    event_id,
                    site_id,
                    event_type,
                    base_priority,
                    prerequisite_ids,
                    payload,
                    status,
                    applied_at
                FROM events
                ORDER BY applied_at DESC NULLS LAST, event_id DESC
                LIMIT %s
                """,
                (limit,)
            )

            rows = cursor.fetchall()

    return [
        {
            "event_id": row[0],
            "site_id": row[1],
            "event_type": row[2],
            "base_priority": row[3],
            "prerequisite_ids": row[4],
            "payload": row[5],
            "status": row[6],
            "applied_at": (
                row[7].isoformat() if row[7] else None
            ),
        }
        for row in rows
    ]


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