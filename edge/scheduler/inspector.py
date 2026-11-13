
"""Read-only explanation of a stateless scheduler decision.

This is a diagnostic tool, not a replay engine. It does not modify events,
claim work, write to SQLite, or communicate with cloud services.
"""

from collections import deque
from datetime import datetime, timezone

from edge.scheduler.dag import DependencyDAG
from edge.scheduler.priority import (
    calculate_aged_priority,
    calculate_effective_priorities,
)
from edge.scheduler.scheduler import select_next_event
from edge.scheduler.validation import (
    validate_ageing_interval,
    validate_enable_inheritance,
    validate_event_collection,
    validate_event_record,
    validate_scheduler_arguments,
)


def inspect_scheduler(
    events,
    applied_ids,
    now=None,
    ageing_interval_seconds=600,
    in_flight_ids=None,
    retry_after=None,
    permanently_blocked_ids=None,
    enable_inheritance=True,
    selection_policy="priority",
):
    """Preview why the stateless scheduler would choose an event.

    The selected event is always obtained from select_next_event(), rather
    than from a second, independent selection implementation. Effective
    priorities follow the same healthy-graph construction as that scheduler.

    The caller must supply current applied, in-flight, retry and permanent-
    blockage information. The preview cannot infer private recovery state.
    """
    validate_ageing_interval(ageing_interval_seconds)
    validate_enable_inheritance(enable_inheritance)
    validate_scheduler_arguments(
        applied_ids=applied_ids,
        now=now,
        in_flight_ids=in_flight_ids,
        retry_after=retry_after,
        permanently_blocked_ids=permanently_blocked_ids,
    )
    validate_event_collection(events)

    snapshot = list(events)  # Also support one-shot generators.
    seen = set()
    for event in snapshot:
        validate_event_record(event)
        event_id = event["event_id"]
        if event_id in seen:
            raise ValueError(f"Duplicate event ID: {event_id}")
        seen.add(event_id)

    if now is None:
        now = datetime.now(timezone.utc)

    applied = set(applied_ids)
    in_flight = set(in_flight_ids or ())
    retry = retry_after or {}

    active_events = [
        e for e in snapshot if e["event_id"] not in applied
    ]
    dag = DependencyDAG(active_events)

    cycle_blocked = dag.get_cycle_blocked_ids()
    permanent_blocked = dag.get_permanently_blocked_ids(
        permanently_blocked_ids
    )

    blocked = cycle_blocked | permanent_blocked

    missing_direct = dag.get_missing_prerequisites(applied)
    missing_inheritance_blocked = dag.get_permanently_blocked_ids(
        set(missing_direct)
    )
    inheritance_excluded = blocked | missing_inheritance_blocked

    eligible_ids = {
        event["event_id"]
        for event in dag.get_eligible_events(
            applied,
            in_flight_ids=blocked | in_flight,
            retry_after=retry,
            now=now,
        )
    }

    healthy = [
        event
        for event in active_events
        if event["event_id"] not in inheritance_excluded
    ]
    healthy_dag = DependencyDAG(healthy)

    aged = {
        e["event_id"]: calculate_aged_priority(
            e,
            now=now,
            ageing_interval_seconds=ageing_interval_seconds,
        )
        for e in snapshot
    }

    if enable_inheritance:
        effective = calculate_effective_priorities(
            healthy_dag,
            applied,
            now=now,
            ageing_interval_seconds=ageing_interval_seconds,
        )
    else:
        effective = {
            e["event_id"]: aged[e["event_id"]]
            for e in healthy
            if e["status"] == "pending"
        }

    # Attribute inherited urgency to one highest-priority unfinished
    # descendant, choosing the lowest event ID when there are ties.
    source = {
        event_id: event_id
        for event_id in effective
    }

    if enable_inheritance and effective:
        indegree = {
            event_id: 0
            for event_id in healthy_dag.events
        }

        for children in healthy_dag.graph.values():
            for child in children:
                indegree[child] += 1

        ready = deque(
            event_id
            for event_id, degree in indegree.items()
            if degree == 0
        )

        topo = []

        while ready:
            event_id = ready.popleft()
            topo.append(event_id)

            for child in healthy_dag.graph[event_id]:
                indegree[child] -= 1

                if indegree[child] == 0:
                    ready.append(child)

        for event_id in reversed(topo):
            if event_id not in effective:
                continue

            if effective[event_id] <= aged[event_id]:
                continue

            possibilities = [
                source[child]
                for child in healthy_dag.graph[event_id]
                if (
                    child in source
                    and effective[child] == effective[event_id]
                )
            ]

            if possibilities:
                source[event_id] = min(possibilities)

    selected = select_next_event(
        snapshot,
        applied_ids=applied,
        now=now,
        ageing_interval_seconds=ageing_interval_seconds,
        in_flight_ids=in_flight,
        retry_after=retry,
        permanently_blocked_ids=permanently_blocked_ids,
        enable_inheritance=enable_inheritance,
        selection_policy=selection_policy,
    )

    next_id = (
        selected["event_id"]
        if selected is not None
        else None
    )

    rows = []

    for event in snapshot:
        event_id = event["event_id"]

        waiting_on = [
            prereq
            for prereq in event["prerequisite_ids"]
            if prereq not in applied
        ]

        if event_id in applied or event["status"] == "applied":
            reason = "Already applied"

        elif event_id in cycle_blocked:
            reason = "Dependency cycle or downstream of cycle"

        elif event_id in permanent_blocked:
            reason = (
                "Permanently blocked or downstream "
                "of blocked event"
            )

        elif event_id in missing_inheritance_blocked:
            reason = (
                "Missing prerequisite or downstream "
                "of missing event"
            )

        elif event_id in in_flight:
            reason = "In flight"

        elif (
            retry.get(event_id) is not None
            and now < retry[event_id]
        ):
            reason = "Waiting for retry deadline"

        elif waiting_on:
            reason = "Waiting for prerequisite acknowledgement"

        elif event_id in eligible_ids:
            reason = "Eligible"

        else:
            reason = "Not selectable"

        p_effective = effective.get(event_id)

        inherited_from = (
            source.get(event_id)
            if (
                p_effective is not None
                and p_effective > aged[event_id]
            )
            else None
        )

        # Normalise creation timestamps for deterministic
        # sorting in the dashboard.
        created_at = event.get("created_at")

        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            )

        if created_at is None:
            created_at = now

        created_at = created_at.astimezone(timezone.utc)


        rows.append({
            "event_id": event_id,
            "created_at": created_at.isoformat(
                timespec="microseconds"
            ),
            "base_priority": event["base_priority"],
            "aged_priority": aged[event_id],
            "effective_priority": p_effective,
            "inherited_from": inherited_from,
            "prerequisite_ids": list(
                event["prerequisite_ids"]
            ),
            "waiting_on": waiting_on,
            "eligible": event_id in eligible_ids,
            "reason": reason,
            "selected": event_id == next_id,
        })

    return {
        "inheritance_enabled": enable_inheritance,
        "evaluated_at": now.isoformat(),
        "next_event_id": next_id,
        "events": rows,
    }


def _demo():
    """Small standalone preview; does not access the hospital database."""
    events = [
        {
            "event_id": "C",
            "base_priority": 3,
            "prerequisite_ids": [],
            "status": "pending",
        },
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending",
        },
        {
            "event_id": "B",
            "base_priority": 5,
            "prerequisite_ids": ["A"],
            "status": "pending",
        },
    ]

    preview = inspect_scheduler(
        events,
        applied_ids=set(),
    )

    print("CrisisGuard Scheduler Inspector - read-only example")
    print("Capture order: C, A, B")

    print(
        f"{'ID':<7}"
        f"{'Base':<8}"
        f"{'Aged':<8}"
        f"{'Effective':<12}"
        f"{'Eligible':<11}"
        f"{'Inherited from'}"
    )

    for row in preview["events"]:
        print(
            f"{row['event_id']:<7}"
            f"{row['base_priority']:<8}"
            f"{row['aged_priority']:<8}"
            f"{str(row['effective_priority']):<12}"
            f"{str(row['eligible']):<11}"
            f"{row['inherited_from'] or '-'}"
        )

    print(f"Next selected: {preview['next_event_id']}")


if __name__ == "__main__":
    _demo()
