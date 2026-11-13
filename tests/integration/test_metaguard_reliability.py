"""Isolated-schema Checkpoint 6 reliability tests; no evaluation artifacts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from cloud.controller import main
from cloud.controller.storage import ControllerStorage
from tests.integration.test_controller_postgres import (
    store, integrated_store, admission_store, integrated_event, acknowledged_attempt,
)


@pytest.mark.parametrize('state', ['issued', 'acknowledged'])
def test_legacy_rows_survive_migration_and_recover_conservatively(store, state):
    storage, connect = store
    gid = str(uuid4())
    with connect() as conn:
        conn.execute('DROP TABLE controller_grants')
        conn.execute('''CREATE TABLE controller_grants (
            grant_id UUID PRIMARY KEY, hospital_id TEXT NOT NULL, event_id TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'issued',
            UNIQUE(hospital_id,event_id), CONSTRAINT unrelated_unique UNIQUE(hospital_id,expires_at))''')
        conn.execute('INSERT INTO controller_grants VALUES (%s,%s,%s,%s,%s)',
                     (gid, 'A', 'LEGACY', datetime.now(timezone.utc)-timedelta(seconds=1), state))
    ControllerStorage().init_db()
    report = storage.reconcile_expired()
    old = storage.get_grant_status(gid)
    assert old['grant_id'] == gid and old['event_digest'] is None
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pg_constraint WHERE conrelid='controller_grants'::regclass AND conname='unrelated_unique'").fetchone()[0] == 1
    if state == 'issued':
        assert report['requeued'] == 1
        assert storage.list_tickets()[0]['priority'] == 1
        assert storage.issue_grant()['grant_id'] != gid
    else:
        assert old['status'] == 'reconciliation_required'
        assert storage.list_tickets() == []
        assert storage.reconcile_authoritative(gid, 'A') == 'unresolved'


def test_sustained_priority_imbalance_keeps_low_priority_bounded(store):
    storage, _ = store
    low_count, high_count, gap, maximum_gap = 0, 0, 0, 0
    storage.add_ticket('B', 'LOW0', 1)
    while low_count < 40:
        if not any(t['hospital_id'] == 'A' for t in storage.list_tickets()):
            storage.add_ticket('A', 'HIGH' + str(high_count), 5)
        grant = storage.issue_grant()
        if grant['hospital_id'] == 'B':
            maximum_gap = max(maximum_gap, gap)
            assert gap <= 2  # One predecessor plus the existing one-bypass budget.
            low_count += 1
            gap = 0
            if low_count < 40:
                storage.add_ticket('B', 'LOW' + str(low_count), 1)
        else:
            high_count += 1
            gap += 1
        storage.acknowledge_grant(grant['grant_id'], grant['hospital_id'])
        storage.complete_grant(grant['grant_id'], grant['hospital_id'])
    assert low_count == 40 and high_count >= 40 and maximum_gap <= 2


def test_concurrent_identical_cloud_retries_apply_once(integrated_store):
    from cloud.database.db import save_cloud_event
    storage, connect = integrated_store
    event = integrated_event()
    gid = acknowledged_attempt(storage, event)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: save_cloud_event(event, grant_id=gid), range(16)))
    assert results.count('applied') == 1 and results.count('already_applied') == 15
    assert storage.reconcile_authoritative(gid, event['site_id']) == 'completed'
    with connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1


def test_out_of_order_application_is_not_mistaken_for_completion(integrated_store):
    from cloud.database.db import save_cloud_event, MissingPrerequisitesError
    storage, _ = integrated_store
    event = integrated_event()
    event['prerequisite_ids'] = ['MISSING-PARENT']
    gid = acknowledged_attempt(storage, event)
    with pytest.raises(MissingPrerequisitesError):
        save_cloud_event(event, grant_id=gid)
    assert storage.reconcile_authoritative(gid, event['site_id'], complete_only=True) == 'unresolved'
    assert storage.reconcile_authoritative(gid, event['site_id']) == 'expired'
    with pytest.raises(HTTPException):
        save_cloud_event(event, grant_id=gid)


def test_conflicting_cloud_content_never_completes_or_blindly_requeues(integrated_store):
    from cloud.database.db import save_cloud_event
    storage, _ = integrated_store
    event = integrated_event()
    gid = acknowledged_attempt(storage, event)
    save_cloud_event({**event, 'payload': {'patient_id': 'different'}}, grant_id=None)
    assert storage.reconcile_authoritative(gid, event['site_id']) == 'unresolved'
    assert storage.list_tickets() == []


@pytest.mark.parametrize('action', ['acknowledge', 'cancel', 'complete', 'reconcile', 'abandon', 'resolve', 'status'])
def test_invalid_operator_uuid_is_sanitized_with_real_storage(store, monkeypatch, action):
    storage, _ = store
    monkeypatch.setattr(main, 'storage', storage)
    with TestClient(main.app, headers={'X-Cloud-Token': 'isolated-test-token'}) as client:
        path = '/grants/by-id/bad' if action == 'status' else '/grants/bad/' + action
        response = client.get(path) if action == 'status' else client.post(path, json={'hospital_id': 'A', 'applied': False})
    assert response.status_code == 422
    for private in ('isolated-test-token', 'postgresql://', 'Traceback', 'psycopg'):
        assert private not in response.text


def test_failed_ack_audit_rolls_back_admission_reservation(admission_store):
    storage, connect = admission_store
    storage.add_ticket('A', 'ACK-ROLLBACK', 1)
    gid = storage.issue_grant()['grant_id']
    with connect() as conn:
        conn.execute("ALTER TABLE controller_grant_audit ADD CONSTRAINT reject_ack CHECK (new_status<>'acknowledged')")
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        storage.acknowledge_grant(gid, 'A')
    assert storage.get_grant_status(gid)['status'] == 'issued'
    with connect() as conn:
        assert conn.execute('SELECT last_ack_at,next_ack_at,allowed FROM controller_admission').fetchone() == (None, None, 1)


@pytest.mark.parametrize('form', ['urn', 'hex', 'upper'])
def test_uuid_forms_are_normalized_before_postgres(store, monkeypatch, form):
    storage, connect = store
    gid = uuid4()
    spelling = {'urn': gid.urn, 'hex': gid.hex, 'upper': str(gid).upper()}[form]
    if form == 'urn':
        import psycopg
        with pytest.raises(psycopg.errors.InvalidTextRepresentation), connect() as conn:
            conn.execute('SELECT %s::uuid', (spelling,))
    monkeypatch.setattr(main, 'storage', storage)
    with TestClient(main.app, headers={'X-Cloud-Token':'isolated-test-token'}) as client:
        assert client.get('/grants/by-id/'+spelling).status_code == 404
