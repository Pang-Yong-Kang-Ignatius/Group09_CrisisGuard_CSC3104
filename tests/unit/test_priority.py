
import pytest

from edge.scheduler.dag import DependencyDAG
from edge.scheduler.priority import calculate_effective_priorities


def test_multilevel_priority_inheritance():
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

    dag = DependencyDAG(events)
    priorities = calculate_effective_priorities(dag, set())

    assert priorities == {
        "A": 5,
        "B": 5,
        "C": 5,
        "D": 3
    }


def test_applied_events_are_excluded():
    events = [
        {"event_id": "A", "base_priority": 1,
         "prerequisite_ids": [], "status": "applied"},
        {"event_id": "B", "base_priority": 5,
         "prerequisite_ids": ["A"], "status": "pending"}
    ]

    dag = DependencyDAG(events)
    priorities = calculate_effective_priorities(dag, {"A"})

    assert priorities == {"B": 5}


def test_priority_rejects_cycles():
    events = [
        {"event_id": "A", "base_priority": 1,
         "prerequisite_ids": ["B"], "status": "pending"},
        {"event_id": "B", "base_priority": 5,
         "prerequisite_ids": ["A"], "status": "pending"}
    ]

    dag = DependencyDAG(events)

    with pytest.raises(ValueError):
        calculate_effective_priorities(dag, set())


from datetime import datetime, timedelta, timezone


def test_inheritance_of_aged_priority():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

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
                now - timedelta(minutes=30)
            ).isoformat()
        },
        {
            "event_id": "C",
            "base_priority": 4,
            "prerequisite_ids": [],
            "status": "pending",
            "created_at": now.isoformat()
        }
    ]

    dag = DependencyDAG(events)

    priorities = calculate_effective_priorities(
        dag,
        applied_ids=set(),
        now=now,
        ageing_interval_seconds=600
    )

    assert priorities == {
        "A": 5,
        "B": 5,
        "C": 4
    }


def test_multiple_descendants_with_quarantine():
    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending"
        },
        {
            "event_id": "B",
            "base_priority": 4,
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "C",
            "base_priority": 10,
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "D",
            "base_priority": 3,
            "prerequisite_ids": ["B"],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    priorities = calculate_effective_priorities(
        dag,
        applied_ids=set(),
        permanently_blocked_ids={"C"}
    )

    assert priorities == {
        "A": 4,
        "B": 4,
        "D": 3
    }


def test_multilevel_permanent_blockage():
    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending"
        },
        {
            "event_id": "B",
            "base_priority": 3,
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "C",
            "base_priority": 9,
            "prerequisite_ids": ["B"],
            "status": "pending"
        },
        {
            "event_id": "D",
            "base_priority": 4,
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "E",
            "base_priority": 5,
            "prerequisite_ids": [],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    blocked = dag.get_permanently_blocked_ids({"B"})

    # C depends on B, so C is also blocked.
    assert blocked == {"B", "C"}

    priorities = calculate_effective_priorities(
        dag,
        applied_ids=set(),
        permanently_blocked_ids={"B"}
    )

    assert priorities == {
        "A": 4,
        "D": 4,
        "E": 5
    }


def test_diamond_dependency_priority_inheritance():
    events = [
        {
            "event_id": "A",
            "base_priority": 1,
            "prerequisite_ids": [],
            "status": "pending"
        },
        {
            "event_id": "B",
            "base_priority": 2,
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "C",
            "base_priority": 3,
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "D",
            "base_priority": 8,
            "prerequisite_ids": ["B", "C"],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    priorities = calculate_effective_priorities(
        dag,
        applied_ids=set()
    )

    assert priorities == {
        "A": 8,
        "B": 8,
        "C": 8,
        "D": 8
    }
