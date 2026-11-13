"""HTTP contract tests; PostgreSQL behavior is tested separately."""
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from psycopg import OperationalError
from cloud.controller import main


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("CONTROLLER_CLOUD_TOKEN", "test-only")
    storage = Mock()
    monkeypatch.setattr(main, "storage", storage)
    with TestClient(main.app, headers={"X-Cloud-Token": "test-only"}) as client:
        storage.init_db.assert_called_once_with()
        yield client, storage


def test_candidate_replace_and_withdraw_contract(api):
    client, storage = api
    storage.set_candidate.return_value = {'status': 'updated', 'event_id': 'C'}
    body = {'event_id': 'C', 'priority': 5, 'event_digest': 'a' * 64}
    assert client.post('/hospitals/A/candidate', json=body).json()['status'] == 'updated'
    storage.set_candidate.assert_called_with('A', 'C', 5, 'a' * 64)
    assert client.post('/hospitals/A/candidate', json={}).status_code == 200
    storage.set_candidate.assert_called_with('A', None, None, None)


@pytest.mark.parametrize('body', [
    {'event_id': 'C'}, {'priority': 5}, {'event_id': ' ', 'priority': 5, 'event_digest': 'a'*64},
    {'event_id': 'C', 'priority': True, 'event_digest': 'a'*64},
])
def test_incomplete_candidate_rejected(api, body):
    client, storage = api
    assert client.post('/hospitals/A/candidate', json=body).status_code == 422
    storage.set_candidate.assert_not_called()


def test_health(api):
    client, _ = api
    assert client.get("/health").json() == {"status": "healthy"}


def test_operator_admission_status(api):
    client, storage = api
    storage.admission_status.return_value = {'state': 'Recovery', 'rate_rps': .2}
    assert client.get('/admission/status').json() == storage.admission_status.return_value


def test_ack_admission_deferral_is_not_lifecycle_ack(api):
    client, storage = api
    storage.acknowledge_grant.return_value = 'admission_deferred'
    response = client.post('/grants/00000000-0000-0000-0000-000000000001/acknowledge', json={'hospital_id': 'A'})
    assert response.status_code == 429 and response.headers['retry-after'] == '1'


@pytest.mark.parametrize("found", [True, False])
def test_grant_status_route_precedes_hospital_event_lookup(api, found):
    client, storage = api
    gid = "00000000-0000-0000-0000-000000000001"
    storage.get_grant_status.return_value = {"grant_id": gid, "status": "completed"} if found else None
    response = client.get(f"/grants/by-id/{gid}")
    assert response.status_code == (200 if found else 404)
    if found:
        assert response.json() == storage.get_grant_status.return_value
    storage.get_grant_status.assert_called_once_with(gid)
    storage.get_grant.assert_not_called()


@pytest.mark.parametrize('result,code', [('expired', 200), ('completed', 200), ('invalid_state', 409)])
def test_authoritative_resolution_response(api, monkeypatch, result, code):
    client, storage = api
    monkeypatch.setenv('CONTROLLER_CLOUD_TOKEN', 'test-only')
    storage.resolve_uncertain.return_value = result
    response = client.post('/grants/00000000-0000-0000-0000-000000000001/resolve', json={'applied': result == 'completed'},
                           headers={'X-Cloud-Token': 'test-only'})
    assert response.status_code == code
    if code == 200:
        assert response.json() == {'status': result}


@pytest.mark.parametrize("added", [True, False])
def test_submit_ticket(api, added):
    client, storage = api
    storage.add_ticket.return_value = added
    response = client.post("/tickets", json={
        "hospital_id": "Hospital_A", "event_id": "A001", "priority": 10})
    assert response.status_code == 200
    assert response.json() == {
        "message": "Ticket submitted" if added else "Ticket not added",
        "hospital_id": "Hospital_A", "added": added}
    storage.add_ticket.assert_called_once_with("Hospital_A", "A001", 10)


@pytest.mark.parametrize("changes", [
    {"hospital_id": ""}, {"hospital_id": " \t"}, {"event_id": "\n"},
    {"priority": 0}, {"priority": -1}, {"priority": True},
    {"priority": "5"}, {"priority": 1.5}, {"priority": 2147483648},
    {"event_id": None}])
def test_invalid_ticket(api, changes):
    client, storage = api
    body = {"hospital_id": "Hospital_A", "event_id": "A001", "priority": 1}
    body.update(changes)
    assert client.post("/tickets", json=body).status_code == 422
    storage.add_ticket.assert_not_called()


def test_missing_fields(api):
    client, storage = api
    assert client.post("/tickets", json={}).status_code == 422
    storage.add_ticket.assert_not_called()


def test_view_tickets(api):
    client, storage = api
    storage.list_tickets.return_value = [{"hospital_id": "Hospital_A", "event_id": "A001",
                                          "priority": 1, "bypass_count": 0}]
    response = client.get("/tickets")
    assert response.status_code == 200
    assert response.json() == storage.list_tickets.return_value


def test_grants(api):
    client, storage = api
    grant = {"grant_id": "test-id", "hospital_id": "Hospital_A", "event_id": "A001",
             "expires_at": "2030-01-01T00:00:00Z"}
    storage.issue_grant.return_value = grant
    assert client.post("/grants/next").json() == grant
    storage.get_grant.return_value = grant
    assert client.get("/grants/Hospital_A/A001").json() == grant
    storage.get_grant.assert_called_once_with("Hospital_A", "A001")
    storage.get_grant.return_value = None
    response = client.get("/grants/Hospital_A/missing")
    assert response.status_code == 404
    assert response.json() == {"detail": "No active grant found"}
    storage.issue_grant.return_value = None
    assert client.post("/grants/next").json() == {"message": "No eligible hospitals"}


@pytest.mark.parametrize("method,path,operation", [
    ("get", "/tickets", "list_tickets"), ("post", "/grants/next", "issue_grant"),
    ("get", "/grants/A/E", "get_grant"), ("post", "/tickets", "add_ticket")])
def test_database_unavailable(api, method, path, operation):
    client, storage = api
    getattr(storage, operation).side_effect = OperationalError("private connection details")
    kwargs = {"json": {"hospital_id": "A", "event_id": "E", "priority": 1}} if path == "/tickets" and method == "post" else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 503
    assert response.json() == {"detail": "Controller database unavailable"}
