"""Checkpoint 6 security/failure boundaries; separate from evaluation workloads."""
import json
from unittest.mock import Mock
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

from cloud.controller import main
from cloud.metaguard import hospital_identity, cloud_persist


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv('CONTROLLER_HOSPITAL_TOKENS', json.dumps({'Hospital_A': 'a'*40, 'Hospital_B': 'b'*40}))
    monkeypatch.setenv('CONTROLLER_CLOUD_TOKEN', 'private-operator-sentinel')


@pytest.mark.parametrize('header', ['a'*40, 'Basic ' + 'a'*40, 'Bearer', 'Bearer ', 'Bearer wrong', 'Bearer '+'a'*40+' '])
def test_non_bearer_and_invalid_credentials_rejected(credentials, header):
    with pytest.raises(HTTPException) as error:
        hospital_identity(header)
    assert error.value.status_code == 401
    assert 'a'*40 not in str(error.value.detail)


@pytest.mark.parametrize('scheme', ['Bearer', 'bearer', 'BEARER'])
def test_standard_bearer_scheme_is_case_insensitive(credentials, scheme):
    assert hospital_identity(scheme + ' ' + 'a'*40) == 'Hospital_A'


@pytest.mark.parametrize('action', ['acknowledge', 'cancel', 'complete', 'reconcile', 'abandon', 'resolve', 'status'])
def test_operator_invalid_uuid_rejected_before_storage(credentials, monkeypatch, action):
    storage = Mock()
    monkeypatch.setattr(main, 'storage', storage)
    path = '/grants/by-id/not-a-uuid' if action == 'status' else '/grants/not-a-uuid/' + action
    with TestClient(main.app, headers={'X-Cloud-Token': 'private-operator-sentinel'}, raise_server_exceptions=False) as client:
        response = client.get(path) if action == 'status' else client.post(path, json={'hospital_id': 'Hospital_A', 'applied': False})
    assert response.status_code == 422
    assert 'private-operator-sentinel' not in response.text
    for method in ('get_grant_status', 'acknowledge_grant', 'cancel_grant', 'complete_grant', 'resolve_uncertain', 'reconcile_authoritative'):
        getattr(storage, method).assert_not_called()


@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan')])
def test_protected_cloud_rejects_non_finite_payload_before_database(credentials, monkeypatch, value):
    monkeypatch.setenv('METAGUARD_REQUIRED', '1')
    request = Request({'type': 'http', 'headers': [(b'authorization', ('Bearer '+'a'*40).encode()),
                       (b'x-metaguard-grant', str(uuid4()).encode())]})
    event = {'event_id': 'E1', 'site_id': 'Hospital_A', 'event_type': 'patient_registration',
             'base_priority': 1, 'prerequisite_ids': [], 'payload': {'number': value}}
    save = Mock()
    with pytest.raises(HTTPException) as error:
        cloud_persist(event, request, save)
    assert error.value.status_code == 422
    save.assert_not_called()


@pytest.mark.parametrize('body', [{}, {'hospital_id': 'Hospital_A'},
    {'hospital_id': 'Hospital_A', 'event_id': '', 'priority': 1},
    {'hospital_id': 'Hospital_A', 'event_id': 'E', 'priority': True},
    {'hospital_id': 'Hospital_A', 'event_id': 'E', 'priority': 2147483648}])
def test_malformed_tickets_never_enter_storage(credentials, monkeypatch, body):
    storage = Mock()
    monkeypatch.setattr(main, 'storage', storage)
    with TestClient(main.app, headers={'Authorization': 'Bearer '+'a'*40}) as client:
        response = client.post('/tickets', json=body)
    assert response.status_code == 422
    storage.add_ticket.assert_not_called()


@pytest.mark.parametrize('path,body', [('/grants/reconcile-expired', None),
    ('/grants/' + str(uuid4()) + '/resolve', {'applied': True})])
def test_hospital_cannot_submit_authoritative_cloud_results(credentials, monkeypatch, path, body):
    storage = Mock()
    monkeypatch.setattr(main, 'storage', storage)
    with TestClient(main.app, headers={'Authorization': 'Bearer '+'a'*40}) as client:
        response = client.post(path, json=body)
    assert response.status_code == 403
    storage.resolve_uncertain.assert_not_called()
    storage.reconcile_expired.assert_not_called()


def test_legacy_sqlite_capture_and_recovery_rows_survive_additive_migration(tmp_path, monkeypatch):
    import sqlite3
    from edge.database import db
    database = tmp_path/'legacy.db'
    monkeypatch.setattr(db, 'DB_PATH', database)
    with sqlite3.connect(database) as conn:
        conn.execute('''CREATE TABLE events (event_id TEXT PRIMARY KEY, site_id TEXT,
            event_type TEXT, base_priority INTEGER, prerequisite_ids TEXT, payload TEXT, status TEXT)''')
        conn.execute("INSERT INTO events VALUES ('OLD','Hospital_A','patient_registration',2,'[]','{}','pending')")
        conn.execute('''CREATE TABLE recovery_state (event_id TEXT PRIMARY KEY, in_flight INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0, retry_after TEXT, lease_expires_at TEXT)''')
        conn.execute("INSERT INTO recovery_state(event_id) VALUES ('OLD')")
    db.init_db()
    assert db.get_events()[0]['event_id'] == 'OLD'
    assert db.get_events()[0]['status'] == 'pending'
    token = db.claim_event('OLD')
    assert token and db.acknowledge_event('OLD', token)
    assert db.get_events()[0]['status'] == 'applied'


def test_dashboard_smoke_owns_postgres_port_instead_of_inheriting_it(tmp_path, monkeypatch):
    from infrastructure.scripts import smoke_deployment as smoke
    monkeypatch.setenv('POSTGRES_PORT', '64505')
    requested = []
    def ports(count):
        requested.append(count)
        return list(range(40000, 40000+count))
    monkeypatch.setattr(smoke, 'free_ports', ports)
    captured = {}
    class SetupComplete(Exception):
        pass
    def project(directory, name, compose_file, settings, image, services, **kwargs):
        captured.update(settings)
        raise SetupComplete()
    monkeypatch.setattr(smoke, 'Project', project)
    with pytest.raises(SetupComplete):
        smoke.verify_layout('combined', tmp_path, 'unused-image', 'cg-unit', automatic=True)
    assert requested == [7] and captured['POSTGRES_PORT'] == 40006
