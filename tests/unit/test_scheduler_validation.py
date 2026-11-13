
from datetime import datetime, timedelta, timezone

import pytest

from edge.scheduler.validation import (
    validate_event_record,
    validate_enable_inheritance,
)
from edge.scheduler.scheduler import select_next_event
from edge.scheduler.session import SchedulingSession


def make_event():
    return {
        "event_id": "EVENT_A",
        "base_priority": 3,
        "prerequisite_ids": ["EVENT_B"],
        "status": "pending",
        "created_at": None,
    }


def make_runnable_event():
    """Create an event with no prerequisite dependencies."""
    event = make_event()
    event["prerequisite_ids"] = []
    return event


# --------------------------------------------------
# Valid event records
# --------------------------------------------------

def test_valid_event_accepted():
    validate_event_record(make_event())


# --------------------------------------------------
# Required field validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "field",
    ["event_id", "base_priority", "prerequisite_ids", "status"],
)
def test_missing_required_field_rejected(field):
    event = make_event()
    del event[field]

    with pytest.raises(ValueError):
        validate_event_record(event)


# --------------------------------------------------
# Event ID validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "event_id",
    ["", "   ", None, 123],
)
def test_invalid_event_id_rejected(event_id):
    event = make_event()
    event["event_id"] = event_id

    with pytest.raises(ValueError):
        validate_event_record(event)


# --------------------------------------------------
# Prerequisite validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "prerequisites",
    [
        None,
        "EVENT_B",
        [""],
        ["   "],
        [123],
        ["EVENT_B", "EVENT_B"],
    ],
)
def test_invalid_prerequisites_rejected(prerequisites):
    event = make_event()
    event["prerequisite_ids"] = prerequisites

    with pytest.raises(ValueError):
        validate_event_record(event)


# --------------------------------------------------
# Base-priority validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "priority",
    ["3", 3.5, True, False, None],
)
def test_invalid_base_priority_rejected(priority):
    event = make_event()
    event["base_priority"] = priority

    with pytest.raises(
        ValueError,
        match="base_priority must be an integer"
    ):
        validate_event_record(event)


def test_higher_integer_base_priority_accepted():
    # The core scheduler supports integer priorities
    # above the Cloud API's initial 1-5 range.
    event = make_event()
    event["base_priority"] = 9

    validate_event_record(event)


# --------------------------------------------------
# Event status validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "status",
    ["", "   ", "unknown", None, 123, "quarantined"],
)
def test_invalid_event_status_rejected(status):
    event = make_event()
    event["status"] = status

    with pytest.raises(
        ValueError,
        match="status must be 'pending' or 'applied'"
    ):
        validate_event_record(event)


@pytest.mark.parametrize(
    "status",
    ["pending", "applied"],
)
def test_valid_event_status_accepted(status):
    event = make_event()
    event["status"] = status

    validate_event_record(event)


# --------------------------------------------------
# Creation timestamp validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "created_at",
    [
        "",
        "invalid-date",
        "2026-10-21T12:00:00",
        datetime(2026, 10, 21, 12, 0),
        123,
        True,
        [],
    ],
)
def test_invalid_creation_timestamp_rejected(created_at):
    event = make_event()
    event["created_at"] = created_at

    with pytest.raises(ValueError):
        validate_event_record(event)


@pytest.mark.parametrize(
    "created_at",
    [
        None,
        "2026-10-21T12:00:00+00:00",
        "2026-10-21T20:00:00+08:00",
        "2026-10-21T12:00:00Z",
        datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
    ],
)
def test_valid_creation_timestamp_accepted(created_at):
    event = make_event()
    event["created_at"] = created_at

    validate_event_record(event)


def test_missing_creation_timestamp_accepted():
    event = make_event()
    del event["created_at"]

    validate_event_record(event)


# --------------------------------------------------
# Stateless scheduler integration tests
# --------------------------------------------------

