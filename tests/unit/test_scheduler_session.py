
from datetime import datetime, timedelta, timezone

from edge.scheduler.session import SchedulingSession


NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def test_session_preserves_order_in_both_modes():
    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat()
        },
        {
            "event_id": "B",
            "base_priority": 10,
            "prerequisite_ids": ["A"],
            "status": "pending",
            "created_at": NOW.isoformat()
        },
        {
            "event_id": "C",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat()
        }
    ]

    expected_orders = {
        True: ["A", "B", "C"],
        False: ["C", "A", "B"]
    }

    for inheritance in [True, False]:
        session = SchedulingSession(
            events,
            enable_inheritance=inheritance
        )

        applied_ids = set()
        actual_order = []

        for _ in range(len(events)):
            selected = session.select_next(
                applied_ids=applied_ids,
                now=NOW
            )

            assert selected is not None

            event_id = selected["event_id"]

            assert event_id not in applied_ids

            assert all(
                dependency in applied_ids
                for dependency in selected["prerequisite_ids"]
            )

            actual_order.append(event_id)
            applied_ids.add(event_id)

        assert actual_order == expected_orders[inheritance]


def test_session_refreshes_ageing_when_time_changes():
    events = [
        {
            "event_id": "A",
            "base_priority": 3,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat()
        },
        {
            "event_id": "C",
            "base_priority": 2,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": (
                NOW - timedelta(seconds=599)
            ).isoformat()
        }
    ]

    session = SchedulingSession(
        events,
        enable_inheritance=True
    )

    # At NOW, A has priority 3 and C has priority 2.
    first = session.select_next(
        applied_ids=set(),
        now=NOW
    )

    assert first["event_id"] == "A"

    # Two seconds later, C crosses its ageing boundary.
    # Both have priority 3; C is older and wins the tie.
    later = session.select_next(
        applied_ids=set(),
        now=NOW + timedelta(seconds=2)
    )

    assert later["event_id"] == "C"


def test_session_matches_stateless_with_changing_state():
    from edge.scheduler.scheduler import select_next_event

    # Create a graph with several dependency chains.
    specs = [
        ("A", 1, []),
        ("B", 10, ["A"]),
        ("C", 5, []),
        ("D", 15, ["C"]),
        ("E", 6, []),
        ("F", 20, ["E"]),
        ("X", 30, ["Y"]),
        ("Y", 40, ["X"])
    ]

    events = [
        {
            "event_id": event_id,
            "base_priority": priority,
            "prerequisite_ids": prerequisites,
            "status": "pending",
            "created_at": NOW.isoformat()
        }
        for event_id, priority, prerequisites in specs
    ]

    # F is quarantined in every scenario.
    # X and Y form a dependency cycle.
    #
    # Each case specifies:
    # name, applied IDs, in-flight IDs, retry delays,
    # quarantined IDs, expected ON, expected OFF.
    cases = [
        (
            "Normal recovery",
            set(), set(), {}, {"F"}, "C", "E"
        ),
        (
            "C waiting for retry",
            set(), set(),
            {"C": NOW + timedelta(minutes=1)},
            {"F"}, "A", "E"
        ),
        (
            "A in flight",
            set(), {"A"}, {},
            {"F"}, "C", "E"
        ),
        (
            "C already applied",
            {"C"}, set(), {},
            {"F"}, "D", "D"
        ),
        (
            "B also quarantined",
            set(), set(), {},
            {"B", "F"}, "C", "E"
        )
    ]

    for inheritance in [True, False]:
        # Reuse the SAME session as recovery state changes.
        session = SchedulingSession(
            events,
            enable_inheritance=inheritance
        )

        for (
            name,
            applied,
            in_flight,
            retry_after,
            quarantined,
            expected_on,
            expected_off
        ) in cases:

            reference = select_next_event(
                events,
                applied_ids=applied,
                now=NOW,
                in_flight_ids=in_flight,
                retry_after=retry_after,
                permanently_blocked_ids=quarantined,
                enable_inheritance=inheritance
            )

            actual = session.select_next(
                applied_ids=applied,
                now=NOW,
                in_flight_ids=in_flight,
                retry_after=retry_after,
                permanently_blocked_ids=quarantined
            )

            reference_id = (
                reference["event_id"]
                if reference is not None else None
            )

            actual_id = (
                actual["event_id"]
                if actual is not None else None
            )

            expected_id = (
                expected_on if inheritance else expected_off
            )

            assert actual_id == reference_id == expected_id, (
                f"{name}, inheritance={inheritance}: "
                f"expected {expected_id}, "
                f"reference={reference_id}, "
                f"session={actual_id}"
            )


