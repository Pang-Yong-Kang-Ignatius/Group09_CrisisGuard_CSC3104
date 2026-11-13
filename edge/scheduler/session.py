
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone

from edge.scheduler.dag import DependencyDAG
from edge.scheduler.priority import calculate_aged_priority
from edge.scheduler.validation import (
    validate_event_collection,
    validate_event_records,
    validate_ageing_interval,
    validate_scheduler_arguments,
    validate_enable_inheritance,
)


class SchedulingSession:
    """
    Reusable dependency-aware scheduling session.

    Builds the dependency graph once and supports
    changing acknowledgements, retry availability,
    quarantine information, and priority ageing.
    """

    def __init__(
        self,
        events,
        enable_inheritance=True,
        ageing_interval_seconds=600
    ):
        # Validate the ageing interval.
        validate_ageing_interval(ageing_interval_seconds)
        validate_enable_inheritance(enable_inheritance)

        # Validate the event container before creating the snapshot.
        validate_event_collection(events)

        # Create an independent snapshot of event records.
        # This prevents external modifications from changing
        # the session's dependency graph or event priorities.
        snapshot = [
            deepcopy(event)
            for event in events
        ]

        # Validate all event records before constructing the DAG.
        validate_event_records(snapshot)

        # Build and validate the dependency graph once.
        self.dag = DependencyDAG(snapshot)

        # Cache dependents of prerequisites outside the snapshot.
        # Internal edges are already stored in self.dag.graph.
        self.external_dependents = {}
        for event in snapshot:
            for prerequisite_id in event["prerequisite_ids"]:
                if prerequisite_id not in self.dag.events:
                    self.external_dependents.setdefault(
                        prerequisite_id, []
                    ).append(event["event_id"])

        self.enable_inheritance = enable_inheritance
        self.ageing_interval_seconds = ageing_interval_seconds

    def _propagate_blocked_ids(self, initial_ids, active_ids):
        """
        Expand blocked IDs through unfinished events only.

        An already-applied event can be an explicitly blocked seed,
        but cannot be an intermediate step in propagation. This
        matches the stateless scheduler's unfinished-event graph.
        """
        blocked = set(initial_ids or ())
        stack = list(blocked)

        while stack:
            event_id = stack.pop()
            dependents = (
                self.dag.graph.get(event_id, ())
                if event_id in self.dag.events
                else self.external_dependents.get(event_id, ())
            )

            for dependent_id in dependents:
                if dependent_id in active_ids and dependent_id not in blocked:
                    blocked.add(dependent_id)
                    stack.append(dependent_id)

        return blocked

    def select_next(
        self,
        applied_ids,
        now=None,
        in_flight_ids=None,
        retry_after=None,
        permanently_blocked_ids=None
    ):
        # Validate scheduling arguments before selection.
        validate_scheduler_arguments(
            applied_ids=applied_ids,
            now=now,
            in_flight_ids=in_flight_ids,
            retry_after=retry_after,
            permanently_blocked_ids=permanently_blocked_ids
        )

        if now is None:
            now = datetime.now(timezone.utc)

        applied_ids = set(applied_ids)

        # Step 1: Identify unfinished events.
        active_ids = {
            event_id
            for event_id in self.dag.events
            if event_id not in applied_ids
        }

        # Step 2: Calculate a topological order
        # using the existing, reusable graph.
        indegree = {
            event_id: 0
            for event_id in active_ids
        }

        for event_id in active_ids:
            for dependent_id in self.dag.graph[event_id]:
                if dependent_id in active_ids:
                    indegree[dependent_id] += 1

        ready = deque(
            event_id
            for event_id in self.dag.events
            if event_id in active_ids
            and indegree[event_id] == 0
        )

        topological_order = []

        while ready:
            event_id = ready.popleft()
            topological_order.append(event_id)

            for dependent_id in self.dag.graph[event_id]:
                if dependent_id not in active_ids:
                    continue

                indegree[dependent_id] -= 1

                if indegree[dependent_id] == 0:
                    ready.append(dependent_id)

        # Unprocessed events belong to a cycle
        # or depend on a cycle.
        cycle_blocked_ids = (
            active_ids - set(topological_order)
        )

        # Step 3: Handle permanent and temporary blocks.
        permanent_ids = self._propagate_blocked_ids(
            permanently_blocked_ids,
            active_ids
        )

        blocked_ids = cycle_blocked_ids | permanent_ids

        unavailable_ids = (
            blocked_ids | set(in_flight_ids or ())
        )

        # Step 4: Determine eligible events.
        eligible = self.dag.get_eligible_events(
            applied_ids,
            in_flight_ids=unavailable_ids,
            retry_after=retry_after,
            now=now
        )

        if not eligible:
            return None

        # Step 5: Calculate priorities.
        if self.enable_inheritance:
            # Identify events with unresolved missing prerequisites.
            # Only unfinished events are considered.
            missing_ids = {
                event_id
                for event_id in active_ids
                if any(
                    prerequisite_id not in active_ids
                    and prerequisite_id not in applied_ids
                    for prerequisite_id in self.dag.events[event_id][
                        "prerequisite_ids"
                    ]
                )
            }

            # Exclude those events and their descendants
            # from contributing inherited urgency.
            # This does not permanently quarantine them.
            missing_inheritance_ids = self._propagate_blocked_ids(
                missing_ids,
                active_ids
            )

            inheritance_excluded_ids = (
                blocked_ids | missing_inheritance_ids
            )

            effective = {}

            # Descendants are processed before
            # their prerequisites.
            for event_id in reversed(topological_order):
                event = self.dag.events[event_id]

                if (
                    event_id in inheritance_excluded_ids
                    or event["status"] == "applied"
                ):
                    continue

                priority = calculate_aged_priority(
                    event,
                    now=now,
                    ageing_interval_seconds=(
                        self.ageing_interval_seconds
                    )
                )

                for dependent_id in self.dag.graph[event_id]:
                    if dependent_id in effective:
                        priority = max(
                            priority,
                            effective[dependent_id]
                        )

                effective[event_id] = priority

        else:
            # Without inheritance, only the event's
            # own aged priority matters.
            effective = {
                event["event_id"]: calculate_aged_priority(
                    event,
                    now=now,
                    ageing_interval_seconds=(
                        self.ageing_interval_seconds
                    )
                )
                for event in eligible
            }

        # Step 6: Deterministic selection.
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
                -effective[event["event_id"]],
                created_at,
                event["event_id"]
            )

        # Return an independent copy so callers cannot
        # modify the session's internal event snapshot.
        return deepcopy(min(eligible, key=sort_key))