@pytest.mark.parametrize(
    "field, invalid_value",
    [
        ("event_id", ""),
        ("prerequisite_ids", "EVENT_B"),
        ("prerequisite_ids", ["EVENT_B", "EVENT_B"]),
        ("base_priority", "3"),
        ("base_priority", 3.5),
        ("base_priority", True),
        ("status", "unknown"),
        ("status", ""),
        ("status", None),
        ("created_at", "invalid-date"),
        ("created_at", "2026-10-21T12:00:00"),
        ("created_at", 123),
    ],
)
def test_stateless_scheduler_rejects_invalid_event(
    field,
    invalid_value
):
    event = make_event()
    event[field] = invalid_value

    with pytest.raises(ValueError):
        select_next_event(
            [event],
            applied_ids=set()
        )


# --------------------------------------------------
# SchedulingSession integration tests
# --------------------------------------------------

@pytest.mark.parametrize(
    "field, invalid_value",
    [
        ("event_id", ""),
        ("prerequisite_ids", "EVENT_B"),
        ("prerequisite_ids", ["EVENT_B", "EVENT_B"]),
        ("base_priority", "3"),
        ("base_priority", 3.5),
        ("base_priority", True),
        ("status", "unknown"),
        ("status", ""),
        ("status", None),
        ("created_at", "invalid-date"),
        ("created_at", "2026-10-21T12:00:00"),
        ("created_at", 123),
    ],
)
def test_scheduling_session_rejects_invalid_event(
    field,
    invalid_value
):
    event = make_event()
    event[field] = invalid_value

    with pytest.raises(ValueError):
        SchedulingSession([event])


# --------------------------------------------------
# Evaluation timestamp (now) validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "invalid_now",
    [
        "2026-10-21T12:00:00+00:00",
        datetime(2026, 10, 21, 12, 0),
        123,
        True,
    ],
)
def test_both_schedulers_reject_invalid_now(invalid_now):
    event = make_runnable_event()

    # Stateless scheduler.
    with pytest.raises(ValueError, match="now must be"):
        select_next_event(
            [event],
            applied_ids=set(),
            now=invalid_now
        )

    # Reusable scheduling session.
    session = SchedulingSession([event])

    with pytest.raises(ValueError, match="now must be"):
        session.select_next(
            applied_ids=set(),
            now=invalid_now
        )


def test_both_schedulers_accept_timezone_aware_now():
    now = datetime.fromisoformat(
        "2026-10-21T20:00:00+08:00"
    )

    event = make_runnable_event()

    selected = select_next_event(
        [event],
        applied_ids=set(),
        now=now
    )

    session = SchedulingSession([event])

    session_selected = session.select_next(
        applied_ids=set(),
        now=now
    )

    assert selected["event_id"] == "EVENT_A"
    assert session_selected["event_id"] == "EVENT_A"


# --------------------------------------------------
# Retry deadline input validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "invalid_deadline",
    [
        "2026-10-21T12:00:00+00:00",
        datetime(2026, 10, 21, 12, 0),
        123,
        True,
    ],
)
def test_both_schedulers_reject_invalid_retry_deadline(
    invalid_deadline
):
    event = make_runnable_event()

    retry_after = {
        "EVENT_A": invalid_deadline
    }

    # Stateless scheduler.
    with pytest.raises(
        ValueError,
        match="retry_after deadlines must be"
    ):
        select_next_event(
            [event],
            applied_ids=set(),
            retry_after=retry_after
        )

    # Reusable scheduling session.
    session = SchedulingSession([event])

    with pytest.raises(
        ValueError,
        match="retry_after deadlines must be"
    ):
        session.select_next(
            applied_ids=set(),
            retry_after=retry_after
        )