def test_randomized_session_matches_stateless():
    import random

    from edge.scheduler.scheduler import select_next_event

    rng = random.Random(3104)

    for case in range(50):
        count = rng.randint(3, 25)

        events = []

        # Generate an acyclic dependency graph.
        for i in range(count):
            prerequisites = [
                f"E{j:02d}"
                for j in range(i)
                if rng.random() < 0.2
            ]

            events.append({
                "event_id": f"E{i:02d}",
                "base_priority": rng.randint(1, 10),
                "prerequisite_ids": prerequisites,
                "status": "pending",
                "created_at": (
                    NOW - timedelta(
                        seconds=rng.choice(
                            [0, 599, 600, 1800, 3600]
                        )
                    )
                ).isoformat()
            })

        rng.shuffle(events)

        for inheritance in [True, False]:
            # Create the session only once.
            session = SchedulingSession(
                events,
                enable_inheritance=inheritance
            )

            # Reuse it across changing recovery states.
            for snapshot in range(5):
                current_time = (
                    NOW + timedelta(seconds=snapshot * 601)
                )

                applied_count = rng.randint(
                    0, count // 3
                )

                applied_ids = {
                    f"E{i:02d}"
                    for i in range(applied_count)
                }

                remaining_ids = [
                    event["event_id"]
                    for event in events
                    if event["event_id"] not in applied_ids
                ]

                quarantined = {
                    event_id
                    for event_id in remaining_ids
                    if rng.random() < 0.1
                }

                in_flight = {
                    event_id
                    for event_id in remaining_ids
                    if rng.random() < 0.1
                }

                retry_after = {
                    event_id: current_time + timedelta(
                        seconds=rng.choice([-60, 60])
                    )
                    for event_id in remaining_ids
                    if rng.random() < 0.15
                }

                # Original scheduler: correctness baseline.
                reference = select_next_event(
                    events,
                    applied_ids=applied_ids,
                    now=current_time,
                    in_flight_ids=in_flight,
                    retry_after=retry_after,
                    permanently_blocked_ids=quarantined,
                    enable_inheritance=inheritance
                )

                # New reusable scheduler.
                actual = session.select_next(
                    applied_ids=applied_ids,
                    now=current_time,
                    in_flight_ids=in_flight,
                    retry_after=retry_after,
                    permanently_blocked_ids=quarantined
                )

                reference_id = (
                    reference["event_id"]
                    if reference is not None else None
                )

                actual_id = (
                    actual["event_id"]
                    if actual is not None else None
                )

                assert actual_id == reference_id, (
                    f"Graph {case}, state {snapshot}, "
                    f"inheritance={inheritance}: "
                    f"expected {reference_id}, "
                    f"got {actual_id}"
                )


def test_external_blocked_prerequisite_in_both_schedulers():
    from edge.scheduler.scheduler import select_next_event

    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat()
        },
        {
            "event_id": "B",
            "base_priority": 20,
            "prerequisite_ids": ["A", "X"],
            "status": "pending",
            "created_at": NOW.isoformat()
        },
        {
            "event_id": "C",
            "base_priority": 30,
            "prerequisite_ids": ["B"],
            "status": "pending",
            "created_at": NOW.isoformat()
        },
        {
            "event_id": "D",
            "base_priority": 3,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat()
        }
    ]

    # Construct the reusable session once.
    session = SchedulingSession(
        events,
        enable_inheritance=True
    )

    # Change permanent-blockage information without
    # rebuilding the scheduling session.
    scenarios = [
        # X is missing: B and C cannot boost A.
        (set(), set(), "D"),

        # X is permanently blocked.
        (set(), {"X"}, "D"),

        # X has now been applied: inheritance is restored.
        ({"X"}, set(), "A"),

        # Return to the original missing-prerequisite state.
        (set(), set(), "D"),
    ]

    for applied_ids, blocked_ids, expected_id in scenarios:
        original = select_next_event(
            events,
            applied_ids=applied_ids,
            now=NOW,
            permanently_blocked_ids=blocked_ids
        )

        reusable = session.select_next(
            applied_ids=applied_ids,
            now=NOW,
            permanently_blocked_ids=blocked_ids
        )

        assert original is not None
        assert reusable is not None

        assert original["event_id"] == expected_id
        assert reusable["event_id"] == expected_id


