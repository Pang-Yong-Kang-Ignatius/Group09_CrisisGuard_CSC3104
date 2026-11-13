
from copy import deepcopy
from datetime import datetime, timezone

from edge.scheduler.scheduler import select_next_event


def test_missing_prerequisite_does_not_inherit_priority():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    events = [
        {
            "event_id": "ROOT",
            "site_id": "Hospital_A",
            "event_type": "patient_registration",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "event_id": "CHILD",
            "site_id": "Hospital_A",
            "event_type": "patient_registration",
            "base_priority": 9,
            "prerequisite_ids": ["ROOT", "MISSING"],
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "event_id": "OTHER",
            "site_id": "Hospital_A",
            "event_type": "patient_registration",
            "base_priority": 3,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    ]

    # Test A: Missing prerequisite blocks inheritance.
    selected = select_next_event(events, set(), now=now)
    assert selected["event_id"] == "OTHER"

    # Test B: Corrected dependency restores inheritance.
    corrected = deepcopy(events)
    corrected[1]["prerequisite_ids"] = ["ROOT"]

    selected = select_next_event(corrected, set(), now=now)
    assert selected["event_id"] == "ROOT"

    # Test C: Without inheritance, OTHER wins.
    selected = select_next_event(
        corrected,
        set(),
        now=now,
        enable_inheritance=False,
    )
    assert selected["event_id"] == "OTHER"

    # Test D: A previously missing prerequisite is
    # now confirmed applied outside this snapshot.
    selected = select_next_event(
        events,
        {"MISSING"},
        now=now,
    )
    assert selected["event_id"] == "ROOT"
