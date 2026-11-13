
"""Integration tests for the read-only Hospital Scheduler Inspector API."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from edge.capture_api.main import app
from edge.database import db


@pytest.fixture
def preview_db(tmp_path, monkeypatch):
    """Create an isolated SQLite database for each test."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hospital.db")
    db.init_db()

    def add(event_id, priority, prerequisites=None):
        db.save_event({
            "event_id": event_id,
            "site_id": "Hospital_B",
            "event_type": "patient_registration",
            "base_priority": priority,
            "prerequisite_ids": prerequisites or [],
            "payload": {"patient_id": "TEST"},
        })

    # Applied prerequisite should remain recognised.
    add("DONE", 2)
    db.mark_event_applied("DONE")

    # Capture C, then A, then B.
    add("C", 3, ["DONE"])
    add("A", 1)
    add("B", 5, ["A"])

    return TestClient(app)


def test_api_inheritance_on_off(preview_db):
    on = preview_db.get(
        "/scheduler/inspect",
        params={"inheritance": "true"},
    )

    off = preview_db.get(
        "/scheduler/inspect",
        params={"inheritance": "false"},
    )

    assert on.status_code == 200
    assert off.status_code == 200

    assert on.json()["next_event_id"] == "A"
    assert off.json()["next_event_id"] == "C"

    assert on.json()["pending_count"] == 3


def test_api_excludes_applied_events(preview_db):
    response = preview_db.get("/scheduler/inspect")

    assert response.status_code == 200

    data = response.json()
    ids = {event["event_id"] for event in data["events"]}

    assert ids == {"A", "B", "C"}
    assert "DONE" not in ids

    # C depends on DONE, which was already applied.
    c = next(
        event for event in data["events"]
        if event["event_id"] == "C"
    )
    assert c["eligible"] is True


def test_api_handles_quarantined_events(preview_db):
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("""
            UPDATE recovery_state
            SET quarantined = 1
            WHERE event_id = 'B'
        """)

    response = preview_db.get("/scheduler/inspect")
    assert response.status_code == 200

    data = response.json()
    events = {
        event["event_id"]: event
        for event in data["events"]
    }

    assert data["next_event_id"] == "C"
    assert events["A"]["effective_priority"] == 1
    assert events["B"]["eligible"] is False


def test_api_handles_in_flight_events(preview_db):
    expiry = (
        datetime.now(timezone.utc)
        + timedelta(minutes=5)
    ).isoformat()

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("""
            UPDATE recovery_state
            SET in_flight = 1,
                lease_expires_at = ?
            WHERE event_id = 'A'
        """, (expiry,))

    response = preview_db.get("/scheduler/inspect")
    assert response.status_code == 200

    data = response.json()
    events = {
        event["event_id"]: event
        for event in data["events"]
    }

    assert data["next_event_id"] == "C"
    assert events["A"]["eligible"] is False
    assert events["A"]["reason"] == "In flight"


def test_api_is_read_only(preview_db):
    def database_snapshot():
        with sqlite3.connect(db.DB_PATH) as conn:
            events = conn.execute(
                "SELECT * FROM events ORDER BY event_id"
            ).fetchall()

            recovery = conn.execute(
                "SELECT * FROM recovery_state ORDER BY event_id"
            ).fetchall()

        return events, recovery

    before = database_snapshot()

    response = preview_db.get("/scheduler/inspect")

    after = database_snapshot()

    assert response.status_code == 200
    assert before == after
