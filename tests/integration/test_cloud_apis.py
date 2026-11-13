
"""Regression tests for CrisisGuard Cloud Backend APIs.

Tests Patient API, Emergency API and Admin API.
Database operations are mocked, so no live PostgreSQL is required.

Run:
    python -m pytest -v tests/integration/test_cloud_apis.py
"""

import importlib
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg import OperationalError


# Cloud services to test
SERVICES = (
    ("patient", "cloud.patient_service.main", "patient_registration"),
    ("emergency", "cloud.emergency_service.main", "emergency_record"),
    ("admin", "cloud.admin_service.main", "patient_discharge"),
)


@pytest.fixture(params=SERVICES, ids=[s[0] for s in SERVICES])
def service(request):
    name, module_name, event_type = request.param

    module = importlib.import_module(module_name)

    # Avoid starting the real PostgreSQL database
    client = TestClient(module.app)

    return name, module, client, event_type


def event_body(event_type):
    """Create a sample cloud event."""
    return {
        "event_id": "REGRESSION_EVENT_001",
        "site_id": "Hospital_A",
        "event_type": event_type,
        "base_priority": 2,
        "prerequisite_ids": [],
        "payload": {
            "patient_id": "P001"
        },
    }


# TEST 1: New event successfully applied
def test_new_event_returns_201(service, monkeypatch):
    _, module, client, event_type = service
    captured = []

    def save(event):
        captured.append(event)
        return "applied"

    monkeypatch.setattr(module, "save_cloud_event", save)

    response = client.post(
        "/events",
        json=event_body(event_type)
    )

    assert response.status_code == 201

    assert response.json() == {
        "status": "applied",
        "event_id": "REGRESSION_EVENT_001",
        "site_id": "Hospital_A",
        "result": "applied"
    }

    assert captured == [event_body(event_type)]


# TEST 2: Duplicate event handled safely
def test_identical_retry_returns_200(service, monkeypatch):
    _, module, client, event_type = service

    monkeypatch.setattr(
        module,
        "save_cloud_event",
        lambda _: "already_applied"
    )

    response = client.post(
        "/events",
        json=event_body(event_type)
    )

    assert response.status_code == 200
    assert response.json()["result"] == "already_applied"


# TEST 3: Missing event dependency
def test_missing_prerequisite_returns_409(service, monkeypatch):
    _, module, client, event_type = service

    def save(_):
        raise module.MissingPrerequisitesError(
            ["PREVIOUS_EVENT"]
        )

    monkeypatch.setattr(module, "save_cloud_event", save)

    body = event_body(event_type)
    body["prerequisite_ids"] = ["PREVIOUS_EVENT"]

    response = client.post("/events", json=body)

    assert response.status_code == 409

    assert response.json()["detail"] == {
        "code": "MISSING_PREREQUISITES",
        "missing_ids": ["PREVIOUS_EVENT"]
    }


# TEST 4: Conflicting event IDs
def test_conflicting_event_returns_409(service, monkeypatch):
    _, module, client, event_type = service

    def save(_):
        raise module.ConflictingEventError(
            "different payload"
        )

    monkeypatch.setattr(module, "save_cloud_event", save)

    response = client.post(
        "/events",
        json=event_body(event_type)
    )

    assert response.status_code == 409

    assert response.json()["detail"]["code"] == "EVENT_CONFLICT"


# TEST 5: PostgreSQL connection failure
def test_database_unavailable_returns_503(service, monkeypatch):
    _, module, client, event_type = service

    def save(_):
        raise OperationalError("connection failed")

    monkeypatch.setattr(module, "save_cloud_event", save)

    response = client.post(
        "/events",
        json=event_body(event_type)
    )

    assert response.status_code == 503

    assert response.json()["detail"]["code"] == "DATABASE_UNAVAILABLE"


