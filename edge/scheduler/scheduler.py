
from datetime import datetime, timezone

from edge.scheduler.dag import DependencyDAG
from edge.scheduler.priority import (
    calculate_aged_priority,
    calculate_effective_priorities,
)
from edge.scheduler.validation import (
    validate_event_collection,
    validate_event_record,
    validate_ageing_interval,
    validate_scheduler_arguments,
    validate_enable_inheritance,
)


def select_next_event(
    events,
    applied_ids,
    now=None,
    ageing_interval_seconds=600,
    in_flight_ids=None,
    retry_after=None,
    permanently_blocked_ids=None,
    enable_inheritance=True,
    include_priority=False,
    selection_policy="priority"
):
    """
    Select the highest-priority eligible event.

    Excludes applied events, isolates cyclic and permanently
    blocked workflows, and respects temporary unavailability.

    When enable_inheritance is True, priorities include
    inherited urgency from unfinished descendants.

    When enable_inheritance is False, events are ranked
    using only their own aged priorities.

    Eligible events are ordered by:
        1. Highest effective priority
        2. Earliest creation timestamp
        3. Lexicographically smallest event ID

    Returns the selected event dictionary, or None when
    no event is eligible.
    """

    # Validate scheduling parameters before processing events.
    validate_ageing_interval(ageing_interval_seconds)
    validate_enable_inheritance(enable_inheritance)
    if selection_policy not in ("priority", "fifo"):
        raise ValueError("selection_policy must be priority or fifo")

    validate_scheduler_arguments(
        applied_ids=applied_ids,
        now=now,
        in_flight_ids=in_flight_ids,
        retry_after=retry_after,
        permanently_blocked_ids=permanently_blocked_ids
    )

    # Reject invalid event containers before iterating over them.
    validate_event_collection(events)

    # Step 1: Validate events and remove already-applied
    # events while preserving duplicate-ID validation.
    unapplied_events = []
    seen_ids = set()

    for event in events:
        # Validate the event before accessing its fields.
        validate_event_record(event)

        event_id = event["event_id"]

        if event_id in seen_ids:
            raise ValueError(
                f"Duplicate event ID: {event_id}"
            )

        seen_ids.add(event_id)

        if event_id not in applied_ids:
            unapplied_events.append(event)

    # Construct a dependency graph using unfinished events.
    dag = DependencyDAG(unapplied_events)

    if now is None:
        now = datetime.now(timezone.utc)

    # Step 2: Identify blocked workflows.

    # Events belonging to dependency cycles and their
    # dependent descendants cannot be scheduled.
    cycle_blocked_ids = dag.get_cycle_blocked_ids()

    # Permanently quarantined events and their dependent
    # descendants must not contribute inherited urgency.
    permanent_ids = dag.get_permanently_blocked_ids(
        permanently_blocked_ids
    )

    blocked_ids = cycle_blocked_ids | permanent_ids

    # Temporarily unavailable events cannot be selected,
    # but may still contribute inherited urgency.
    unavailable_ids = (
        set(in_flight_ids or ()) | blocked_ids
    )

    # Step 3: Determine eligible events.
    eligible = dag.get_eligible_events(
        applied_ids,
        in_flight_ids=unavailable_ids,
        retry_after=retry_after,
        now=now
    )

    if not eligible:
        return None

    # Step 4: Calculate priorities using the chosen policy.

    if enable_inheritance:
        # Inheritance ON:
        # Find events with unresolved missing prerequisites.
        missing_ids = set(
            dag.get_missing_prerequisites(applied_ids)
        )

        # Exclude those events and their descendants from
        # contributing inherited urgency.
        # This only calculates affected IDs; it does not
        # permanently quarantine or modify any events.
        missing_inheritance_ids = (
            dag.get_permanently_blocked_ids(missing_ids)
        )

        # Exclude cycles, permanent failures, and workflows
        # with missing prerequisites from inheritance.
        inheritance_excluded_ids = (
            blocked_ids | missing_inheritance_ids
        )

        healthy_events = [
            event
            for event in unapplied_events
            if event["event_id"] not in inheritance_excluded_ids
        ]

        healthy_dag = DependencyDAG(healthy_events)

        # Inherit the highest aged priority from unfinished,
        # non-blocked descendants.
        effective = calculate_effective_priorities(
            healthy_dag,
            applied_ids,
            now=now,
            ageing_interval_seconds=ageing_interval_seconds
        )

    else:
        # Inheritance OFF:
        # Use each eligible event's own aged priority.
        # No additional dependency graph is needed.
        effective = {
            event["event_id"]: calculate_aged_priority(
                event,
                now=now,
                ageing_interval_seconds=ageing_interval_seconds
            )
            for event in eligible
        }

    # Step 5: Apply deterministic event ordering.
    def sort_key(event):
        created_at = event.get("created_at")

        if created_at is None:
            created_at = now

        elif isinstance(created_at, str):
            created_at = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            )

        if created_at.tzinfo is None:
            raise ValueError(
                "Creation timestamp must include timezone"
            )

        return (
            -effective[event["event_id"]] if selection_policy == "priority" else 0,
            created_at,
            event["event_id"]
        )

    # Step 6: Return the highest-priority eligible event.
    selected = min(eligible, key=sort_key)
    return {**selected, "effective_priority": effective[selected["event_id"]]} if include_priority else selected
