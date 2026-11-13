
import pytest
from edge.database import db

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Use a temporary database instead of Hospital A's real database."""
    database_path = tmp_path / "hospital_test.db"

    monkeypatch.setattr(db, "DB_PATH", database_path)

    db.init_db()
    return database_path

def create_test_event():
    return {
        "event_id": "TEST_A",
        "site_id": "hospital-a",
        "event_type": "patient_registration",
        "base_priority": 1,
        "prerequisite_ids": [],
        "payload": {"patient_id": "P001"}
    }

def test_new_event_has_recovery_state(isolated_db):
    db.save_event(create_test_event())

    import sqlite3

    with sqlite3.connect(db.DB_PATH) as conn:
        row = conn.execute("""
            SELECT in_flight, retry_count
            FROM recovery_state
            WHERE event_id = ?
        """, ("TEST_A",)).fetchone()

    assert row == (0, 0)

def test_only_one_active_claim(isolated_db):
    db.save_event(create_test_event())

    token = db.claim_event("TEST_A")

    assert isinstance(token, str)
    assert len(token) > 0
    assert db.claim_event("TEST_A") is None

def test_unknown_event_cannot_be_claimed(isolated_db):
    assert db.claim_event("UNKNOWN") is None

def test_applied_event_cannot_be_claimed(isolated_db):
    db.save_event(create_test_event())
    db.mark_event_applied("TEST_A")

    assert db.claim_event("TEST_A") is None

def test_invalid_lease_duration(isolated_db):
    with pytest.raises(ValueError):
        db.claim_event("TEST_A", lease_seconds=0)

def test_old_worker_cannot_release_new_claim(isolated_db):
    db.save_event(create_test_event())

    # Worker 1 claims the event
    token_1 = db.claim_event("TEST_A", lease_seconds=30)
    assert token_1 is not None

    # Simulate the first worker's lease expiring
    import sqlite3

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("""
            UPDATE recovery_state
            SET lease_expires_at = ?
            WHERE event_id = ?
        """, ("2000-01-01T00:00:00+00:00", "TEST_A"))

    # Worker 2 takes over
    token_2 = db.claim_event("TEST_A")
    assert token_2 is not None
    assert token_2 != token_1

    # Worker 1 cannot release Worker 2's claim
    assert db.release_event("TEST_A", token_1) is False

    # Worker 2 can release its own claim
    assert db.release_event("TEST_A", token_2) is True

def test_acknowledge_event(isolated_db):
    db.save_event(create_test_event())

    token = db.claim_event("TEST_A")
    assert token is not None

    assert db.acknowledge_event("TEST_A", token) is True

    event = next(
        e for e in db.get_events()
        if e["event_id"] == "TEST_A"
    )
    assert event["status"] == "applied"

    import sqlite3
    with sqlite3.connect(db.DB_PATH) as conn:
        state = conn.execute("""
            SELECT in_flight, claim_token
            FROM recovery_state
            WHERE event_id = ?
        """, ("TEST_A",)).fetchone()

    assert state == (0, None)

def test_old_worker_cannot_acknowledge_new_claim(isolated_db):
    db.save_event(create_test_event())

    token_1 = db.claim_event("TEST_A")

    import sqlite3
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("""
            UPDATE recovery_state
            SET lease_expires_at = ?
            WHERE event_id = ?
        """, ("2000-01-01T00:00:00+00:00", "TEST_A"))

    token_2 = db.claim_event("TEST_A")
    assert token_2 != token_1

    assert db.acknowledge_event("TEST_A", token_1) is False
    assert db.acknowledge_event("TEST_A", token_2) is True

def test_failed_attempt_schedules_retry(isolated_db):
    import sqlite3

    db.save_event(create_test_event())
    token = db.claim_event("TEST_A")

    delay = db.record_failed_attempt("TEST_A", token)
    assert 1 <= delay <= 2

    with sqlite3.connect(db.DB_PATH) as conn:
        state = conn.execute("""
            SELECT in_flight, retry_count, retry_after, claim_token
            FROM recovery_state
            WHERE event_id = ?
        """, ("TEST_A",)).fetchone()

    assert state[0] == 0
    assert state[1] == 1
    assert state[2] is not None
    assert state[3] is None

    # Retry is not permitted before the delay expires
    assert db.claim_event("TEST_A") is None