def test_session_isolated_from_external_event_mutations():
    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat()
        },
        {
            "event_id": "B",
            "base_priority": 10,
            "prerequisite_ids": ["A"],
            "status": "pending",
            "created_at": NOW.isoformat()
        },
        {
            "event_id": "C",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat()
        }
    ]

    session = SchedulingSession(
        events,
        enable_inheritance=True
    )

    # A initially inherits B's priority of 10.
    first = session.select_next(
        applied_ids=set(),
        now=NOW
    )

    assert first["event_id"] == "A"

    # Modify the original event after session creation.
    events[1]["base_priority"] = 0
    events[1]["prerequisite_ids"].clear()

    # The existing session should retain its original
    # dependency graph and priority information.
    second = session.select_next(
        applied_ids=set(),
        now=NOW
    )

    assert second["event_id"] == "A"


def test_returned_event_cannot_modify_session_snapshot():
    events = [
        {
            "event_id": "A",
            "base_priority": 10,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat()
        },
        {
            "event_id": "B",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat()
        }
    ]

    session = SchedulingSession(
        events,
        enable_inheritance=False
    )

    first = session.select_next(
        applied_ids=set(),
        now=NOW
    )

    assert first["event_id"] == "A"

    # Simulate a caller accidentally changing
    # the returned event dictionary.
    first["base_priority"] = 0
    first["prerequisite_ids"].append("MISSING")

    # These changes must not modify the
    # session's internal event snapshot.
    second = session.select_next(
        applied_ids=set(),
        now=NOW
    )

    assert second["event_id"] == "A"
    assert second["base_priority"] == 10
    assert second["prerequisite_ids"] == []


# --------------------------------------------------
# Applied-event regression tests
# --------------------------------------------------

def test_applied_event_with_missing_history_does_not_block_inheritance():
    """
    An applied event's missing historical prerequisite
    must not suppress urgency in an unfinished workflow.
    """
    from edge.scheduler.scheduler import select_next_event

    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": ["X"],
            "status": "applied",
            "created_at": NOW.isoformat(),
        },
        {
            "event_id": "R",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat(),
        },
        {
            "event_id": "B",
            "base_priority": 10,
            "prerequisite_ids": ["A", "R"],
            "status": "pending",
            "created_at": NOW.isoformat(),
        },
        {
            "event_id": "C",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat(),
        },
    ]

    applied_ids = {"A"}

    reference = select_next_event(
        events,
        applied_ids=applied_ids,
        now=NOW
    )

    session = SchedulingSession(events)

    actual = session.select_next(
        applied_ids=applied_ids,
        now=NOW
    )

    # B is urgent, so unfinished prerequisite R
    # should inherit its priority and win over C.
    assert reference["event_id"] == "R"
    assert actual["event_id"] == reference["event_id"]


def test_permanent_blockage_does_not_pass_through_applied_intermediary():
    """
    A permanently blocked ancestor must not propagate
    blockage through an already-applied intermediate event.
    """
    from edge.scheduler.scheduler import select_next_event

    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": ["X"],
            "status": "applied",
            "created_at": NOW.isoformat(),
        },
        {
            "event_id": "R",
            "base_priority": 1,
            "prerequisite_ids": ["A"],
            "status": "pending",
            "created_at": NOW.isoformat(),
        },
        {
            "event_id": "B",
            "base_priority": 10,
            "prerequisite_ids": ["R"],
            "status": "pending",
            "created_at": NOW.isoformat(),
        },
        {
            "event_id": "C",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": NOW.isoformat(),
        },
    ]

    applied_ids = {"A"}
    blocked_ids = {"X"}

    reference = select_next_event(
        events,
        applied_ids=applied_ids,
        now=NOW,
        permanently_blocked_ids=blocked_ids
    )

    session = SchedulingSession(events)

    actual = session.select_next(
        applied_ids=applied_ids,
        now=NOW,
        permanently_blocked_ids=blocked_ids
    )

    # X is blocked, but A has already been applied.
    # R can still inherit urgency from B and win.
    assert reference["event_id"] == "R"
    assert actual["event_id"] == reference["event_id"]
