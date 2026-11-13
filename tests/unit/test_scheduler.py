from edge.scheduler.scheduler import select_next_event
from datetime import datetime, timedelta, timezone

def test_inherited_priority_selection():
    events = [
        {"event_id": "A", "base_priority": 1,
         "prerequisite_ids": [], "status": "pending"},
        {"event_id": "B", "base_priority": 2,
         "prerequisite_ids": ["A"], "status": "pending"},
        {"event_id": "C", "base_priority": 5,
         "prerequisite_ids": ["B"], "status": "pending"},
        {"event_id": "D", "base_priority": 3,
         "prerequisite_ids": [], "status": "pending"}
    ]

    selected = select_next_event(events, set())

    assert selected["event_id"] == "A"


def test_no_eligible_event():
    events = [
        {"event_id": "B", "base_priority": 5,
         "prerequisite_ids": ["A"], "status": "pending"}
    ]

    selected = select_next_event(events, set())

    assert selected is None


def test_select_after_prerequisite_applied():
    events = [
        {"event_id": "A", "base_priority": 1,
         "prerequisite_ids": [], "status": "applied"},
        {"event_id": "B", "base_priority": 5,
         "prerequisite_ids": ["A"], "status": "pending"}
    ]

    selected = select_next_event(events, {"A"})

    assert selected["event_id"] == "B"


def test_oldest_event_wins_equal_priority():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    events = [
        {
            "event_id": "A",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": now.isoformat()
        },
        {
            "event_id": "B",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": (
                now - timedelta(minutes=2)
            ).isoformat()
        }
    ]

    selected = select_next_event(
        events,
        applied_ids=set(),
        now=now
    )

    assert selected["event_id"] == "B"


def test_higher_priority_wins_over_older_event():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    events = [
        {
            "event_id": "A",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": now.isoformat()
        },
        {
            "event_id": "B",
            "base_priority": 2,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": (
                now - timedelta(minutes=5)
            ).isoformat()
        }
    ]

    selected = select_next_event(
        events,
        applied_ids=set(),
        now=now
    )

    assert selected["event_id"] == "A"


def test_in_flight_event_is_not_selected():
    events = [
        {
            "event_id": "A",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending"
        },
        {
            "event_id": "B",
            "base_priority": 2,
            "prerequisite_ids": [],
            "status": "pending"
        }
    ]

    selected = select_next_event(
        events,
        applied_ids=set(),
        in_flight_ids={"A"}
    )

    assert selected["event_id"] == "B"


def test_retry_delay_blocks_event():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    events = [
        {
            "event_id": "A",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending"
        }
    ]

    selected = select_next_event(
        events,
        applied_ids=set(),
        now=now,
        retry_after={
            "A": now + timedelta(seconds=30)
        }
    )

    assert selected is None

def test_event_eligible_after_retry_delay():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    events = [
        {
            "event_id": "A",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending"
        }
    ]

    selected = select_next_event(
        events,
        applied_ids=set(),
        now=now,
        retry_after={
            "A": now - timedelta(seconds=1)
        }
    )

    assert selected["event_id"] == "A"

def test_quarantined_descendant_does_not_boost_priority():
    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending"
        },
        {
            "event_id": "B",
            "base_priority": 5,
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "C",
            "base_priority": 3,
            "prerequisite_ids": [],
            "status": "pending"
        }
    ]

    selected = select_next_event(
        events,
        applied_ids=set(),
        permanently_blocked_ids={"B"}
    )

    assert selected["event_id"] == "C"

def test_cycle_does_not_block_independent_event():
    events = [
        {
            "event_id": "A",
            "base_priority": 5,
            "prerequisite_ids": ["B"],
            "status": "pending",
            "created_at": None
        },
        {
            "event_id": "B",
            "base_priority": 4,
            "prerequisite_ids": ["A"],
            "status": "pending",
            "created_at": None
        },
        {
            "event_id": "C",
            "base_priority": 3,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": None
        }
    ]

    selected = select_next_event(
        events,
        applied_ids=set()
    )

    assert selected is not None
    assert selected["event_id"] == "C"

