"""The optional Dashboard replay proxy must authenticate its caller."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from dashboard import main


OPERATOR_TOKEN = "dashboard-operator-private-token-123456"
HOSPITAL_TOKEN = "hospital-replay-private-token-123456"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENABLE_MANUAL_REPLAY", "1")
    monkeypatch.setenv("MANUAL_REPLAY_TOKEN", HOSPITAL_TOKEN)
    monkeypatch.setenv("DASHBOARD_REPLAY_OPERATOR_TOKEN", OPERATOR_TOKEN)
    with TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 1234)) as test_client:
        yield test_client


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token", OPERATOR_TOKEN,
                                           "Basic " + OPERATOR_TOKEN])
def test_replay_rejects_unauthorized_callers_before_hospital_request(
    client, monkeypatch, authorization
):
    hospital_request = AsyncMock()
    monkeypatch.setattr(main, "hospital_request", hospital_request)
    headers = {"Authorization": authorization} if authorization else {}
    response = client.post("/api/hospitals/hospital-a/replay", headers=headers)
    assert response.status_code == 403
    assert OPERATOR_TOKEN not in response.text
    assert HOSPITAL_TOKEN not in response.text
    hospital_request.assert_not_awaited()


@pytest.mark.parametrize("operator_token", ["", "too-short", HOSPITAL_TOKEN])
def test_replay_fails_closed_on_missing_weak_or_reused_operator_token(
    client, monkeypatch, operator_token
):
    monkeypatch.setenv("DASHBOARD_REPLAY_OPERATOR_TOKEN", operator_token)
    hospital_request = AsyncMock()
    monkeypatch.setattr(main, "hospital_request", hospital_request)
    response = client.post("/api/hospitals/hospital-a/replay",
                           headers={"Authorization": "Bearer " + operator_token})
    assert response.status_code == 503
    hospital_request.assert_not_awaited()


@pytest.mark.parametrize("hospital", ["hospital-a", "hospital-b"])
def test_authorized_replay_uses_server_side_hospital_credential(
    client, monkeypatch, hospital
):
    upstream = httpx.Response(200, json={"total_attempted": 1, "applied": 1, "results": []})
    hospital_request = AsyncMock(return_value=upstream)
    monkeypatch.setattr(main, "hospital_request", hospital_request)
    response = client.post(f"/api/hospitals/{hospital}/replay",
                           headers={"Authorization": "Bearer " + OPERATOR_TOKEN})
    assert response.status_code == 200
    assert response.json()["response"]["applied"] == 1
    hospital_request.assert_awaited_once_with(
        "POST", main.HOSPITALS[hospital] + "/replay", timeout=60.0,
        headers={"Authorization": "Bearer " + HOSPITAL_TOKEN},
    )


def test_disabled_replay_stays_disabled(client, monkeypatch):
    monkeypatch.setenv("ENABLE_MANUAL_REPLAY", "0")
    hospital_request = AsyncMock()
    monkeypatch.setattr(main, "hospital_request", hospital_request)
    response = client.post("/api/hospitals/hospital-a/replay",
                           headers={"Authorization": "Bearer " + OPERATOR_TOKEN})
    assert response.status_code == 404
    hospital_request.assert_not_awaited()


def test_replay_credentials_are_not_embedded_in_browser_assets(client):
    for path in ("/", "/static/app.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert OPERATOR_TOKEN not in response.text
        assert HOSPITAL_TOKEN not in response.text
