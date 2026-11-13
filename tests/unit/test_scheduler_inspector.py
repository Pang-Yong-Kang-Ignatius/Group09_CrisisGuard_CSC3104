
from datetime import datetime, timedelta, timezone

import pytest

from edge.scheduler.inspector import inspect_scheduler
from edge.scheduler.scheduler import select_next_event


NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def event(
    event_id,
    priority,
    prerequisites=None,
    status="pending",
    created_at=None,
):
    row = {
        "event_id": event_id,
        "base_priority": priority,
        "prerequisite_ids": prerequisites or [],
        "status": status,
    }

    if created_at is not None:
        row["created_at"] = created_at

    return row


def lookup(preview, event_id):
    return next(
        row
        for row in preview["events"]
        if row["event_id"] == event_id
    )


def test_inspector_explains_inherited_selection_without_mutation():
    events = [
        event("C", 3),
        event("A", 1),
        event("B", 5, ["A"]),
    ]

    before = [dict(e) for e in events]

    preview = inspect_scheduler(
        events,
        set(),
        now=NOW,
    )

    assert preview["next_event_id"] == "A"
    assert lookup(preview, "A")["effective_priority"] == 5
    assert lookup(preview, "A")["inherited_from"] == "B"
    assert lookup(preview, "A")["selected"] is True
    assert lookup(preview, "B")["eligible"] is False
    assert lookup(preview, "C")["eligible"] is True
    assert events == before


def test_no_inheritance_changes_selection():
    events = [
        event("C", 3),
        event("A", 1),
        event("B", 5, ["A"]),
    ]

    preview = inspect_scheduler(
        events,
        set(),
        now=NOW,
        enable_inheritance=False,
    )

    assert preview["next_event_id"] == "C"
    assert lookup(preview, "A")["effective_priority"] == 1
    assert lookup(preview, "A")["inherited_from"] is None


def test_applied_and_empty_backlogs():
    events = [
        event("A", 1, status="applied"),
        event("B", 5, ["A"]),
    ]

    preview = inspect_scheduler(
        events,
        {"A"},
        now=NOW,
    )

    assert preview["next_event_id"] == "B"
    assert not lookup(preview, "A")["eligible"]

    empty = inspect_scheduler(
        [],
        set(),
        now=NOW,
    )

    assert empty["next_event_id"] is None


def test_retry_and_in_flight_exclusions():
    events = [
        event("A", 2),
        event("B", 5),
        event("C", 4),
    ]

    preview = inspect_scheduler(
        events,
        set(),
        now=NOW,
        in_flight_ids={"B"},
        retry_after={
            "C": NOW + timedelta(minutes=2)
        },
    )

    assert preview["next_event_id"] == "A"

    assert lookup(preview, "B")["reason"] == "In flight"

    assert (
        lookup(preview, "C")["reason"]
        == "Waiting for retry deadline"
    )


def test_missing_and_permanent_priority_excluded():
    events = [
        event("A", 1),
        event("B", 5, ["A", "X"]),
        event("C", 3),
    ]

    preview = inspect_scheduler(
        events,
        set(),
        now=NOW,
    )

    assert preview["next_event_id"] == "C"
    assert lookup(preview, "A")["effective_priority"] == 1
    assert lookup(preview, "B")["effective_priority"] is None

    events = [
        event("A", 1),
        event("B", 5, ["A"]),
        event("C", 3),
    ]

    preview = inspect_scheduler(
        events,
        set(),
        now=NOW,
        permanently_blocked_ids={"B"},
    )

    assert preview["next_event_id"] == "C"
    assert lookup(preview, "B")["effective_priority"] is None


def test_cycle_does_not_break_preview():
    events = [
        event("A", 1, ["B"]),
        event("B", 5, ["A"]),
        event("C", 3),
    ]

    preview = inspect_scheduler(
        events,
        set(),
        now=NOW,
    )

    assert preview["next_event_id"] == "C"
    assert lookup(preview, "A")["effective_priority"] is None
    assert "cycle" in lookup(preview, "A")["reason"].lower()