def test_cyclic_descendant_does_not_boost_priority():
    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": None
        },
        {
            "event_id": "B",
            "base_priority": 10,
            "prerequisite_ids": ["A", "C"],
            "status": "pending",
            "created_at": None
        },
        {
            "event_id": "C",
            "base_priority": 9,
            "prerequisite_ids": ["B"],
            "status": "pending",
            "created_at": None
        },
        {
            "event_id": "D",
            "base_priority": 3,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": None
        }
    ]

    selected = select_next_event(
        events,
        applied_ids=set()
    )

    # A must not inherit B's urgency because
    # B and C form a dependency cycle.
    assert selected is not None
    assert selected["event_id"] == "D"

def test_deep_dependency_chain_does_not_crash():
    """
    A long dependency chain should be scheduled
    without exceeding Python's recursion limit.
    """
    chain_length = 1500

    events = []

    for i in range(chain_length):
        event = {
            "event_id": f"E{i}",
            "base_priority": 5 if i == chain_length - 1 else 1,
            "prerequisite_ids": (
                [f"E{i-1}"] if i > 0 else []
            ),
            "status": "pending",
            "created_at": None
        }

        events.append(event)

    selected = select_next_event(
        events,
        applied_ids=set()
    )

    # E0 is the only event with no prerequisites.
    assert selected is not None
    assert selected["event_id"] == "E0"

def test_deep_chain_inherits_priority_correctly():
    chain_length = 1500

    events = []

    for i in range(chain_length):
        events.append({
            "event_id": f"E{i}",
            "base_priority": (
                10 if i == chain_length - 1 else 1
            ),
            "prerequisite_ids": (
                [f"E{i-1}"] if i > 0 else []
            ),
            "status": "pending",
            "created_at": None
        })

    # Independent event with higher base priority
    # than E0, but lower than E1499.
    events.append({
        "event_id": "X",
        "base_priority": 5,
        "prerequisite_ids": [],
        "status": "pending",
        "created_at": None
    })

    # Reverse input order to verify the algorithm
    # does not depend on event insertion order.
    events.reverse()

    selected = select_next_event(
        events,
        applied_ids=set()
    )

    # E0 should inherit priority 10 from E1499.
    # Therefore, E0 should beat independent event X.
    assert selected is not None
    assert selected["event_id"] == "E0"

def test_deterministic_selection_regardless_of_input_order():
    from itertools import permutations

    events = [
        {
            "event_id": "C",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": "2026-10-15T10:00:00+00:00"
        },
        {
            "event_id": "A",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": "2026-10-15T10:00:00+00:00"
        },
        {
            "event_id": "B",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": "2026-10-15T10:00:00+00:00"
        }
    ]

    # Test every possible ordering of the input list.
    for ordering in permutations(events):
        selected = select_next_event(
            list(ordering),
            applied_ids=set()
        )

        assert selected is not None

        # When priority and time are identical,
        # the smallest event ID must win.
        assert selected["event_id"] == "A"

def test_priority_inheritance_can_be_disabled():
    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": None
        },
        {
            "event_id": "B",
            "base_priority": 10,
            "prerequisite_ids": ["A"],
            "status": "pending",
            "created_at": None
        },
        {
            "event_id": "C",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": None
        }
    ]

    # With inheritance enabled:
    # A inherits priority 10 from B.
    with_inheritance = select_next_event(
        events,
        applied_ids=set(),
        enable_inheritance=True
    )

    assert with_inheritance["event_id"] == "A"

    # With inheritance disabled:
    # A stays at priority 1, so C wins.
    without_inheritance = select_next_event(
        events,
        applied_ids=set(),
        enable_inheritance=False
    )

    assert without_inheritance["event_id"] == "C"

def test_inheritance_modes_with_ageing():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": now.isoformat()
        },
        {
            "event_id": "B",
            "base_priority": 2,
            "prerequisite_ids": ["A"],
            "status": "pending",
            "created_at": (
                now - timedelta(minutes=60)
            ).isoformat()
        },
        {
            "event_id": "C",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": now.isoformat()
        }
    ]

    # B has waited 60 minutes.
    # Its aged priority is 2 + 6 = 8.

    # Inheritance ON:
    # A inherits B's aged priority 8.
    selected_on = select_next_event(
        events,
        applied_ids=set(),
        now=now,
        enable_inheritance=True
    )

    assert selected_on["event_id"] == "A"

    # Inheritance OFF:
    # A has priority 1, C has priority 5.
    selected_off = select_next_event(
        events,
        applied_ids=set(),
        now=now,
        enable_inheritance=False
    )

    assert selected_off["event_id"] == "C"