# TEST 6: Invalid priority rejected
@pytest.mark.parametrize("value", [0, 6])
def test_invalid_priority_returns_422_without_db_call(
    service, monkeypatch, value
):
    _, module, client, event_type = service

    def should_not_call_db(_):
        pytest.fail("Database called for invalid input")

    monkeypatch.setattr(
        module,
        "save_cloud_event",
        should_not_call_db
    )

    body = event_body(event_type)
    body["base_priority"] = value

    response = client.post("/events", json=body)

    assert response.status_code == 422


# TEST 7: Invalid hospital rejected
def test_invalid_site_returns_422(service):
    _, _, client, event_type = service

    body = event_body(event_type)
    body["site_id"] = "Hospital_X"

    response = client.post("/events", json=body)

    assert response.status_code == 422


# TEST 8: Unsupported event type rejected
def test_invalid_event_type_returns_422(service):
    _, _, client, _ = service

    body = event_body("unsupported_event_type")

    response = client.post("/events", json=body)

    assert response.status_code == 422


# TEST 9: Health check succeeds
def test_health_check_success(service, monkeypatch):
    name, module, client, _ = service

    monkeypatch.setattr(
        module,
        "test_connection",
        lambda: "crisisguard"
    )

    response = client.get("/health")

    assert response.status_code == 200

    assert response.json() == {
        "status": "healthy",
        "service": name,
        "database": "crisisguard"
    }


# TEST 10: Health check detects database failure
def test_health_check_db_failure(service, monkeypatch):
    _, module, client, _ = service

    def unavailable():
        raise RuntimeError("db down")

    monkeypatch.setattr(
        module,
        "test_connection",
        unavailable
    )

    response = client.get("/health")

    assert response.status_code == 503

    assert response.json()["detail"]["code"] == "DATABASE_UNAVAILABLE"


def test_event_status_lookup_contract(service, monkeypatch):
    _, module, client, _ = service
    stored = {
        "event_id": "LOOKUP_EVENT",
        "site_id": "Hospital_A",
        "event_type": "patient_registration",
        "status": "applied",
        "applied_at": "2026-10-31T00:00:00+00:00",
    }

    monkeypatch.setattr(module, "get_cloud_event", lambda _: stored)
    response = client.get("/events/LOOKUP_EVENT")
    assert response.status_code == 200
    assert response.json() == stored

    monkeypatch.setattr(module, "get_cloud_event", lambda _: None)
    response = client.get("/events/MISSING_EVENT")
    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "EVENT_NOT_FOUND",
        "event_id": "MISSING_EVENT",
    }

    def unavailable(_):
        raise OperationalError("connection failed")

    monkeypatch.setattr(module, "get_cloud_event", unavailable)
    response = client.get("/events/LOOKUP_EVENT")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DATABASE_UNAVAILABLE"


@pytest.mark.parametrize("event_id", [
    "LOOKUP#fragment", "LOOKUP?query", "LOOKUP/child", "LOOKUP%2Fchild",
    " LOOKUP with spaces ", "LOOKUP_病人_é", ".", "..", "LOOKUP+plus&value",
])
@pytest.mark.parametrize("protected", [False, True], ids=["legacy", "protected"])
def test_query_lookup_preserves_exact_id(service, monkeypatch, event_id, protected):
    _, module, client, event_type = service
    monkeypatch.setenv("METAGUARD_REQUIRED", "1" if protected else "0")
    stored = {"event_id": event_id, "site_id": "Hospital_A", "event_type": event_type,
              "status": "applied", "applied_at": "2026-11-07T00:00:00+00:00"}
    looked_up = []

    def lookup(value):
        looked_up.append(value)
        return stored

    monkeypatch.setattr(module, "get_cloud_event", lookup)
    response = client.get("/events", params={"event_id": event_id})

    assert response.status_code == 200
    assert response.json() == stored
    assert looked_up == [event_id]


@pytest.mark.parametrize("unavailable", [False, True], ids=["absent", "database-down"])
def test_query_lookup_preserves_error_contract(service, monkeypatch, unavailable):
    _, module, client, _ = service
    event_id = "MISSING/#?% 病人"

    def lookup(value):
        assert value == event_id
        if unavailable:
            raise OperationalError("connection failed")
        return None

    monkeypatch.setattr(module, "get_cloud_event", lookup)
    response = client.get("/events", params={"event_id": event_id})

    if unavailable:
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "DATABASE_UNAVAILABLE"
    else:
        assert response.status_code == 404
        assert response.json()["detail"] == {"code": "EVENT_NOT_FOUND", "event_id": event_id}


