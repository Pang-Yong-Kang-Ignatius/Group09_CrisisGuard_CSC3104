"""
Automatic recovery for one hospital.

A background loop checks cloud health every few seconds.
When the cloud is reachable and there is a backlog, it runs the
existing dependency-aware scheduled replay. It also records
reconnection and drain times for the evaluation.

When METAGUARD_ENABLED=1, scheduled replay uses authenticated controller grants
and its durable attempt journal instead of legacy lease-only reconciliation.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

from edge.capture_api.cloud_client import (
    check_cloud_health,
    get_cloud_event_status,
)
from edge.capture_api.scheduled_replay import replay_scheduled_events
from edge.database.db import (
    count_replayable_events,
    get_in_flight_events,
    mark_reconciled_applied,
    clear_stale_claim,
)

logger = logging.getLogger("crisisguard.auto_replay")


def _now():
    return datetime.now(timezone.utc).isoformat()


# Shared status, exposed by GET /recovery/status.
recovery_state = {
    "enabled": False,
    "cloud_reachable": None,
    "service_health": {},
    "last_check_at": None,
    "last_disconnected_at": None,
    "last_reconnected_at": None,
    "drain_completed_at": None,
    "last_replay_at": None,
    "last_replay_attempted": 0,
    "last_replay_applied": 0,
    "replayable_backlog": None,
    "reconciled_on_startup": None,
    "last_error": None,
}


def reconcile_in_flight_events():
    """
    Resolve events left 'in flight' by a crash or restart.

    - Cloud says applied (same site) -> mark applied locally.
    - Cloud says not found           -> release claim, replay later.
    - Cloud unreachable / unclear    -> leave it; the lease will
      expire and the resend is safe because the cloud treats an
      identical resubmission as a duplicate.
    """
    if os.getenv("METAGUARD_ENABLED") == "1":
        # Controller reconciliation fences late Cloud writes; the legacy 404/lease
        # path must never bypass the durable grant attempt journal.
        return {"applied": [], "released": [], "unresolved": []}
    site_id = os.getenv("SITE_ID")
    summary = {"applied": [], "released": [], "unresolved": []}

    for event in get_in_flight_events():
        status = get_cloud_event_status(event)
        event_id = event["event_id"]

        if status["state"] == "applied" and (
            site_id is None or status.get("site_id") == event["site_id"]
        ):
            mark_reconciled_applied(event_id)
            summary["applied"].append(event_id)

        elif status["state"] == "not_found":
            clear_stale_claim(event_id)
            summary["released"].append(event_id)

        else:
            summary["unresolved"].append(event_id)

    return summary


async def run_auto_replay(stop_event, interval_seconds=5.0):
    recovery_state["enabled"] = True
    was_reachable = None
    reconciled = False

    while not stop_event.is_set():
        try:
            health = await asyncio.to_thread(check_cloud_health)
            reachable = any(health.values())
            now = _now()

            recovery_state["service_health"] = health
            recovery_state["cloud_reachable"] = reachable
            recovery_state["last_check_at"] = now

            if was_reachable and not reachable:
                recovery_state["last_disconnected_at"] = now
                logger.warning("Cloud unreachable; capture continues locally")

            if reachable and was_reachable is False:
                recovery_state["last_reconnected_at"] = now
                recovery_state["drain_completed_at"] = None
                logger.info("Cloud reachable again; starting recovery")

            was_reachable = reachable

            if reachable and not reconciled:
                summary = await asyncio.to_thread(reconcile_in_flight_events)
                recovery_state["reconciled_on_startup"] = summary
                reconciled = True

            backlog = await asyncio.to_thread(count_replayable_events)
            recovery_state["replayable_backlog"] = backlog

            if reachable and backlog > 0:
                results = await asyncio.to_thread(replay_scheduled_events)

                recovery_state["last_replay_at"] = _now()
                recovery_state["last_replay_attempted"] = len(results)
                recovery_state["last_replay_applied"] = sum(
                    1 for r in results if r["status"] == "applied"
                )

                backlog = await asyncio.to_thread(count_replayable_events)
                recovery_state["replayable_backlog"] = backlog

                if backlog == 0:
                    recovery_state["drain_completed_at"] = _now()
                    logger.info("Backlog drained")

            recovery_state["last_error"] = None

        except Exception as error:
            # Never let one bad iteration kill the loop.
            recovery_state["last_error"] = str(error)
            logger.exception("Auto replay iteration failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass

    recovery_state["enabled"] = False