def test_blocked_events_excluded_in_both_modes():
    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": None
        },
        {
            "event_id": "B",
            "base_priority": 20,
            "prerequisite_ids": ["A"],
            "status": "pending",
            "created_at": None
        },
        {
            "event_id": "C",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": None
        },
        {
            "event_id": "D",
            "base_priority": 50,
            "prerequisite_ids": ["B"],
            "status": "pending",
            "created_at": None
        }
    ]

    # B is quarantined.
    # D depends on B, so D is also blocked.
    # Neither should boost A's priority.

    for inheritance_enabled in [True, False]:
        selected = select_next_event(
            events,
            applied_ids=set(),
            permanently_blocked_ids={"B"},
            enable_inheritance=inheritance_enabled
        )

        assert selected is not None
        assert selected["event_id"] == "C"

def test_randomized_scheduler_against_reference():
    import random
    from datetime import datetime, timedelta, timezone

    rng = random.Random(3104)
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    for case in range(100):
        count = rng.randint(2, 12)
        events = []

        # Generate a random acyclic dependency graph.
        for i in range(count):
            prerequisites = [
                f"E{j:02d}"
                for j in range(i)
                if rng.random() < 0.25
            ]

            waiting_seconds = rng.choice(
                [0, 599, 600, 1200, 3600]
            )

            events.append({
                "event_id": f"E{i:02d}",
                "base_priority": rng.randint(1, 10),
                "prerequisite_ids": prerequisites,
                "status": "pending",
                "created_at": (
                    now - timedelta(seconds=waiting_seconds)
                ).isoformat()
            })

        # Simulate events previously applied in the cloud.
        applied_count = rng.randint(0, min(count, 3))
        applied = {
            f"E{i:02d}" for i in range(applied_count)
        }

        # Randomly quarantine some unfinished events.
        quarantined = {
            event["event_id"]
            for event in events
            if event["event_id"] not in applied
            and rng.random() < 0.15
        }

        # Reference calculation for blocked descendants.
        blocked = set(quarantined)

        changed = True
        while changed:
            changed = False

            for event in events:
                event_id = event["event_id"]

                if any(
                    prerequisite in blocked
                    for prerequisite in event["prerequisite_ids"]
                ):
                    if event_id not in blocked:
                        blocked.add(event_id)
                        changed = True

        # Independently determine eligible events.
        eligible = [
            event
            for event in events
            if event["event_id"] not in applied
            and event["event_id"] not in blocked
            and all(
                prerequisite in applied
                for prerequisite in event["prerequisite_ids"]
            )
        ]

        def reference_priority(start_event, inheritance):
            reachable = {start_event["event_id"]}

            if inheritance:
                changed = True

                while changed:
                    changed = False

                    for event in events:
                        event_id = event["event_id"]

                        if event_id in applied or event_id in blocked:
                            continue

                        if event_id in reachable:
                            continue

                        if any(
                            prerequisite in reachable
                            for prerequisite in event["prerequisite_ids"]
                        ):
                            reachable.add(event_id)
                            changed = True

            # Calculate ageing independently.
            return max(
                event["base_priority"] + int(
                    (
                        now - datetime.fromisoformat(
                            event["created_at"]
                        )
                    ).total_seconds() // 600
                )
                for event in events
                if event["event_id"] in reachable
            )

        # Test both scheduling policies.
        for inheritance in [True, False]:
            expected = min(
                eligible,
                key=lambda event: (
                    -reference_priority(event, inheritance),
                    datetime.fromisoformat(event["created_at"]),
                    event["event_id"]
                ),
                default=None
            )

            # Shuffle input to avoid relying on its order.
            shuffled = events.copy()
            rng.shuffle(shuffled)

            actual = select_next_event(
                shuffled,
                applied_ids=applied,
                now=now,
                permanently_blocked_ids=quarantined,
                enable_inheritance=inheritance
            )

            expected_id = (
                expected["event_id"] if expected else None
            )
            actual_id = (
                actual["event_id"] if actual else None
            )

            assert actual_id == expected_id, (
                f"Case {case}, inheritance={inheritance}: "
                f"expected {expected_id}, got {actual_id}"
            )
