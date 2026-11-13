"""Hospital dashboard requests remain independent of Cloud DNS failures."""
import asyncio
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from dashboard import main


@pytest.mark.parametrize("status", ["pending", "applied"])
def test_hospital_queue_uses_worker_resolver(monkeypatch, status):
    caller = threading.get_ident()
    urls = []

    def request(method, url, **kwargs):
        assert threading.get_ident() != caller
        urls.append(url)
        return httpx.Response(200, json=[{"event_id": "offline", "status": status}],
                              request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", request)
    # Hospital access must not use the event loop's DNS pool at all.
    async def check():
        async def unavailable(*args, **kwargs):
            raise AssertionError("Shared async resolver was used")
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", unavailable)
        return await main.hospital_status()

    results = asyncio.run(check())
    assert len(urls) == 2
    for hospital in results.values():
        assert hospital["status"] == "healthy"
        assert hospital["total"] == 1
        assert hospital[status] == 1
        assert hospital["events"][0]["event_id"] == "offline"


@pytest.mark.parametrize("failure", ["connect", "http", "json", "shape"])
def test_queue_failure_is_explicit_and_other_hospital_survives(monkeypatch, failure):
    def request(method, url, **kwargs):
        req = httpx.Request(method, url)
        if url.startswith(main.HOSPITALS["hospital-a"]):
            if failure == "connect":
                raise httpx.ConnectError("DNS unavailable", request=req)
            if failure == "http":
                return httpx.Response(503, request=req)
            if failure == "json":
                return httpx.Response(200, text="invalid", request=req)
            return httpx.Response(200, json={"events": []}, request=req)
        return httpx.Response(200, json=[], request=req)

    monkeypatch.setattr(httpx, "request", request)
    result = asyncio.run(main.hospital_status())
    assert result["hospital-a"]["status"] == "unavailable"
    assert result["hospital-a"]["error"]
    assert result["hospital-b"]["status"] == "healthy"
    assert result["hospital-b"]["total"] == 0


@pytest.mark.parametrize("hospital,site", [("hospital-a", "Hospital_A"), ("hospital-b", "Hospital_B")])
def test_edge_capture_routes_to_selected_hospital(monkeypatch, hospital, site):
    def request(method, url, **kwargs):
        assert method == "POST"
        assert url == main.HOSPITALS[hospital] + "/capture"
        assert kwargs["json"]["site_id"] == site
        return httpx.Response(201, json={"status": "pending"})

    monkeypatch.setattr(httpx, "request", request)
    with TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        response = client.post(f"/api/hospitals/{hospital}/capture", json={
            "event_id": "offline", "site_id": site, "event_type": "patient_registration",
            "base_priority": 2, "payload": {},
        })
    assert response.json() == {"http_status": 201, "response": {"status": "pending"}}
