"""Edge routing, site identity, reconciliation and auto replay. No live services."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from edge.database import db
from edge.capture_api import cloud_client, scheduled_replay, auto_replay
from edge.capture_api import main as hospital


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hospital_test.db")
    db.init_db()


@pytest.fixture
def urls(monkeypatch):
    monkeypatch.delenv("PATIENT_URL", raising=False)
    monkeypatch.setenv("CLOUD_URL", "http://cloud:8001/events")
    monkeypatch.setenv("EMERGENCY_URL", "http://cloud:8003/events")
    monkeypatch.setenv("ADMIN_URL", "http://cloud:8004/events")


class FakeCloud:
    """Stands in for the three cloud services."""

    def __init__(self):
        self.up = True
        self.applied = set()
        self.posts = []

    def post(self, url, json=None, timeout=None):
        if not self.up:
            raise httpx.ConnectError("down")
        self.posts.append(url)
        self.applied.add(json["event_id"])
        return httpx.Response(
            201,
            json={"status": "applied", "event_id": json["event_id"]},
            request=httpx.Request("POST", url),
        )

    def get(self, url, timeout=None, headers=None):
        if not self.up:
            raise httpx.ConnectError("down")
        request = httpx.Request("GET", url)
        if url.endswith("/health"):
            return httpx.Response(200, json={"status": "healthy"}, request=request)
        event_id = url.rsplit("/", 1)[1]
        if event_id in self.applied:
            return httpx.Response(
                200, json={"status": "applied", "site_id": "Hospital_A"}, request=request
            )
        return httpx.Response(404, request=request)


@pytest.fixture
def cloud(monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(cloud_client.httpx, "post", fake.post)
    monkeypatch.setattr(cloud_client.httpx, "get", fake.get)
    return fake


def event(event_id, event_type, priority, prerequisites=()):
    return {
        "event_id": event_id,
        "site_id": "Hospital_A",
        "event_type": event_type,
        "base_priority": priority,
        "prerequisite_ids": list(prerequisites),
        "payload": {},
    }


def statuses():
    return {e["event_id"]: e["status"] for e in db.get_events()}


def test_each_type_goes_to_its_service(urls):
    assert cloud_client.get_event_url("patient_registration") == "http://cloud:8001/events"
    assert cloud_client.get_event_url("encounter_creation") == "http://cloud:8001/events"
    assert cloud_client.get_event_url("emergency_record") == "http://cloud:8003/events"
    assert cloud_client.get_event_url("patient_discharge") == "http://cloud:8004/events"
    assert cloud_client.get_event_url("unknown") is None


@pytest.mark.parametrize("event_id,status,state", [
    ("NORMAL-01", 200, "applied"),
    ("SPECIAL/#?% 病人", 405, "unknown"),
    ("SPECIAL/#?% 病人", 404, "not_found"),
    ("SPECIAL/#?% 病人", 503, "unknown"),
])
def test_lookup_keeps_legacy_path_and_query_failure_semantics(urls, monkeypatch, event_id, status, state):
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(status, json={"status": "applied", "site_id": "Hospital_A"})

    monkeypatch.setattr(cloud_client.httpx, "get", get)
    result = cloud_client.get_cloud_event_status({"event_id": event_id, "event_type": "patient_registration"})
    assert result["state"] == state
    url, kwargs = requests[0]
    if event_id == "NORMAL-01":
        assert url == "http://cloud:8001/events/NORMAL-01"
        assert "params" not in kwargs
    else:
        assert url == "http://cloud:8001/events"
        assert kwargs["params"] == {"event_id": event_id}
    if state == "unknown":
        assert result["error"] == f"HTTP {status}"


def test_query_lookup_connection_failure_stays_unresolved(urls, monkeypatch):
    def get(url, **kwargs):
        raise httpx.ConnectError("synthetic outage")

    monkeypatch.setattr(cloud_client.httpx, "get", get)
    result = cloud_client.get_cloud_event_status({"event_id": "SPECIAL/#?% 病人", "event_type": "patient_registration"})
    assert result == {"state": "unknown", "error": "synthetic outage"}


def test_full_chain_replays_to_three_services(isolated_db, urls, cloud):
    db.save_event(event("R", "patient_registration", 1))
    db.save_event(event("E", "encounter_creation", 2, ["R"]))
    db.save_event(event("X", "emergency_record", 5, ["E"]))
    db.save_event(event("D", "patient_discharge", 1, ["X"]))

    scheduled_replay.replay_scheduled_events()

    assert cloud.posts == [
        "http://cloud:8001/events",
        "http://cloud:8001/events",
        "http://cloud:8003/events",
        "http://cloud:8004/events",
    ]
    assert set(statuses().values()) == {"applied"}


def test_unknown_type_is_quarantined_not_sent(isolated_db, urls, cloud):
    db.save_event(event("BAD", "weird_type", 3))

    results = scheduled_replay.replay_scheduled_events()

    assert results[0]["status"] == "quarantined"
    assert cloud.posts == []
    assert db.count_replayable_events() == 0


def test_reconcile_after_crash(isolated_db, urls, cloud):
    db.save_event(event("C1", "patient_registration", 1))
    db.save_event(event("C2", "patient_registration", 1))
    db.claim_event("C1")
    db.claim_event("C2")
    cloud.applied.add("C1")  # cloud applied C1, but the hospital crashed

    summary = auto_replay.reconcile_in_flight_events()

    assert summary == {"applied": ["C1"], "released": ["C2"], "unresolved": []}
    assert statuses() == {"C1": "applied", "C2": "pending"}
    assert db.claim_event("C2") is not None


def test_auto_replay_drains_after_reconnect(isolated_db, urls, cloud):
    async def scenario():
        cloud.up = False
        stop = asyncio.Event()
        task = asyncio.create_task(auto_replay.run_auto_replay(stop, 0.05))
        await asyncio.sleep(0.15)
        db.save_event(event("OFF1", "patient_registration", 1))
        db.save_event(event("OFF2", "emergency_record", 5, ["OFF1"]))
        cloud.up = True
        await asyncio.sleep(0.4)
        stop.set()
        await task

    asyncio.run(scenario())

    state = auto_replay.recovery_state
    assert state["last_reconnected_at"] is not None
    assert state["drain_completed_at"] is not None
    assert statuses() == {"OFF1": "applied", "OFF2": "applied"}


@pytest.mark.parametrize("site_env,body_site,expected_status,stored_site", [
    ("Hospital_A", None, 201, "Hospital_A"),
    ("Hospital_A", "Hospital_A", 201, "Hospital_A"),
    ("Hospital_A", "Hospital_B", 403, None),
    (None, None, 422, None),
])
def test_capture_site_identity(isolated_db, monkeypatch, site_env, body_site,
                               expected_status, stored_site):
    if site_env:
        monkeypatch.setenv("SITE_ID", site_env)
    else:
        monkeypatch.delenv("SITE_ID", raising=False)

    body = {"event_id": "S1", "event_type": "patient_registration", "base_priority": 1}
    if body_site:
        body["site_id"] = body_site

    response = TestClient(hospital.app).post("/capture", json=body)

    assert response.status_code == expected_status
    if stored_site:
        assert db.get_events()[0]["site_id"] == stored_site


@pytest.mark.parametrize("bad", [
    {"event_type": "not_a_type"},
    {"base_priority": 9},
    {"event_id": "   "},
])
def test_capture_rejects_bad_events(isolated_db, monkeypatch, bad):
    monkeypatch.setenv("SITE_ID", "Hospital_A")
    body = {"event_id": "B1", "event_type": "patient_registration", "base_priority": 1, **bad}

    response = TestClient(hospital.app).post("/capture", json=body)

    assert response.status_code == 422
    assert db.get_events() == []