def test_query_lookup_requires_an_id(service, monkeypatch):
    _, module, client, _ = service
    monkeypatch.setattr(module, "get_cloud_event", lambda _: pytest.fail("Unexpected database lookup"))
    assert client.get("/events").status_code == 422


@pytest.mark.parametrize("event_id", [
    "LOOKUP_NORMAL-01", "LOOKUP#fragment", "LOOKUP?query", "LOOKUP/child",
    "LOOKUP%2Fchild", " LOOKUP with spaces ", "LOOKUP_病人_é", ".", "..",
])
def test_edge_status_lookup_round_trip(service, monkeypatch, event_id):
    from edge.capture_api import cloud_client

    _, module, client, event_type = service
    monkeypatch.setenv("CLOUD_REPLAY_TELEMETRY_TOKEN", "synthetic-replay-" * 4)
    looked_up = []

    def lookup(value):
        looked_up.append(value)
        return {"event_id": value, "site_id": "Hospital_A", "event_type": event_type,
                "status": "applied", "applied_at": "2026-11-07T00:00:00+00:00"}

    def get(url, **kwargs):
        request = httpx.Request("GET", url, params=kwargs.get("params"))
        assert kwargs["headers"] == {
            "X-CrisisGuard-Traffic": "replay",
            "X-CrisisGuard-Telemetry-Token": "synthetic-replay-" * 4,
        }
        assert kwargs["timeout"] == 3.0
        return client.get(str(request.url), headers=kwargs["headers"])

    monkeypatch.setattr(module, "get_cloud_event", lookup)
    monkeypatch.setattr(cloud_client.httpx, "get", get)
    assert cloud_client.get_cloud_event_status({"event_id": event_id, "event_type": event_type}) == {
        "state": "applied", "site_id": "Hospital_A",
    }
    assert looked_up == [event_id]


def test_protected_event_admission_contract(service, monkeypatch):
    _, module, client, event_type = service
    monkeypatch.setenv("METAGUARD_REQUIRED", "1")
    monkeypatch.setenv(
        "CONTROLLER_HOSPITAL_TOKENS",
        json.dumps({"Hospital_A": "a" * 40, "Hospital_B": "b" * 40}),
    )
    monkeypatch.setenv("CONTROLLER_CLOUD_TOKEN", "c" * 40)
    body = event_body(event_type)
    grant_id = str(uuid4())
    captured = []

    def save(event, *, grant_id):
        captured.append((event, grant_id))
        return "applied"

    monkeypatch.setattr(module, "save_cloud_event", save)

    assert client.post("/events", json=body).status_code == 401
    assert client.post(
        "/events",
        json=body,
        headers={
            "Authorization": "Bearer " + "b" * 40,
            "X-MetaGuard-Grant": grant_id,
        },
    ).status_code == 403
    assert client.post(
        "/events",
        json=body,
        headers={
            "Authorization": "Bearer " + "a" * 40,
            "X-MetaGuard-Grant": "not-a-uuid",
        },
    ).status_code == 403

    response = client.post(
        "/events",
        json=body,
        headers={
            "Authorization": "Bearer " + "a" * 40,
            "X-MetaGuard-Grant": grant_id,
        },
    )
    assert response.status_code == 201
    assert captured == [(body, grant_id)]


def test_protected_event_admission_fails_closed_when_unconfigured(
    service, monkeypatch
):
    _, _, client, event_type = service
    monkeypatch.setenv("METAGUARD_REQUIRED", "1")
    monkeypatch.setenv("CONTROLLER_HOSPITAL_TOKENS", "{}")

    response = client.post("/events", json=event_body(event_type))

    assert response.status_code == 503
    assert response.json()["detail"] == "Hospital authentication is not configured"
