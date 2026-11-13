"""Direct submissions cannot bypass the hospital grant workflow."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from cloud.traffic import classify_traffic, traffic_headers
from dashboard import main


@pytest.fixture
def client():
    with TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        yield client


@pytest.mark.parametrize("service,event_type", [
    ("patient", "patient_registration"), ("emergency", "emergency_record"),
    ("admin", "patient_discharge"),
])
def test_enforcement_rejects_direct_posts_before_forwarding(client, monkeypatch, service, event_type):
    monkeypatch.setenv("METAGUARD_REQUIRED", "1")
    forward = AsyncMock()
    monkeypatch.setattr(main, "forward_request", forward)
    response = client.post(f"/api/events/{service}", json={
        "event_id": "guard-test", "site_id": "Hospital_A",
        "event_type": event_type, "base_priority": 2,
    }, headers={"X-CrisisGuard-Traffic": "live", "X-MetaGuard-Grant": "untrusted"})
    assert response.status_code == 403
    assert "Hospital A/B capture" in response.json()["detail"]
    forward.assert_not_awaited()


@pytest.mark.parametrize("enforced", [False, True])
def test_direct_controls_follow_enforcement(client, monkeypatch, enforced):
    monkeypatch.setenv("METAGUARD_REQUIRED", "1" if enforced else "0")
    response = client.get("/")
    assert response.status_code == 200
    assert ('<details class="advanced-tools" hidden>' in response.text) == enforced
    assert ("Direct Cloud submissions are disabled" in response.text) == enforced


def test_unprotected_stack_preserves_direct_submission(client, monkeypatch):
    monkeypatch.delenv("METAGUARD_REQUIRED", raising=False)
    forward = AsyncMock(return_value={"http_status": 201})
    monkeypatch.setattr(main, "forward_request", forward)
    response = client.post("/api/events/patient", json={
        "event_id": "guard-test", "site_id": "Hospital_A",
        "event_type": "patient_registration", "base_priority": 2,
    })
    assert response.status_code == 200
    forward.assert_awaited_once()


@pytest.mark.parametrize("service,event_type", [
    ("patient", "patient_registration"), ("emergency", "emergency_record"),
    ("admin", "patient_discharge"),
])
def test_unprotected_direct_route_uses_authenticated_live_provenance(
    client, monkeypatch, service, event_type
):
    monkeypatch.delenv("METAGUARD_REQUIRED", raising=False)
    monkeypatch.setenv("CLOUD_LIVE_TELEMETRY_TOKEN", "dashboard-live-private-" * 2)
    captured = {}

    class CloudClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return httpx.Response(
                401,
                json={"detail": "Hospital credentials required"},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(main.httpx, "AsyncClient", CloudClient)
    response = client.post(f"/api/events/{service}", json={
        "event_id": "live-provenance-test", "site_id": "Hospital_A",
        "event_type": event_type, "base_priority": 2,
    })

    assert response.status_code == 200
    assert response.json()["http_status"] == 401
    assert captured["method"] == "POST"
    assert captured["url"] == main.SERVICES[service] + "/events"
    assert captured["headers"] == traffic_headers("live")
    assert classify_traffic(Headers(captured["headers"])) == "live"
    assert "Authorization" not in captured["headers"]
    assert "X-MetaGuard-Grant" not in captured["headers"]


@pytest.mark.parametrize("hospital,site", [("hospital-a", "Hospital_A"), ("hospital-b", "Hospital_B")])
def test_enforcement_keeps_hospital_capture_available(client, monkeypatch, hospital, site):
    monkeypatch.setenv("METAGUARD_REQUIRED", "1")
    capture = AsyncMock(return_value=httpx.Response(201, json={"status": "pending"}))
    monkeypatch.setattr(main, "hospital_request", capture)
    response = client.post(f"/api/hospitals/{hospital}/capture", json={
        "event_id": "guard-test", "site_id": site,
        "event_type": "patient_registration", "base_priority": 2,
    })
    assert response.status_code == 200
    assert response.json()["http_status"] == 201
    assert capture.await_args.args == ("POST", main.HOSPITALS[hospital] + "/capture")
