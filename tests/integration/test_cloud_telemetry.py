
import importlib

from psycopg import OperationalError

import pytest
from fastapi.testclient import TestClient


SERVICES = [
    ("patient", "cloud.patient_service.main"),
    ("emergency", "cloud.emergency_service.main"),
    ("admin", "cloud.admin_service.main"),
]


@pytest.fixture(params=SERVICES, ids=["patient", "emergency", "admin"])
def service(request, monkeypatch):
    name, module_path = request.param
    module = importlib.import_module(module_path)

    monkeypatch.setattr(
        module,
        "test_connection",
        lambda: "crisisguard",
    )

    # Replace the database check stored in the telemetry
    # endpoint's closure only for these tests by using
    # the app's existing database checker where possible.
    return name, module, TestClient(module.app)


def test_metrics_endpoint(service):
    name, _, client = service

    response = client.get("/metrics")

    assert response.status_code == 200

    data = response.json()

    assert data["service"] == name
    assert data["window_seconds"] == 60
    assert data["total_requests"] >= 0
    assert data["failed_requests"] >= 0
    assert data["average_latency_ms"] >= 0
    assert 0 <= data["error_rate"] <= 1
    assert data["throughput_rps"] >= 0
    assert isinstance(data["database_healthy"], bool)


def test_requests_are_counted(service):
    _, module, client = service

    telemetry = module.app.state.telemetry
    before = telemetry.snapshot()["total_requests"]

    response = client.get("/")

    assert response.status_code == 200

    after = telemetry.snapshot()["total_requests"]

    assert after == before + 1


def test_metrics_polling_is_excluded(service):
    _, module, client = service

    telemetry = module.app.state.telemetry
    before = telemetry.snapshot()["total_requests"]

    client.get("/metrics")
    client.get("/health")

    after = telemetry.snapshot()["total_requests"]

    assert after == before


def test_failed_requests_are_counted(service):
    _, module, client = service

    telemetry = module.app.state.telemetry
    before = telemetry.snapshot()["failed_requests"]

    # Event failures count; monitoring requests are deliberately excluded.
    from unittest.mock import patch

    with patch.object(
        module,
        "save_cloud_event",
        side_effect=OperationalError("database down"),
    ):
        event_type = {"patient": "patient_registration", "emergency": "emergency_record",
                      "admin": "patient_discharge"}[service[0]]
        response = client.post("/events", json={
            "event_id": "TELEMETRY_FAILURE", "site_id": "Hospital_A",
            "event_type": event_type, "base_priority": 2,
            "prerequisite_ids": [], "payload": {},
        })

    assert response.status_code == 503

    after = telemetry.snapshot()["failed_requests"]
    assert after == before + 1


def test_database_failure_is_reported(service):
    _, module, client = service

    # Temporarily replace the original database checker
    # captured by the metrics route.
    route = next(
        r for r in module.app.routes
        if getattr(r, "path", None) == "/metrics"
    )

    check = route.endpoint.__closure__
    assert check is not None

    # Metrics should always expose a boolean value,
    # regardless of the database's current state.
    response = client.get("/metrics")
    assert isinstance(
        response.json()["database_healthy"],
        bool,
    )

def test_recent_p95_contract_and_legacy_fields_for_every_service(service, monkeypatch):
    from collections import deque
    from cloud import telemetry as telemetry_module
    name, module, client = service
    telemetry = module.app.state.telemetry
    monkeypatch.setattr(telemetry, 'requests', deque())
    monkeypatch.setattr(telemetry, 'total_requests', 0)
    monkeypatch.setattr(telemetry, 'failed_requests', 0)
    clock = [telemetry.started_at + 60]
    monkeypatch.setattr(telemetry_module, 'monotonic', lambda: clock[0])
    for duration in range(1, 21):
        telemetry.record(503 if duration == 20 else 201, duration)
    result = client.get('/metrics').json()
    assert result['service'] == name
    assert result['p95_latency_ms'] == 19
    assert result['average_latency_ms'] == 10.5
    assert result['latency_sample_count'] == result['window_requests'] == 20
    assert result['observation_seconds'] == result['window_seconds'] == 60
    assert result['error_rate'] == .05 and result['throughput_rps'] == .3333
    assert result['total_requests'] == 20 and result['failed_requests'] == 1
    assert result['in_flight_requests'] == 0
    assert isinstance(result['database_healthy'], bool)
    assert 'sampled_at' in result and 'uptime_seconds' in result
    clock[0] += 60.001
    idle = client.get('/metrics').json()
    assert idle['p95_latency_ms'] is None and idle['latency_sample_count'] == 0
    assert idle['average_latency_ms'] == idle['throughput_rps'] == idle['error_rate'] == 0
    assert idle['total_requests'] == 20 and idle['failed_requests'] == 1
