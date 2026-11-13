
from collections import deque
from datetime import datetime, timezone

from edge.scheduler.validation import (
    validate_ageing_interval,
    validate_evaluation_time,
    validate_scheduler_arguments,
    validate_timestamp,
)

def calculate_aged_priority(
    event,
    now=None,
    ageing_interval_seconds=600
):
    """
    Calculate an event's time-aged priority.

    Priority increases by 1 for every completed
    ageing interval.

    Events without creation timestamps retain
    their original base priority.

    Future timestamps do not reduce priority.
    """
    validate_ageing_interval(ageing_interval_seconds)
    validate_evaluation_time(now)

    base_priority = event["base_priority"]
    created_at = event.get("created_at")
    validate_timestamp(created_at, "created_at")

    # Events without timestamps do not receive ageing.
    if created_at is None:
        return base_priority

    if now is None:
        now = datetime.now(timezone.utc)

    # Convert ISO 8601 timestamp strings to datetime.
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(
            created_at.replace("Z", "+00:00")
        )

    # Require timezone-aware timestamps.
    if created_at.tzinfo is None or now.tzinfo is None:
        raise ValueError(
            "Timestamps must include timezone information"
        )

    # Prevent negative waiting time for future events.
    waiting_seconds = max(
        0,
        (now - created_at).total_seconds()
    )

    # Increase priority for every completed interval.
    ageing_increase = int(
        waiting_seconds // ageing_interval_seconds
    )

    return base_priority + ageing_increase


def calculate_effective_priorities(
    dag,
    applied_ids,
    now=None,
    ageing_interval_seconds=600,
    permanently_blocked_ids=None
):
    """
    Calculate dependency-aware effective priorities.

    An unfinished event inherits the maximum aged
    priority of itself and its unfinished, non-blocked
    descendants.

    Applied and permanently blocked events do not
    contribute inherited urgency.

    Temporary retry delays and in-flight states do not
    affect inheritance; they affect eligibility instead.

    Uses iterative topological sorting to avoid
    recursion-depth limitations.

    Time complexity: O(V + E).
    """

    # Validate configuration before processing the graph.
    validate_ageing_interval(ageing_interval_seconds)
    validate_scheduler_arguments(
        applied_ids=applied_ids,
        now=now,
        permanently_blocked_ids=permanently_blocked_ids,
    )

    if now is None:
        now = datetime.now(timezone.utc)

    # Identify permanently blocked events and descendants.
    blocked_ids = dag.get_permanently_blocked_ids(
        permanently_blocked_ids
    )

    # Step 1: Calculate the number of internal
    # prerequisites for each event.
    indegree = {
        event_id: 0
        for event_id in dag.events
    }

    for dependents in dag.graph.values():
        for dependent_id in dependents:
            indegree[dependent_id] += 1

    # Step 2: Start with events that have
    # no internal prerequisites.
    ready = deque(
        event_id
        for event_id, degree in indegree.items()
        if degree == 0
    )

    topological_order = []

    # Process the graph iteratively.
    while ready:
        event_id = ready.popleft()
        topological_order.append(event_id)

        for dependent_id in dag.graph[event_id]:
            indegree[dependent_id] -= 1

            if indegree[dependent_id] == 0:
                ready.append(dependent_id)

    # A valid DAG must allow every event to
    # appear in a topological ordering.
    if len(topological_order) != len(dag.events):
        raise ValueError(
            "Cannot calculate priorities: DAG contains a cycle"
        )

    # Step 3: Calculate inherited priorities
    # in reverse topological order.
    effective = {}

    for event_id in reversed(topological_order):
        event = dag.events[event_id]

        # Applied and permanently blocked events
        # must not contribute inherited urgency.
        if (
            event_id in applied_ids
            or event["status"] == "applied"
            or event_id in blocked_ids
        ):
            continue

        # Start with the event's own aged priority.
        priority = calculate_aged_priority(
            event,
            now=now,
            ageing_interval_seconds=ageing_interval_seconds
        )

        # Inherit the highest priority from
        # unfinished, non-blocked descendants.
        for dependent_id in dag.graph[event_id]:
            if dependent_id in effective:
                priority = max(
                    priority,
                    effective[dependent_id]
                )

        effective[event_id] = priority

    return effective
