"""Replay authorization and local dashboard boundary; no live databases."""
import pytest
from fastapi.testclient import TestClient
from edge.capture_api import main as hospital
from dashboard import main as dashboard


@pytest.mark.parametrize("enabled,token,auth,status", [
    ("0", "test-secret", "Bearer test-secret", 404),
    ("1", "", "", 503),
    ("1", "test-secret", "", 403),
    ("1", "test-secret", "Bearer wrong", 403),
    ("1", "test-secret", "Bearer test-secret", 200),
])
def test_hospital_authorization(monkeypatch, enabled, token, auth, status):
    monkeypatch.setenv("ENABLE_MANUAL_REPLAY", enabled)
    monkeypatch.setenv("MANUAL_REPLAY_TOKEN", token)
    calls = []
    monkeypatch.setattr(hospital, "replay_scheduled_events", lambda: calls.append(True) or [])
    # Docker-forwarded addresses must work with authorization.
    client = TestClient(hospital.app, client=("172.20.0.1", 1234))
    response = client.post("/replay", headers={"Authorization": auth})
    assert response.status_code == status
    assert bool(calls) == (status == 200)


@pytest.mark.parametrize("host,peer,headers,status", [
    ("127.0.0.1", "127.0.0.1", {}, 200),
    ("localhost", "127.0.0.1", {}, 200),
    ("127.0.0.1", "172.18.0.1", {}, 200),
    ("127.0.0.1", "8.8.8.8", {}, 403),
    ("127.0.0.1", "2001:4860:4860::8888", {}, 403),
    ("evil.example", "127.0.0.1", {}, 403),
    ("127.0.0.1", "192.0.2.1", {}, 403),
    ("127.0.0.1", "127.0.0.1", {"Origin": "https://evil.example"}, 403),
    ("127.0.0.1", "127.0.0.1", {"Sec-Fetch-Site": "cross-site"}, 403),
])
def test_dashboard_boundary(host, peer, headers, status):
    client = TestClient(dashboard.app, base_url=f"http://{host}", client=(peer, 1234))
    assert client.get("/", headers=headers).status_code == status


def test_dashboard_forwards_server_token(monkeypatch):
    monkeypatch.setenv("ENABLE_MANUAL_REPLAY", "1")
    monkeypatch.setenv("MANUAL_REPLAY_TOKEN", "test-secret")
    operator_token = "dashboard-operator-private-token-123456"
    monkeypatch.setenv("DASHBOARD_REPLAY_OPERATOR_TOKEN", operator_token)
    def request(method, url, **kwargs):
        assert method == 'POST'
        assert url == dashboard.HOSPITALS['hospital-a'] + '/replay'
        assert kwargs['headers'] == {'Authorization': 'Bearer test-secret'}
        import httpx
        return httpx.Response(200, json={'total_attempted': 0, 'applied': 0, 'results': []})
    monkeypatch.setattr(dashboard.httpx, 'request', request)
    client = TestClient(dashboard.app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
    response = client.post("/api/hospitals/hospital-a/replay",
                           headers={"Authorization": "Bearer " + operator_token})
    assert response.status_code == 200
    assert response.json()["http_status"] == 200
    assert "test-secret" not in response.text
