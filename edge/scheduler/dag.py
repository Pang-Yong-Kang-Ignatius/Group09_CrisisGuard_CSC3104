
from collections import deque
from datetime import datetime, timezone


class DependencyDAG:
    """
    Dependency graph for hospital event scheduling.

    Each directed edge represents:
        prerequisite -> dependent event

    The graph supports dependency validation, cycle
    detection, eligibility checking, and propagation
    of permanent blockage.
    """

    def __init__(self, events):
        # Store events using unique event IDs.
        self.events = {}

        for event in events:
            event_id = event["event_id"]

            if event_id in self.events:
                raise ValueError(
                    f"Duplicate event ID: {event_id}"
                )

            self.events[event_id] = event

        # Graph contains only events in this snapshot.
        self.graph = {
            event_id: []
            for event_id in self.events
        }

        self.build_graph()

    def build_graph(self):
        """
        Build prerequisite -> dependent edges.

        Prerequisites outside the current snapshot are
        not inserted into the internal graph.
        """
        for event in self.events.values():
            event_id = event["event_id"]

            for prerequisite in event["prerequisite_ids"]:
                if prerequisite in self.graph:
                    self.graph[prerequisite].append(event_id)

    def get_permanently_blocked_ids(
        self,
        permanently_blocked_ids=None
    ):
        """
        Return permanently blocked IDs and all descendants
        that depend directly or indirectly on them.

        Supports blocked prerequisites that are absent
        from the current event snapshot.

        Runs in O(V + E) time.
        """
        blocked = set(permanently_blocked_ids or ())

        if not blocked:
            return blocked

        # Build a dependency lookup including references
        # to prerequisites outside the current DAG.
        all_dependents = {}

        for event in self.events.values():
            event_id = event["event_id"]

            for prerequisite in event["prerequisite_ids"]:
                all_dependents.setdefault(
                    prerequisite, []
                ).append(event_id)

        # Propagate permanent blockage through descendants.
        stack = list(blocked)

        while stack:
            event_id = stack.pop()

            for dependent_id in all_dependents.get(
                event_id, ()
            ):
                if dependent_id not in blocked:
                    blocked.add(dependent_id)
                    stack.append(dependent_id)

        return blocked

    def get_cycle_blocked_ids(self):
        """
        Return cycle members and their blocked descendants.

        Uses iterative Kahn topological sorting.
        """
        indegree = {
            event_id: 0
            for event_id in self.graph
        }

        for dependents in self.graph.values():
            for dependent_id in dependents:
                indegree[dependent_id] += 1

        ready = deque(
            event_id
            for event_id, degree in indegree.items()
            if degree == 0
        )

        while ready:
            event_id = ready.popleft()

            for dependent_id in self.graph[event_id]:
                indegree[dependent_id] -= 1

                if indegree[dependent_id] == 0:
                    ready.append(dependent_id)

        # Any remaining node is either in a cycle
        # or depends on one.
        return {
            event_id
            for event_id, degree in indegree.items()
            if degree > 0
        }

    def get_cycle_event_ids(self):
        """
        Return only events that participate in cycles.

        Uses iterative Kosaraju strongly connected
        components detection.

        Runs in O(V + E) time without recursion.
        """

        # Step 1: Determine DFS finishing order.
        visited = set()
        finish_order = []

        for start in self.graph:
            if start in visited:
                continue

            visited.add(start)

            stack = [
                (start, iter(self.graph[start]))
            ]

            while stack:
                event_id, neighbors = stack[-1]

                try:
                    dependent_id = next(neighbors)

                except StopIteration:
                    finish_order.append(event_id)
                    stack.pop()
                    continue

                if dependent_id not in visited:
                    visited.add(dependent_id)

                    stack.append(
                        (
                            dependent_id,
                            iter(self.graph[dependent_id])
                        )
                    )

        # Step 2: Reverse all internal dependency edges.
        reversed_graph = {
            event_id: []
            for event_id in self.graph
        }

        for event_id, dependents in self.graph.items():
            for dependent_id in dependents:
                reversed_graph[dependent_id].append(
                    event_id
                )

        # Step 3: Discover strongly connected components.
        visited.clear()
        cycle_ids = set()

        for start in reversed(finish_order):
            if start in visited:
                continue

            component = []
            stack = [start]
            visited.add(start)

            while stack:
                event_id = stack.pop()
                component.append(event_id)

                for neighbor in reversed_graph[event_id]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        stack.append(neighbor)

            # A component of multiple vertices is cyclic.
            if len(component) > 1:
                cycle_ids.update(component)

            # A single vertex is cyclic only with a self-edge.
            elif component[0] in self.graph[component[0]]:
                cycle_ids.add(component[0])

        return cycle_ids

    def has_cycle(self):
        """
        Return True if the dependency graph contains a cycle.
        """
        return bool(self.get_cycle_blocked_ids())

    def get_missing_prerequisites(self, applied_ids):
        """
        Report prerequisites that are absent from the
        current snapshot and not confirmed applied.
        """
        missing = {}

        for event in self.events.values():
            unresolved = [
                prerequisite
                for prerequisite in event["prerequisite_ids"]
                if prerequisite not in self.events
                and prerequisite not in applied_ids
            ]

            if unresolved:
                missing[event["event_id"]] = unresolved

        return missing

    def get_eligible_events(
        self,
        applied_ids,
        in_flight_ids=None,
        retry_after=None,
        now=None
    ):
        """
        Return pending events eligible for scheduling.

        An event must have all prerequisites confirmed
        applied and must not be in flight or waiting
        for a retry deadline.

        Permanent and cycle-related exclusions are
        supplied by the scheduler through in_flight_ids.
        """
        if in_flight_ids is None:
            in_flight_ids = set()

        if retry_after is None:
            retry_after = {}

        if now is None:
            now = datetime.now(timezone.utc)

        eligible = []

        for event in self.events.values():
            event_id = event["event_id"]

            # Exclude events already applied.
            if event_id in applied_ids:
                continue

            # Only pending events may be selected.
            if event["status"] != "pending":
                continue

            # Exclude temporarily or otherwise unavailable IDs.
            if event_id in in_flight_ids:
                continue

            # Respect retry deadlines.
            next_retry = retry_after.get(event_id)

            if next_retry is not None and now < next_retry:
                continue

            # All prerequisites must be centrally acknowledged.
            if all(
                prerequisite in applied_ids
                for prerequisite in event["prerequisite_ids"]
            ):
                eligible.append(event)

        return eligible
