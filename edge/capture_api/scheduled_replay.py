
import json
import logging

from edge.database.db import (
    get_events,
    get_blocked_recovery_ids,
    get_quarantined_recovery_ids,
    claim_event,
    acknowledge_event,
    record_failed_attempt,
    quarantine_event
)
from edge.capture_api.cloud_client import send_to_cloud
from edge.capture_api import grant_client, admission_retry
from edge.scheduler.scheduler import select_next_event
from edge.scheduler.config import load_scheduler_settings

# Inherit Uvicorn's INFO handler so deployed replay traces are actually emitted.
logger = logging.getLogger('uvicorn.error.scheduler')


def replay_scheduled_events():
    bounded_retry = admission_retry.enabled()
    if bounded_retry and not admission_retry.replay_lock.acquire(blocking=False):
        return []
    try:
        return _replay_scheduled_events(bounded_retry)
    finally:
        if bounded_retry:
            admission_retry.replay_lock.release()


def _replay_scheduled_events(bounded_retry):
    """
    Sequential dependency-aware replay with persistent
    claiming, acknowledgements, retry backoff,
    and permanent failure quarantine.
    """
    # Validate before reading/claiming work or contacting Cloud/controller.
    settings = load_scheduler_settings()
    if bounded_retry and admission_retry.waiting():
        return []
    results = []
    attempted_ids = set()

    while True:
        if bounded_retry and admission_retry.waiting():
            break
        events = get_events()

        applied_ids = {
            event["event_id"]
            for event in events
            if event["status"] == "applied"
        }

        blocked_ids = get_blocked_recovery_ids()
        quarantined_ids = get_quarantined_recovery_ids()

        # Exclude events that are claimed, quarantined,
        # waiting for retry, or already attempted.
        unavailable_ids = blocked_ids | attempted_ids

        if grant_client.enabled():
            outstanding = grant_client.pinned_event()
            if outstanding:
                unavailable_ids |= {e["event_id"] for e in events if e["event_id"] != outstanding}

        selected = select_next_event(
            events,
            applied_ids=applied_ids,
            in_flight_ids=unavailable_ids,
            permanently_blocked_ids=quarantined_ids,
            enable_inheritance=settings.enable_inheritance,
            ageing_interval_seconds=settings.ageing_interval_seconds,
            include_priority=True,
            selection_policy=settings.selection_policy,
        )

        if selected is None:
            if grant_client.enabled() and not outstanding:
                # Remove a stale unsent candidate when all local work becomes
                # ineligible. This operation never withdraws acknowledged work.
                try:
                    grant_client.advertise_candidate()
                except Exception:
                    # No ACK/send occurred here. Keep both journals intact and
                    # retry withdrawal on the next recovery iteration.
                    logger.warning('scheduler.candidate_withdrawal_deferred')
            break

        event_id = selected["event_id"]
        attempted_ids.add(event_id)

        logger.info('scheduler.selection %s', json.dumps({
            'event_id': event_id, 'base_priority': selected['base_priority'],
            'effective_priority': selected['effective_priority'],
            'prerequisite_ids': selected['prerequisite_ids'],
            'scheduler_policy': settings.as_dict(),
        }))

        # Claim the event atomically before transmission.
        token = claim_event(event_id)

        if token is None:
            logger.info('scheduler.outcome %s', json.dumps({'event_id': event_id, 'status': 'not_claimed'}))
            results.append({
                "event_id": event_id,
                "status": "not_claimed"
            })
            continue

        try:
            response = (grant_client.transmit(selected, send_to_cloud)
                        if grant_client.enabled() else send_to_cloud(selected))

            # Successful cloud acknowledgement
            if (
                isinstance(response, dict)
                and response.get("status") == "applied"
                and response.get("event_id") == event_id
            ):
                updated = acknowledge_event(
                    event_id,
                    token
                )

                results.append({
                    "event_id": event_id,
                    "status": (
                        "applied" if updated
                        else "acknowledgement_not_recorded"
                    )
                })

            else:
                status_code = (
                    response.get("status_code")
                    if isinstance(response, dict)
                    else None
                )

                proof = (admission_retry.hint(response.get('admission_deferral'))
                         if bounded_retry and isinstance(response, dict)
                         and response.get('status') in ('waiting_for_grant', 'waiting_for_admission') else None)
                if proof:
                    deadline = admission_retry.record(event_id, token, proof)
                    results.append({'event_id': event_id, 'status': 'admission_deferred' if deadline else 'deferral_not_recorded',
                                    'reason': proof['reason'], 'retry_after': deadline})
                    logger.info('scheduler.admission_deferred %s', json.dumps(results[-1]))
                    # Retain the current candidate/grant and fairness position.
                    # Do not scan/withdraw other candidates during the cooldown.
                    break
                # Permanent validation failure
                elif status_code == 422:
                    reason = (
                        f"HTTP 422: "
                        f"{response.get('error', 'Invalid event data')}"
                    )

                    quarantined = quarantine_event(
                        event_id,
                        token,
                        reason
                    )

                    results.append({
                        "event_id": event_id,
                        "status": (
                            "quarantined" if quarantined
                            else "quarantine_not_recorded"
                        ),
                        "reason": reason
                    })

                # Temporary or unresolved failure
                else:
                    delay = record_failed_attempt(
                        event_id,
                        token
                    )

                    results.append({
                        "event_id": event_id,
                        "status": "pending",
                        "retry_delay_seconds": delay,
                        "response": response
                    })

        except Exception as exc:
            # Preserve the event and schedule a retry
            # if an unexpected transmission error occurs.
            delay = record_failed_attempt(
                event_id,
                token
            )

            results.append({
                "event_id": event_id,
                "status": "pending",
                "retry_delay_seconds": delay,
                "error": str(exc)
            })

        logger.info('scheduler.outcome %s', json.dumps({
            'event_id': event_id, 'status': results[-1]['status'],
        }))

    return results
