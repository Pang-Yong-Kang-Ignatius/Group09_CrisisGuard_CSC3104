
import sqlite3
import pytest
from unittest.mock import patch

from edge.database import db
from edge.capture_api.scheduled_replay import replay_scheduled_events


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Use a temporary SQLite database for each test."""
    database_path = tmp_path / "hospital_test.db"
    monkeypatch.setattr(db, "DB_PATH", database_path)
    db.init_db()
    return database_path


def create_event(event_id, priority, prerequisites=None):
    return {
        "event_id": event_id,
        "site_id": "hospital-a",
        "event_type": "patient_registration",
        "base_priority": priority,
        "prerequisite_ids": prerequisites or [],
        "payload": {"patient_id": f"P_{event_id}"}
    }


def get_status(event_id):
    events = db.get_events()
    return next(
        e["status"] for e in events
        if e["event_id"] == event_id
    )


def get_recovery_state(event_id):
    with sqlite3.connect(db.DB_PATH) as conn:
        return conn.execute("""
            SELECT in_flight, retry_count,
                   retry_after, claim_token
            FROM recovery_state
            WHERE event_id = ?
        """, (event_id,)).fetchone()


def test_dependency_priority_replay(isolated_db):
    db.save_event(create_event("A", 1))
    db.save_event(create_event("B", 5, ["A"]))
    db.save_event(create_event("C", 3))

    sent_order = []

    def mock_send(event):
        sent_order.append(event["event_id"])
        return {
            "status": "applied",
            "event_id": event["event_id"]
        }

    with patch(
        "edge.capture_api.scheduled_replay.send_to_cloud",
        side_effect=mock_send
    ):
        results = replay_scheduled_events()

    assert sent_order == ["A", "B", "C"]
    assert all(r["status"] == "applied" for r in results)

    for event_id in ["A", "B", "C"]:
        assert get_status(event_id) == "applied"
        state = get_recovery_state(event_id)
        assert state[0] == 0
        assert state[3] is None


def test_failed_prerequisite_blocks_descendant(isolated_db):
    db.save_event(create_event("A", 1))
    db.save_event(create_event("B", 5, ["A"]))
    db.save_event(create_event("C", 3))

    sent_order = []

    def mock_send(event):
        event_id = event["event_id"]
        sent_order.append(event_id)

        if event_id == "A":
            return {"status": "connection_failed"}

        return {
            "status": "applied",
            "event_id": event_id
        }

    with patch(
        "edge.capture_api.scheduled_replay.send_to_cloud",
        side_effect=mock_send
    ):
        results = replay_scheduled_events()

    assert sent_order == ["A", "C"]
    assert get_status("A") == "pending"
    assert get_status("B") == "pending"
    assert get_status("C") == "applied"

    state_a = get_recovery_state("A")
    assert state_a[0] == 0
    assert state_a[1] == 1
    assert state_a[2] is not None

    assert 1 <= results[0]["retry_delay_seconds"] <= 2


def test_incorrect_ack_does_not_apply_event(isolated_db):
    db.save_event(create_event("A", 1))
    db.save_event(create_event("B", 5, ["A"]))

    sent_order = []

    def mock_send(event):
        sent_order.append(event["event_id"])
        return {
            "status": "applied",
            "event_id": "WRONG_EVENT"
        }

    with patch(
        "edge.capture_api.scheduled_replay.send_to_cloud",
        side_effect=mock_send
    ):
        results = replay_scheduled_events()

    assert sent_order == ["A"]
    assert get_status("A") == "pending"
    assert get_status("B") == "pending"

    assert results[0]["status"] == "pending"
    assert get_recovery_state("A")[1] == 1


def test_active_claim_is_skipped(isolated_db):
    db.save_event(create_event("A", 5))
    db.save_event(create_event("B", 3))

    token = db.claim_event("A")
    assert token is not None

    sent_order = []

    def mock_send(event):
        sent_order.append(event["event_id"])
        return {
            "status": "applied",
            "event_id": event["event_id"]
        }

    with patch(
        "edge.capture_api.scheduled_replay.send_to_cloud",
        side_effect=mock_send
    ):
        replay_scheduled_events()

    assert sent_order == ["B"]
    assert get_status("A") == "pending"
    assert get_status("B") == "applied"

    # Another worker still owns A.
    assert get_recovery_state("A")[3] == token


def test_retry_deadline_is_respected(isolated_db):
    db.save_event(create_event("A", 5))
    db.save_event(create_event("B", 3))

    token = db.claim_event("A")
    assert token is not None
    assert 1 <= db.record_failed_attempt("A", token) <= 2

    sent_order = []

    def mock_send(event):
        sent_order.append(event["event_id"])
        return {
            "status": "applied",
            "event_id": event["event_id"]
        }

    with patch(
        "edge.capture_api.scheduled_replay.send_to_cloud",
        side_effect=mock_send
    ):
        replay_scheduled_events()

    assert sent_order == ["B"]
    assert get_status("A") == "pending"

    # Simulate the retry deadline having passed.
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("""
            UPDATE recovery_state
            SET retry_after = ?
            WHERE event_id = ?
        """, ("2000-01-01T00:00:00+00:00", "A"))

    with patch(
        "edge.capture_api.scheduled_replay.send_to_cloud",
        side_effect=mock_send
    ):
        replay_scheduled_events()

    assert sent_order == ["B", "A"]
    assert get_status("A") == "applied"

def test_invalid_event_is_quarantined(isolated_db):
    db.save_event(create_event("A", 1))
    db.save_event(create_event("B", 5, ["A"]))
    db.save_event(create_event("C", 3))

    sent_order = []

    def mock_send(event):
        event_id = event["event_id"]
        sent_order.append(event_id)

        if event_id == "A":
            return {
                "status": "http_error",
                "status_code": 422,
                "error": "Invalid site_id"
            }

        return {
            "status": "applied",
            "event_id": event_id
        }

    with patch(
        "edge.capture_api.scheduled_replay.send_to_cloud",
        side_effect=mock_send
    ):
        results = replay_scheduled_events()

    assert sent_order == ["A", "C"]

    assert get_status("A") == "pending"
    assert get_status("B") == "pending"
    assert get_status("C") == "applied"

    with sqlite3.connect(db.DB_PATH) as conn:
        state = conn.execute("""
            SELECT quarantined, quarantine_reason
            FROM recovery_state
            WHERE event_id = ?
        """, ("A",)).fetchone()

    assert state[0] == 1
    assert "HTTP 422" in state[1]

    assert results[0]["status"] == "quarantined"

    # A must not be retried on the next replay call.
    with patch(
        "edge.capture_api.scheduled_replay.send_to_cloud",
        side_effect=mock_send
    ):
        replay_scheduled_events()

    assert sent_order == ["A", "C"]

def test_quarantined_descendant_does_not_boost_replay_priority(
    isolated_db
):
    # A is routine, B is urgent and depends on A.
    # C is independent.
    db.save_event(create_event("A", 1))
    db.save_event(create_event("B", 20, ["A"]))
    db.save_event(create_event("C", 3))

    # Simulate B having already been permanently
    # quarantined during an earlier recovery attempt.
    token = db.claim_event("B")
    assert token is not None

    assert db.quarantine_event(
        "B",
        token,
        "HTTP 422: Invalid event data"
    ) is True

    sent_order = []

    def mock_send(event):
        event_id = event["event_id"]
        sent_order.append(event_id)

        return {
            "status": "applied",
            "event_id": event_id
        }

    with patch(
        "edge.capture_api.scheduled_replay.send_to_cloud",
        side_effect=mock_send
    ):
        replay_scheduled_events()

    # B cannot be replayed and must not boost A.
    # Therefore C should be processed before A.
    assert sent_order == ["C", "A"]

    assert get_status("A") == "applied"
    assert get_status("B") == "pending"
    assert get_status("C") == "applied"

def test_temporary_retry_preserves_inherited_urgency(isolated_db):
    # A and Q are prerequisites of urgent event B.
    db.save_event(create_event("A", 1))
    db.save_event(create_event("Q", 2))
    db.save_event(create_event("B", 20, ["A", "Q"]))
    db.save_event(create_event("C", 3))

    # Q encounters a temporary failure and must wait.
    token = db.claim_event("Q")
    assert token is not None

    delay = db.record_failed_attempt("Q", token)
    assert 1 <= delay <= 2

    # A temporary retry must not be mistaken
    # for permanent quarantine.
    assert db.get_quarantined_recovery_ids() == set()

    sent_order = []

    def mock_send(event):
        event_id = event["event_id"]
        sent_order.append(event_id)

        return {
            "status": "applied",
            "event_id": event_id
        }

    with patch(
        "edge.capture_api.scheduled_replay.send_to_cloud",
        side_effect=mock_send
    ):
        replay_scheduled_events()

    # A inherits B's priority of 20.
    # Therefore A is selected before C.
    #
    # Q is waiting for retry, so B cannot
    # be applied during this replay.
    assert sent_order == ["A", "C"]

    assert get_status("A") == "applied"
    assert get_status("C") == "applied"
    assert get_status("Q") == "pending"
    assert get_status("B") == "pending"
