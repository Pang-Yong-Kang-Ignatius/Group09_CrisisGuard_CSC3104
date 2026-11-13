
"""Read-only preview of hospital scheduling decisions."""

import json
import sqlite3
from datetime import datetime, timezone

from edge.database import db
from edge.scheduler.inspector import inspect_scheduler
from edge.scheduler.scheduler import select_next_event
from edge.scheduler.config import load_scheduler_settings
from edge.capture_api import grant_client


def inspect_hospital_queue(enable_inheritance=None):
    """Inspect SQLite without changing or replaying events."""

    settings = load_scheduler_settings()
    if enable_inheritance is None:
        enable_inheritance = settings.enable_inheritance
    comparison_only = enable_inheritance != settings.enable_inheritance
    now = datetime.now(timezone.utc)

    with sqlite3.connect(db.DB_PATH) as conn:
        records = conn.execute("""
            SELECT e.event_id,
                   e.base_priority,
                   e.prerequisite_ids,
                   e.status,
                   e.created_at,
                   COALESCE(r.in_flight, 0),
                   r.lease_expires_at,
                   r.retry_after,
                   COALESCE(r.quarantined, 0)
            FROM events AS e
            LEFT JOIN recovery_state AS r
                ON r.event_id = e.event_id
        """).fetchall()

    events = []
    applied_ids = set()
    in_flight_ids = set()
    retry_after = {}
    quarantined_ids = set()

    for (
        event_id,
        base_priority,
        prerequisites,
        status,
        created_at,
        in_flight,
        lease_expires_at,
        next_retry,
        quarantined,
    ) in records:

        events.append({
            "event_id": event_id,
            "base_priority": base_priority,
            "prerequisite_ids": json.loads(prerequisites),
            "status": status,
            "created_at": created_at,
        })

        if status == "applied":
            applied_ids.add(event_id)

        if status != "pending":
            continue

        if quarantined:
            quarantined_ids.add(event_id)

        if in_flight:
            expiry = (
                datetime.fromisoformat(lease_expires_at)
                if lease_expires_at
                else None
            )

            if expiry is None or expiry > now:
                in_flight_ids.add(event_id)

        if next_retry is not None:
            retry_after[event_id] = (
                datetime.fromisoformat(next_retry)
            )

    # Only inspect pending events.
    # Applied event IDs are still kept for dependency checking.
    pending_events = [
        event
        for event in events
        if event["status"] == "pending"
    ]

    preview = inspect_scheduler(
        pending_events,
        applied_ids=applied_ids,
        now=now,
        in_flight_ids=in_flight_ids,
        retry_after=retry_after,
        permanently_blocked_ids=quarantined_ids,
        enable_inheritance=enable_inheritance,
        ageing_interval_seconds=settings.ageing_interval_seconds,
        selection_policy=settings.selection_policy,
    )

    preview['scheduler_next_event_id'] = preview['next_event_id']
    pinned = grant_client.pinned_event() if grant_client.enabled() else None
    preview['metaguard_enabled'] = grant_client.enabled()
    preview['outstanding_event_id'] = pinned
    preview['replay_wait_reason'] = None
    for row in preview['events']:
        row['dependency_eligible'] = row['eligible']
    if pinned:
        gated = select_next_event(
            pending_events, applied_ids, now=now,
            in_flight_ids=in_flight_ids | {e['event_id'] for e in pending_events if e['event_id'] != pinned},
            retry_after=retry_after, permanently_blocked_ids=quarantined_ids,
            enable_inheritance=enable_inheritance,
            ageing_interval_seconds=settings.ageing_interval_seconds,
            selection_policy=settings.selection_policy,
        )
        preview['next_event_id'] = gated['event_id'] if gated else None
        if gated is None:
            preview['replay_wait_reason'] = f'Waiting for outstanding MetaGuard event {pinned}'
        for row in preview['events']:
            row['selected'] = row['event_id'] == preview['next_event_id']
            if row['event_id'] != pinned and row['eligible']:
                row['eligible'] = False
                row['reason'] = f'Waiting for outstanding MetaGuard event {pinned}'

    preview["pending_count"] = len(pending_events)
    preview["replay_policy"] = settings.as_dict()
    preview["ageing_interval_seconds"] = settings.ageing_interval_seconds
    preview["comparison_only"] = comparison_only

    preview["warning"] = (
        "Read-only snapshot. Concurrent replay or capture "
        "may change the next scheduling decision. Controller grants/admission "
        "can still defer transmission. "
        + ("Comparison only; this preview does not change the active replay policy."
           if comparison_only else "Preview uses the active replay policy.")
    )

    return preview