# --------------------------------------------------
# Retry mapping validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "invalid_retry_after",
    [
        [],
        "invalid",
        {None: datetime(2026, 10, 21, tzinfo=timezone.utc)},
        {"EVENT_A": []},
    ],
)
def test_both_schedulers_reject_invalid_retry_mapping(
    invalid_retry_after
):
    event = make_runnable_event()

    with pytest.raises(ValueError):
        select_next_event(
            [event],
            applied_ids=set(),
            retry_after=invalid_retry_after
        )

    session = SchedulingSession([event])

    with pytest.raises(ValueError):
        session.select_next(
            applied_ids=set(),
            retry_after=invalid_retry_after
        )


# --------------------------------------------------
# Ageing interval validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "invalid_interval",
    [
        0,
        -10,
        "600",
        None,
        True,
        float("nan"),
        float("inf"),
    ],
)
def test_both_schedulers_reject_invalid_ageing_interval(
    invalid_interval
):
    event = make_runnable_event()

    # Stateless scheduler.
    with pytest.raises(ValueError, match="Ageing interval"):
        select_next_event(
            [event],
            applied_ids=set(),
            ageing_interval_seconds=invalid_interval
        )

    # Reusable scheduling session.
    with pytest.raises(ValueError, match="Ageing interval"):
        SchedulingSession(
            [event],
            ageing_interval_seconds=invalid_interval
        )


# --------------------------------------------------
# Scheduler event-ID collection validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "field, invalid_value",
    [
        ("applied_ids", "EVENT_A"),
        ("applied_ids", [None]),
        ("in_flight_ids", "EVENT_A"),
        ("in_flight_ids", [123]),
        ("permanently_blocked_ids", "EVENT_A"),
    ],
)
def test_both_schedulers_reject_invalid_id_collections(
    field,
    invalid_value
):
    event = make_runnable_event()

    arguments = {
        "applied_ids": set(),
        field: invalid_value,
    }

    # Stateless scheduler.
    with pytest.raises(ValueError):
        select_next_event(
            [event],
            **arguments
        )

    # Reusable scheduling session.
    session = SchedulingSession([event])

    with pytest.raises(ValueError):
        session.select_next(
            **arguments
        )


# --------------------------------------------------
# Retry deadline scheduling behavior
# --------------------------------------------------

@pytest.mark.parametrize(
    "offset_seconds, expected_id",
    [
        (30, None),
        (-1, "EVENT_A"),
        (0, "EVENT_A"),
    ],
)
def test_both_schedulers_respect_retry_deadlines(
    offset_seconds,
    expected_id
):
    now = datetime(
        2026, 10, 21,
        tzinfo=timezone.utc
    )

    event = make_runnable_event()

    retry_after = {
        "EVENT_A": now + timedelta(
            seconds=offset_seconds
        )
    }

    # Stateless scheduler.
    selected = select_next_event(
        [event],
        applied_ids=set(),
        now=now,
        retry_after=retry_after
    )

    # Reusable scheduling session.
    session = SchedulingSession([event])

    session_selected = session.select_next(
        applied_ids=set(),
        now=now,
        retry_after=retry_after
    )

    selected_id = (
        selected["event_id"]
        if selected else None
    )

    session_selected_id = (
        session_selected["event_id"]
        if session_selected else None
    )

    assert selected_id == expected_id
    assert session_selected_id == expected_id


# --------------------------------------------------
# No retry restriction
# --------------------------------------------------

def test_both_schedulers_accept_none_retry_deadline():
    event = make_runnable_event()

    retry_after = {
        "EVENT_A": None
    }

    selected = select_next_event(
        [event],
        applied_ids=set(),
        retry_after=retry_after
    )

    session = SchedulingSession([event])

    session_selected = session.select_next(
        applied_ids=set(),
        retry_after=retry_after
    )

    assert selected["event_id"] == "EVENT_A"
    assert session_selected["event_id"] == "EVENT_A"


# --------------------------------------------------
# Inheritance flag validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "invalid_flag",
    ["False", "True", 0, 1, None, [], {}],
)
def test_invalid_inheritance_flag_rejected_directly(invalid_flag):
    with pytest.raises(
        ValueError,
        match="enable_inheritance must be a boolean"
    ):
        validate_enable_inheritance(invalid_flag)