def test_generator_supported_and_validation_preserved():
    events = (
        event(event_id, 3)
        for event_id in ["A", "B"]
    )

    preview = inspect_scheduler(
        events,
        set(),
        now=NOW,
    )

    assert preview["next_event_id"] == "A"

    with pytest.raises(ValueError, match="base_priority"):
        inspect_scheduler(
            [event("A", "bad")],
            set(),
            now=NOW,
        )

    with pytest.raises(ValueError, match="Duplicate"):
        inspect_scheduler(
            [
                event("A", 1),
                event("A", 3),
            ],
            set(),
            now=NOW,
        )


def test_inspector_agrees_with_real_scheduler_for_states():
    events = [
        event("C", 4),
        event("A", 1),
        event("B", 5, ["A"]),
    ]

    for inheritance in (True, False):
        for applied in (
            set(),
            {"A"},
            {"A", "B"},
        ):
            for in_flight in (
                set(),
                {"C"},
            ):
                preview = inspect_scheduler(
                    events,
                    applied,
                    now=NOW,
                    enable_inheritance=inheritance,
                    in_flight_ids=in_flight,
                )

                selected = select_next_event(
                    events,
                    applied,
                    now=NOW,
                    enable_inheritance=inheritance,
                    in_flight_ids=in_flight,
                )

                expected = (
                    selected["event_id"]
                    if selected is not None
                    else None
                )

                assert preview["next_event_id"] == expected


def test_equal_priority_oldest_event_selected():
    # Z was created earlier than A, despite its later ID.
    events = [
        event(
            "A", 3,
            created_at=NOW - timedelta(minutes=2)
        ),
        event(
            "Z", 3,
            created_at=NOW - timedelta(minutes=8)
        ),
    ]

    preview = inspect_scheduler(events, set(), now=NOW)

    assert lookup(preview, "A")["effective_priority"] == 3
    assert lookup(preview, "Z")["effective_priority"] == 3
    assert preview["next_event_id"] == "Z"

    assert (
        lookup(preview, "Z")["created_at"]
        < lookup(preview, "A")["created_at"]
    )


def test_equal_priority_and_time_uses_event_id():
    same_time = NOW - timedelta(minutes=1)

    events = [
        event("Z", 3, created_at=same_time),
        event("A", 3, created_at=same_time),
    ]

    preview = inspect_scheduler(events, set(), now=NOW)

    # Same priority and timestamp: A comes before Z.
    assert preview["next_event_id"] == "A"


def test_multilevel_inheritance_explains_original_source():
    # C depends on B, which depends on A.
    events = [
        event("D", 3),
        event("A", 1),
        event("B", 2, ["A"]),
        event("C", 5, ["B"]),
    ]

    preview = inspect_scheduler(events, set(), now=NOW)

    assert preview["next_event_id"] == "A"

    # Both A and B inherit urgency originating from C.
    assert lookup(preview, "A")["effective_priority"] == 5
    assert lookup(preview, "B")["effective_priority"] == 5

    assert lookup(preview, "A")["inherited_from"] == "C"
    assert lookup(preview, "B")["inherited_from"] == "C"

    assert lookup(preview, "C")["eligible"] is False


def test_multiple_descendants_highest_priority_wins():
    # Both B and C depend on A.
    events = [
        event("A", 1),
        event("B", 4, ["A"]),
        event("C", 5, ["A"]),
        event("D", 3),
    ]

    preview = inspect_scheduler(events, set(), now=NOW)

    assert preview["next_event_id"] == "A"

    # A inherits from C (priority 5), not B (priority 4).
    assert lookup(preview, "A")["effective_priority"] == 5
    assert lookup(preview, "A")["inherited_from"] == "C"


def test_equal_urgent_descendants_deterministic_source():
    # C and D have equal priority and both depend on A.
    events = [
        event("A", 1),
        event("D", 5, ["A"]),
        event("C", 5, ["A"]),
    ]

    preview = inspect_scheduler(events, set(), now=NOW)

    assert preview["next_event_id"] == "A"
    assert lookup(preview, "A")["effective_priority"] == 5

    # When multiple descendants contribute equal urgency,
    # the Inspector consistently chooses the smaller ID.
    assert lookup(preview, "A")["inherited_from"] == "C"
