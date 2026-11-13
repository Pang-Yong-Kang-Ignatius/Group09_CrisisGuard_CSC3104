"""Capture must reject malformed prerequisites before they poison replay."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from edge.capture_api import main as hospital
from edge.capture_api import scheduled_replay
from edge.database import db


@pytest.fixture
def capture_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hospital.db")
    monkeypatch.setenv("SITE_ID", "Hospital_A")
    monkeypatch.setenv("AUTO_REPLAY", "0")
    monkeypatch.setenv("ENABLE_MANUAL_REPLAY", "1")
    monkeypatch.setenv("MANUAL_REPLAY_TOKEN", "test-capture-prerequisites")
    monkeypatch.delenv("METAGUARD_ENABLED", raising=False)
    with TestClient(hospital.app) as client:
        yield client


def event(event_id, priority=1, prerequisites=None):
    return {
        "event_id": event_id,
        "event_type": "patient_registration",
        "base_priority": priority,
        "prerequisite_ids": prerequisites if prerequisites is not None else [],
        "payload": {"patient_id": "TEST"},
    }


def database_snapshot():
    with sqlite3.connect(db.DB_PATH) as conn:
        return (
            conn.execute("SELECT * FROM events ORDER BY event_id").fetchall(),
            conn.execute(
                "SELECT * FROM recovery_state ORDER BY event_id"
            ).fetchall(),
        )


@pytest.mark.parametrize("prerequisites", [
    ["A", "A"],
    ["A", "B", "A"],
    [""],
    [" "],
    ["\t\n"],
    ["A", " "],
])
def test_invalid_capture_preserves_healthy_preview_and_replay(
    capture_client, monkeypatch, prerequisites
):
    # Capture order differs from the expected inherited-priority replay order.
    for body in [event("OTHER", 3), event("ROOT"), event("CHILD", 5, ["ROOT"])]:
        assert capture_client.post("/capture", json=body).status_code == 201

    before = database_snapshot()
    response = capture_client.post("/capture", json=event("BAD", 5, prerequisites))

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", "prerequisite_ids"]
        for error in response.json()["detail"]
    )
    assert database_snapshot() == before

    preview = capture_client.get("/scheduler/inspect")
    assert preview.status_code == 200
    assert preview.json()["next_event_id"] == "ROOT"
    assert {row["event_id"] for row in preview.json()["events"]} == {
        "ROOT", "CHILD", "OTHER"
    }
    assert database_snapshot() == before

    sent = []

    def send(event):
        sent.append(event["event_id"])
        return {"status": "applied", "event_id": event["event_id"]}

    monkeypatch.setattr(scheduled_replay, "send_to_cloud", send)
    replay = capture_client.post(
        "/replay",
        headers={"Authorization": "Bearer test-capture-prerequisites"},
    )
    assert replay.status_code == 200
    assert replay.json()["applied"] == 3
    assert sent == ["ROOT", "CHILD", "OTHER"]
    assert all(row["status"] == "applied" for row in db.get_events())


@pytest.mark.parametrize("prerequisites", [
    [],
    ["NOT_YET_CAPTURED"],
    ["SELF"],
    [" ROOT "],
    ["A", "a"],
])
def test_capture_preserves_well_formed_prerequisite_ids(capture_client, prerequisites):
    response = capture_client.post("/capture", json=event("SELF", 1, prerequisites))

    assert response.status_code == 201
    assert db.get_events()[0]["prerequisite_ids"] == prerequisites
    assert capture_client.get("/scheduler/inspect").status_code == 200


def test_cycle_and_missing_dependencies_remain_captureable(capture_client):
    for body in [
        event("A", 5, ["B"]),
        event("B", 5, ["A"]),
        event("MISSING", 5, ["ABSENT"]),
        event("HEALTHY", 3),
    ]:
        assert capture_client.post("/capture", json=body).status_code == 201

    preview = capture_client.get("/scheduler/inspect")
    assert preview.status_code == 200
    assert preview.json()["next_event_id"] == "HEALTHY"
    rows = {row["event_id"]: row for row in preview.json()["events"]}
    assert "cycle" in rows["A"]["reason"].lower()
    assert "cycle" in rows["B"]["reason"].lower()
    assert "missing" in rows["MISSING"]["reason"].lower()