def test_old_token_cannot_record_failure(isolated_db):
    import sqlite3

    db.save_event(create_test_event())
    old_token = db.claim_event("TEST_A")

    # Simulate lease expiration
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("""
            UPDATE recovery_state
            SET lease_expires_at = ?
            WHERE event_id = ?
        """, ("2000-01-01T00:00:00+00:00", "TEST_A"))

    new_token = db.claim_event("TEST_A")
    assert new_token is not None
    assert new_token != old_token

    # Previous claim holder cannot change retry state
    assert db.record_failed_attempt("TEST_A", old_token) is None

    with sqlite3.connect(db.DB_PATH) as conn:
        state = conn.execute("""
            SELECT in_flight, retry_count, claim_token
            FROM recovery_state
            WHERE event_id = ?
        """, ("TEST_A",)).fetchone()

    assert state == (1, 0, new_token)

def test_exponential_backoff(isolated_db):
    import sqlite3

    db.save_event(create_test_event())

    expected_delays = [2, 4, 8, 16, 30, 30]

    for expected_delay in expected_delays:
        # Make the retry deadline expire
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("""
                UPDATE recovery_state
                SET retry_after = NULL
                WHERE event_id = ?
            """, ("TEST_A",))

        token = db.claim_event("TEST_A")
        assert token is not None

        delay = db.record_failed_attempt("TEST_A", token)
        assert expected_delay / 2 <= delay <= expected_delay

    with sqlite3.connect(db.DB_PATH) as conn:
        retry_count = conn.execute("""
            SELECT retry_count
            FROM recovery_state
            WHERE event_id = ?
        """, ("TEST_A",)).fetchone()[0]

    assert retry_count == 6


def test_retry_allowed_after_deadline(isolated_db):
    import sqlite3

    db.save_event(create_test_event())

    token = db.claim_event("TEST_A")
    assert token is not None

    db.record_failed_attempt("TEST_A", token)

    # Retry is initially blocked
    assert db.claim_event("TEST_A") is None

    # Simulate the retry deadline passing
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("""
            UPDATE recovery_state
            SET retry_after = ?
            WHERE event_id = ?
        """, (
            "2000-01-01T00:00:00+00:00",
            "TEST_A"
        ))

    # Event becomes claimable again
    new_token = db.claim_event("TEST_A")

    assert new_token is not None
    assert new_token != token

def test_quarantine_event(isolated_db):
    import sqlite3

    db.save_event(create_test_event())

    token = db.claim_event("TEST_A")
    assert token is not None

    assert db.quarantine_event(
        "TEST_A",
        token,
        "HTTP 422: Invalid site ID"
    ) is True

    with sqlite3.connect(db.DB_PATH) as conn:
        state = conn.execute("""
            SELECT quarantined, quarantine_reason,
                   in_flight, claim_token
            FROM recovery_state
            WHERE event_id = ?
        """, ("TEST_A",)).fetchone()

    assert state == (
        1,
        "HTTP 422: Invalid site ID",
        0,
        None
    )

def test_old_token_cannot_quarantine_new_claim(isolated_db):
    import sqlite3

    db.save_event(create_test_event())

    old_token = db.claim_event("TEST_A")

    # Simulate expiration of the old claim
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("""
            UPDATE recovery_state
            SET lease_expires_at = ?
            WHERE event_id = ?
        """, ("2000-01-01T00:00:00+00:00", "TEST_A"))

    new_token = db.claim_event("TEST_A")
    assert new_token is not None
    assert new_token != old_token

    assert db.quarantine_event(
        "TEST_A",
        old_token,
        "Old worker error"
    ) is False

    with sqlite3.connect(db.DB_PATH) as conn:
        state = conn.execute("""
            SELECT quarantined, claim_token
            FROM recovery_state
            WHERE event_id = ?
        """, ("TEST_A",)).fetchone()

    assert state == (0, new_token)

def test_quarantined_event_cannot_be_claimed(isolated_db):
    db.save_event(create_test_event())

    token = db.claim_event("TEST_A")
    assert token is not None

    assert db.quarantine_event(
        "TEST_A",
        token,
        "HTTP 422: Invalid event data"
    ) is True

    assert db.claim_event("TEST_A") is None

    blocked_ids = db.get_blocked_recovery_ids()
    assert "TEST_A" in blocked_ids


def test_retry_jitter_uses_bounded_delay(isolated_db, monkeypatch):
    db.save_event(create_test_event())

    token = db.claim_event("TEST_A")
    assert token is not None

    observed = {}

    def fake_uniform(low, high):
        observed["low"] = low
        observed["high"] = high
        return 1.5

    monkeypatch.setattr(db.random, "uniform", fake_uniform)

    delay = db.record_failed_attempt("TEST_A", token)

    assert observed == {"low": 1.0, "high": 2}
    assert delay == 1.5