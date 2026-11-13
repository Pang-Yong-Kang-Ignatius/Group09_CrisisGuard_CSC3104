import pytest
from edge.scheduler.dag import DependencyDAG

def test_eligibility():
    events = [
        {
            "event_id": "A",
            "prerequisite_ids": [],
            "status": "pending"
        },
        {
            "event_id": "B",
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "C",
            "prerequisite_ids": [],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    eligible = dag.get_eligible_events(set())

    assert {
        event["event_id"]
        for event in eligible
    } == {"A", "C"}

    eligible = dag.get_eligible_events({"A"})

    assert {
        event["event_id"]
        for event in eligible
    } == {"B", "C"}


def test_cycle_detection():
    events = [
        {
            "event_id": "A",
            "prerequisite_ids": ["B"],
            "status": "pending"
        },
        {
            "event_id": "B",
            "prerequisite_ids": ["A"],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    assert dag.has_cycle() is True


def test_missing_prerequisites():
    events = [
        {
            "event_id": "A",
            "prerequisite_ids": [],
            "status": "pending"
        },
        {
            "event_id": "B",
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "C",
            "prerequisite_ids": ["X"],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    assert dag.get_missing_prerequisites(set()) == {
        "C": ["X"]
    }

    # X was applied previously, so it is not missing.
    assert dag.get_missing_prerequisites({"X"}) == {}

def test_duplicate_event_ids_are_rejected():
    events = [
        {
            "event_id": "A",
            "prerequisite_ids": [],
            "status": "pending"
        },
        {
            "event_id": "A",
            "prerequisite_ids": ["B"],
            "status": "pending"
        }
    ]

    with pytest.raises(ValueError, match="Duplicate event ID"):
        DependencyDAG(events)

def test_self_dependency_detected():
    events = [
        {
            "event_id": "A",
            "prerequisite_ids": ["A"],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    # An event cannot depend on itself.
    assert dag.has_cycle() is True

def test_missing_prerequisite_blocks_only_dependent():
    events = [
        {
            "event_id": "A",
            "prerequisite_ids": ["MISSING_X"],
            "status": "pending"
        },
        {
            "event_id": "B",
            "prerequisite_ids": [],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    # Report the missing prerequisite.
    assert dag.get_missing_prerequisites(set()) == {
        "A": ["MISSING_X"]
    }

    # A is blocked, but independent B can proceed.
    eligible = dag.get_eligible_events(set())

    assert {
        event["event_id"]
        for event in eligible
    } == {"B"}

    # If X is confirmed applied externally,
    # A can now proceed.
    eligible = dag.get_eligible_events({"MISSING_X"})

    assert {
        event["event_id"]
        for event in eligible
    } == {"A", "B"}

def test_cycle_blocks_all_dependent_descendants():
    events = [
        {
            "event_id": "A",
            "prerequisite_ids": ["B"],
            "status": "pending"
        },
        {
            "event_id": "B",
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "D",
            "prerequisite_ids": ["B"],
            "status": "pending"
        },
        {
            "event_id": "C",
            "prerequisite_ids": [],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    blocked = dag.get_cycle_blocked_ids()

    assert blocked == {"A", "B", "D"}

    # Independent event C must not be blocked.
    assert "C" not in blocked

def test_cycle_members_separated_from_blocked_descendants():
    events = [
        {
            "event_id": "A",
            "prerequisite_ids": ["B"],
            "status": "pending"
        },
        {
            "event_id": "B",
            "prerequisite_ids": ["A"],
            "status": "pending"
        },
        {
            "event_id": "D",
            "prerequisite_ids": ["B"],
            "status": "pending"
        },
        {
            "event_id": "E",
            "prerequisite_ids": ["D"],
            "status": "pending"
        },
        {
            "event_id": "C",
            "prerequisite_ids": [],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    # Only A and B actually form the cycle.
    assert dag.get_cycle_event_ids() == {"A", "B"}

    # D and E cannot proceed because they
    # depend on events within that cycle.
    assert dag.get_cycle_blocked_ids() == {
        "A", "B", "D", "E"
    }

def test_cycle_reporting_handles_self_dependency():
    events = [
        {
            "event_id": "S",
            "prerequisite_ids": ["S"],
            "status": "pending"
        },
        {
            "event_id": "T",
            "prerequisite_ids": ["S"],
            "status": "pending"
        },
        {
            "event_id": "U",
            "prerequisite_ids": [],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    # S is the only actual cycle member.
    assert dag.get_cycle_event_ids() == {"S"}

    # T is blocked because it depends on S.
    assert dag.get_cycle_blocked_ids() == {"S", "T"}

    # U remains independent.
    assert "U" not in dag.get_cycle_blocked_ids()


def test_cycle_reporting_handles_multiple_cycles():
    events = [
        {"event_id": "A", "prerequisite_ids": ["B"]},
        {"event_id": "B", "prerequisite_ids": ["A"]},
        {"event_id": "D", "prerequisite_ids": ["B"]},
        {"event_id": "X", "prerequisite_ids": ["Y"]},
        {"event_id": "Y", "prerequisite_ids": ["X"]},
        {"event_id": "Z", "prerequisite_ids": ["Y"]},
        {"event_id": "C", "prerequisite_ids": []}
    ]

    dag = DependencyDAG(events)

    # Two separate cycles: A-B and X-Y.
    assert dag.get_cycle_event_ids() == {
        "A", "B", "X", "Y"
    }

    # D and Z are blocked descendants.
    assert dag.get_cycle_blocked_ids() == {
        "A", "B", "D", "X", "Y", "Z"
    }

    assert dag.has_cycle() is True


def test_cycle_reporting_handles_deep_acyclic_chain():
    chain_length = 2000

    events = [
        {
            "event_id": f"E{i}",
            "prerequisite_ids": (
                [f"E{i - 1}"] if i > 0 else []
            )
        }
        for i in range(chain_length)
    ]

    dag = DependencyDAG(events)

    # Long valid chains must not be mistaken
    # for cycles or cause recursion errors.
    assert dag.has_cycle() is False
    assert dag.get_cycle_event_ids() == set()
    assert dag.get_cycle_blocked_ids() == set()

def test_external_permanent_block_propagates_to_descendants():
    events = [
        {
            "event_id": "A",
            "prerequisite_ids": [],
            "status": "pending"
        },
        {
            "event_id": "B",
            "prerequisite_ids": ["A", "X"],
            "status": "pending"
        },
        {
            "event_id": "C",
            "prerequisite_ids": ["B"],
            "status": "pending"
        }
    ]

    dag = DependencyDAG(events)

    # X is permanently blocked but is not included
    # in the current event snapshot.
    blocked = dag.get_permanently_blocked_ids({"X"})

    # B depends on X, and C depends on B.
    assert blocked == {"X", "B", "C"}

    # A itself is not permanently blocked.
    assert "A" not in blocked