@pytest.mark.parametrize(
    "invalid_flag",
    ["False", "True", 0, 1, None, [], {}],
)
def test_both_schedulers_reject_invalid_inheritance_flag(invalid_flag):
    event = make_runnable_event()

    with pytest.raises(
        ValueError,
        match="enable_inheritance must be a boolean"
    ):
        select_next_event(
            [event],
            applied_ids=set(),
            enable_inheritance=invalid_flag
        )

    with pytest.raises(
        ValueError,
        match="enable_inheritance must be a boolean"
    ):
        SchedulingSession(
            [event],
            enable_inheritance=invalid_flag
        )


@pytest.mark.parametrize(
    "inheritance, expected_event_id",
    [
        (True, "A"),
        (False, "C"),
    ],
)
def test_both_schedulers_accept_boolean_inheritance(
    inheritance, expected_event_id
):
    # B is urgent but depends on A; C is independent.
    # Inheritance ON: A inherits B's urgency and wins.
    # Inheritance OFF: C has the greatest eligible base priority.
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    events = [
        {
            "event_id": "A", "base_priority": 1,
            "prerequisite_ids": [], "status": "pending",
            "created_at": now.isoformat(),
        },
        {
            "event_id": "B", "base_priority": 10,
            "prerequisite_ids": ["A"], "status": "pending",
            "created_at": now.isoformat(),
        },
        {
            "event_id": "C", "base_priority": 5,
            "prerequisite_ids": [], "status": "pending",
            "created_at": now.isoformat(),
        },
    ]

    selected = select_next_event(
        events,
        applied_ids=set(),
        now=now,
        enable_inheritance=inheritance
    )

    session = SchedulingSession(
        events,
        enable_inheritance=inheritance
    )
    session_selected = session.select_next(
        applied_ids=set(),
        now=now
    )

    assert selected["event_id"] == expected_event_id
    assert session_selected["event_id"] == expected_event_id


# --------------------------------------------------
# Event collection validation
# --------------------------------------------------

@pytest.mark.parametrize(
    "invalid_events",
    [
        None,
        "",
        "EVENT_A",
        b"EVENT_A",
        bytearray(b"EVENT_A"),
        memoryview(b"EVENT_A"),
        {"event_id": "EVENT_A"},
        123,
        False,
    ],
)
def test_both_schedulers_reject_invalid_event_collections(invalid_events):
    with pytest.raises(
        ValueError,
        match="events must be an iterable of event records"
    ):
        select_next_event(invalid_events, applied_ids=set())

    with pytest.raises(
        ValueError,
        match="events must be an iterable of event records"
    ):
        SchedulingSession(invalid_events)


@pytest.mark.parametrize("container_type", ["list", "tuple", "generator"])
def test_both_schedulers_accept_valid_event_collections(container_type):
    def create_collection():
        event = make_runnable_event()
        if container_type == "list":
            return [event]
        if container_type == "tuple":
            return (event,)
        return (item for item in [event])

    selected = select_next_event(create_collection(), applied_ids=set())
    session = SchedulingSession(create_collection())
    selected_from_session = session.select_next(applied_ids=set())

    assert selected["event_id"] == "EVENT_A"
    assert selected_from_session["event_id"] == "EVENT_A"


@pytest.mark.parametrize("bad_event", [None, 123, "EVENT_A"])
def test_both_schedulers_reject_non_record_items(bad_event):
    with pytest.raises(ValueError, match="Event must be a dictionary"):
        select_next_event([bad_event], applied_ids=set())

    with pytest.raises(ValueError, match="Event must be a dictionary"):
        SchedulingSession([bad_event])


def test_both_schedulers_accept_empty_event_collection():
    assert select_next_event([], applied_ids=set()) is None
    assert SchedulingSession([]).select_next(applied_ids=set()) is None
