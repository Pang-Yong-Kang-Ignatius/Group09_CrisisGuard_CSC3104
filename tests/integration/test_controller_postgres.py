"""Opt-in real PostgreSQL tests, each confined to a newly created schema.

Set CONTROLLER_TEST_DATABASE_URL to an isolated test database. Never falls
back to DATABASE_URL or the application's demonstration database.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from fastapi.testclient import TestClient

from cloud.controller import main, storage as module
from cloud.controller.storage import ControllerStorage


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("CONTROLLER_CLOUD_TOKEN", "isolated-test-token")
    url = os.environ.get("CONTROLLER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set CONTROLLER_TEST_DATABASE_URL to an isolated PostgreSQL database")
    schema = "checkpoint1_" + uuid4().hex
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        return psycopg.connect(url, options=f"-c search_path={schema}")

    monkeypatch.setattr(module, "get_connection", connect)
    storage = ControllerStorage()
    try:
        storage.init_db()
        yield storage, connect
    finally:
        with psycopg.connect(url) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_candidate_replacement_preserves_waiting_fairness(store):
    storage, connect = store
    storage.set_candidate('A', 'old', 1, 'a'*64)
    storage.add_ticket('B', 'other', 2)
    with connect() as conn:
        conn.execute("UPDATE controller_tickets SET bypass_count=1 WHERE hospital_id='A'")
        before = conn.execute("SELECT queue_position,bypass_count FROM controller_tickets WHERE hospital_id='A'").fetchone()
    assert storage.set_candidate('A', 'urgent', 5, 'b'*64)['status'] == 'updated'
    with connect() as conn:
        after = conn.execute("SELECT queue_position,bypass_count FROM controller_tickets WHERE hospital_id='A'").fetchone()
    assert before == after
    grant = storage.issue_grant()
    assert grant['event_id'] == 'urgent'


def test_unsent_issued_grant_is_fenced_and_not_requeued(store):
    storage, _ = store
    storage.set_candidate('A', 'old', 1, 'a'*64)
    old = storage.issue_grant()
    assert storage.set_candidate('A', 'new', 5, 'b'*64)['status'] == 'updated'
    assert storage.acknowledge_grant(old['grant_id'], 'A') == 'invalid_state'
    assert storage.get_grant_status(old['grant_id'])['status'] == 'superseded'
    assert storage.issue_grant()['event_id'] == 'new'
    assert storage.set_candidate('A')['status'] == 'withdrawn'
    storage.reconcile_expired()
    assert storage.list_tickets() == []


@pytest.mark.parametrize('withdraw', [False, True])
def test_acknowledged_work_cannot_be_replaced_or_withdrawn(store, withdraw):
    storage, _ = store
    storage.set_candidate('A', 'old', 1, 'a'*64)
    grant = storage.issue_grant()
    assert storage.acknowledge_grant(grant['grant_id'], 'A') == 'acknowledged'
    result = storage.set_candidate('A') if withdraw else storage.set_candidate('A', 'new', 5, 'b'*64)
    assert result == {'status': 'pinned', 'event_id': 'old'}
    assert storage.get_grant_status(grant['grant_id'])['status'] == 'acknowledged'


def test_ack_and_replacement_are_mutually_exclusive(store):
    storage, _ = store
    for index in range(12):
        site = f'race-{index}'
        storage.set_candidate(site, 'old', 1, 'a'*64)
        grant = storage.issue_grant()
        assert grant['hospital_id'] == site
        with ThreadPoolExecutor(max_workers=2) as pool:
            ack = pool.submit(storage.acknowledge_grant, grant['grant_id'], site)
            switch = pool.submit(storage.set_candidate, site, 'new', 5, 'b'*64)
            ack_result, switch_result = ack.result(), switch.result()
        if ack_result == 'acknowledged':
            assert switch_result == {'status': 'pinned', 'event_id': 'old'}
        else:
            assert ack_result == 'invalid_state'
            assert switch_result['status'] == 'updated'
            storage.set_candidate(site)  # Remove this race's unallocated new ticket.


@pytest.mark.parametrize("hospital", ["Hospital_A", "Hospital_B"])
def test_ticket_and_grant_persist_across_storage_instances(store, hospital):
    storage, _ = store
    assert storage.add_ticket(hospital, "E1", 1)
    fresh = ControllerStorage()
    assert fresh.list_tickets() == [{"hospital_id": hospital, "event_id": "E1",
                                      "priority": 1, "bypass_count": 0}]
    grant = fresh.issue_grant()
    assert storage.list_tickets() == []
    assert ControllerStorage().get_grant(hospital, "E1") == grant
    assert storage.get_grant(hospital, "other") is None


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_bounded_priority_and_round_robin(store, limit):
    storage, _ = store
    storage.bypass_limit = limit
    storage.add_ticket("Hospital_A", "A1", 1)
    for index in range(limit + 1):
        storage.add_ticket("Hospital_B", f"B{index}", 5)
        grant = storage.issue_grant()
        if index < limit:
            assert grant["hospital_id"] == "Hospital_B"
            assert storage.list_tickets()[0]["bypass_count"] == index + 1
        else:
            assert grant["hospital_id"] == "Hospital_A"


def test_priority_update_and_tie_order(store):
    storage, _ = store
    storage.add_ticket("A", "A1", 1)
    storage.add_ticket("B", "B1", 5)
    assert not storage.update_priority("A", "wrong", 10)
    assert storage.update_priority("A", "A1", 5)
    assert storage.issue_grant()["hospital_id"] == "A"
    assert storage.issue_grant()["hospital_id"] == "B"
    assert storage.issue_grant() is None


def test_priority_update_keeps_bypass_count_and_position(store):
    storage, _ = store
    storage.add_ticket("A", "A1", 1)
    storage.add_ticket("B", "B1", 5)
    assert storage.issue_grant()["hospital_id"] == "B"
    assert storage.update_priority("A", "A1", 10)
    assert storage.list_tickets() == [{"hospital_id": "A", "event_id": "A1",
                                      "priority": 10, "bypass_count": 1}]
    storage.add_ticket("B", "B2", 20)
    assert storage.issue_grant()["hospital_id"] == "A"


def test_nonissued_grant_is_not_retrievable(store):
    storage, connect = store
    storage.add_ticket("A", "A1", 1)
    storage.issue_grant()
    with connect() as conn:
        conn.execute("UPDATE controller_grants SET status = 'completed'")
    assert storage.get_grant("A", "A1") is None


def test_expiry_and_permanent_duplicate_rejection(store):
    storage, connect = store
    storage.add_ticket("A", "A1", 1)
    grant = storage.issue_grant()
    assert not storage.add_ticket("A", "A1", 5)
    with connect() as conn:
        conn.execute("UPDATE controller_grants SET expires_at = %s",
                     (datetime.now(timezone.utc) - timedelta(seconds=1),))
    assert storage.get_grant("A", "A1") is None
    assert not storage.add_ticket("A", "A1", 1)
    with connect() as conn:
        assert conn.execute("SELECT grant_id::text, status FROM controller_grants").fetchone() == (grant["grant_id"], "issued")
    # Existing design permits another event while this hospital has a grant.
    assert storage.add_ticket("A", "A2", 1)


def test_concurrent_duplicate_and_conflicting_registration(store):
    storage, _ = store
    with ThreadPoolExecutor(max_workers=8) as pool:
        added = list(pool.map(lambda i: storage.add_ticket("A", f"A{i % 2}", 1), range(16)))
    assert sum(added) == 1
    assert len(storage.list_tickets()) == 1


def test_concurrent_grants_are_unique_and_match_tickets(store):
    storage, connect = store
    for hospital in ["A", "B"]:
        storage.add_ticket(hospital, hospital + "1", 1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: storage.issue_grant(), range(16)))
    grants = [grant for grant in results if grant is not None]
    assert {(g["hospital_id"], g["event_id"]) for g in grants} == {("A", "A1"), ("B", "B1")}
    assert len({g["grant_id"] for g in grants}) == 2
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM controller_grants").fetchone()[0] == 2


def test_registration_racing_allocation_cannot_regrant_event(store):
    storage, _ = store
    storage.add_ticket("A", "A1", 1)

    def operation(index):
        if index % 2:
            storage.add_ticket("A", "A1", 5)
            return None
        return storage.issue_grant()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(operation, range(24)))
    grants = [result for result in results if result is not None]
    assert len(grants) == 1
    assert storage.get_grant("A", "A1") == grants[0]
    assert storage.list_tickets() == []


def test_same_event_identifier_is_scoped_to_hospital(store):
    storage, _ = store
    for hospital in ["A", "B"]:
        assert storage.add_ticket(hospital, "shared-id", 1)
    assert storage.issue_grant()["hospital_id"] == "A"
    assert storage.issue_grant()["hospital_id"] == "B"
    assert storage.get_grant("A", "shared-id")["grant_id"] != storage.get_grant("B", "shared-id")["grant_id"]


def test_failed_grant_insert_rolls_back_queue_and_bypasses(store):
    storage, connect = store
    storage.add_ticket("A", "A1", 1)
    storage.add_ticket("B", "B1", 5)
    before = storage.list_tickets()
    with connect() as conn:
        conn.execute("ALTER TABLE controller_grants ADD CONSTRAINT injected_failure CHECK (false)")
    with pytest.raises(psycopg.errors.CheckViolation):
        storage.issue_grant()
    assert storage.list_tickets() == before
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM controller_grants").fetchone()[0] == 0
        conn.execute("ALTER TABLE controller_grants DROP CONSTRAINT injected_failure")
    assert storage.issue_grant()["hospital_id"] == "B"


def test_database_constraints(store):
    storage, connect = store
    storage.add_ticket("A", "A1", 1)
    with pytest.raises(psycopg.errors.UniqueViolation), connect() as conn:
        conn.execute("INSERT INTO controller_tickets VALUES ('A', 'A2', 5, 0, 99)")
    grant = storage.issue_grant()
    with pytest.raises(psycopg.errors.UniqueViolation), connect() as conn:
        conn.execute("INSERT INTO controller_grants VALUES (%s, 'A', 'A1', %s, 'issued')",
                     (str(uuid4()), grant["expires_at"]))
    with pytest.raises(psycopg.errors.UniqueViolation), connect() as conn:
        conn.execute("INSERT INTO controller_grants VALUES (%s, 'B', 'B1', %s, 'issued')",
                     (grant["grant_id"], grant["expires_at"]))


def test_http_with_real_storage(store, monkeypatch):
    storage, _ = store
    monkeypatch.setattr(main, "storage", storage)
    with TestClient(main.app, headers={"X-Cloud-Token": "isolated-test-token"}) as client:
        for hospital, priority in [("Hospital_A", 1), ("Hospital_B", 5)]:
            assert client.post("/tickets", json={"hospital_id": hospital,
                "event_id": hospital + "1", "priority": priority}).json()["added"]
        assert len(client.get("/tickets").json()) == 2
        for hospital in ["Hospital_B", "Hospital_A"]:
            issued = client.post("/grants/next")
            assert issued.status_code == 200
            assert issued.json()["hospital_id"] == hospital
            assert client.get(f"/grants/{hospital}/{hospital}1").json() == issued.json()
        assert client.post("/grants/next").json() == {"message": "No eligible hospitals"}

# Checkpoint 2 lifecycle tests (isolated schema fixture above).
def test_acknowledgement_is_durable_and_idempotent(store):
    storage, _ = store
    storage.add_ticket('A', 'A-ACK', 1)
    grant = storage.issue_grant()
    gid = grant['grant_id']
    assert storage.acknowledge_grant(gid, 'A') == 'acknowledged'
    assert ControllerStorage().acknowledge_grant(gid, 'A') == 'acknowledged'
    assert storage.get_grant_status(gid)['status'] == 'acknowledged'
    assert storage.acknowledge_grant(gid, 'B') == 'not_found'


def test_completed_twice_and_reject_invalid_transitions(store):
    storage, _ = store
    storage.add_ticket('A', 'A-COMPLETE', 1)
    gid = storage.issue_grant()['grant_id']
    assert storage.complete_grant(gid, 'A') == 'invalid_state'
    assert storage.acknowledge_grant(gid, 'A') == 'acknowledged'
    assert storage.complete_grant(gid, 'A') == 'completed'
    assert storage.complete_grant(gid, 'A') == 'completed'
    assert storage.cancel_grant(gid, 'A') == 'invalid_state'


def test_expired_unacknowledged_grant_requeues_once(store):
    storage, connect = store
    storage.add_ticket('A', 'A-EXPIRE', 2)
    old = storage.issue_grant()
    with connect() as conn:
        conn.execute('UPDATE controller_grants SET expires_at=NOW()-INTERVAL \'1 second\' WHERE grant_id=%s', (old['grant_id'],))
    first = storage.reconcile_expired()
    assert first['expired'] == 1
    assert first['requeued'] == 1
    assert storage.reconcile_expired()['requeued'] == 0
    new = storage.issue_grant()
    assert new['event_id'] == old['event_id']
    assert new['grant_id'] != old['grant_id']
    assert storage.get_grant_status(old['grant_id'])['status'] == 'expired'


def test_expired_acknowledged_requires_cloud_reconciliation(store):
    storage, connect = store
    storage.add_ticket('A', 'A-UNCERTAIN', 3)
    gid = storage.issue_grant()['grant_id']
    assert storage.acknowledge_grant(gid, 'A') == 'acknowledged'
    with connect() as conn:
        conn.execute('UPDATE controller_grants SET expires_at=NOW()-INTERVAL \'1 second\' WHERE grant_id=%s', (gid,))
    assert storage.reconcile_expired()['needs_reconciliation'] == 1
    assert storage.list_tickets() == []
    assert storage.resolve_uncertain(gid, True) == 'completed'
    assert storage.list_tickets() == []


def test_uncertain_not_applied_may_retry(store):
    storage, connect = store
    storage.add_ticket('A', 'A-RETRY', 2)
    gid = storage.issue_grant()['grant_id']
    storage.acknowledge_grant(gid, 'A')
    with connect() as conn:
        conn.execute('UPDATE controller_grants SET expires_at=NOW()-INTERVAL \'1 second\' WHERE grant_id=%s', (gid,))
    storage.reconcile_expired()
    assert storage.resolve_uncertain(gid, False) == 'expired'
    assert storage.list_tickets()[0]['event_id'] == 'A-RETRY'
    assert storage.issue_grant()['grant_id'] != gid


def test_cancel_unacknowledged_requeues(store):
    storage, _ = store
    storage.add_ticket('A', 'A-CANCEL', 1)
    gid = storage.issue_grant()['grant_id']
    assert storage.cancel_grant(gid, 'A') == 'cancelled'
    assert storage.cancel_grant(gid, 'A') == 'cancelled'
    assert len(storage.list_tickets()) == 1


def test_cancelled_grant_stays_withdrawn_during_reconciliation(store):
    storage, _ = store
    storage.set_candidate('Hospital_B', 'policy-priority', 5, 'a' * 64)
    grant = storage.issue_grant()
    assert storage.cancel_grant(grant['grant_id'], 'Hospital_B') == 'cancelled'
    assert storage.list_tickets()[0]['event_id'] == 'policy-priority'
    assert storage.set_candidate('Hospital_B')['status'] == 'withdrawn'

    storage.set_candidate('Hospital_A', 'recovery', 2, 'b' * 64)
    storage.reconcile_expired()
    assert storage.list_tickets() == [
        {'hospital_id': 'Hospital_A', 'event_id': 'recovery',
         'priority': 2, 'bypass_count': 0}]
    assert storage.issue_grant()['event_id'] == 'recovery'
    assert storage.get_grant_status(grant['grant_id'])['status'] == 'superseded'


def test_cancelled_grant_requeues_later_when_ticket_slot_was_busy(store):
    storage, _ = store
    storage.set_candidate('A', 'old', 1, 'a' * 64)
    old = storage.issue_grant()
    assert storage.add_ticket('A', 'new', 2, 'b' * 64)
    assert storage.cancel_grant(old['grant_id'], 'A') == 'cancelled'
    assert storage.list_tickets()[0]['event_id'] == 'new'

    newer = storage.issue_grant()
    assert newer['event_id'] == 'new'
    assert storage.reconcile_expired()['requeued'] == 1
    assert storage.list_tickets()[0]['event_id'] == 'old'
    assert storage.get_grant_status(old['grant_id'])['status'] == 'cancelled'


@pytest.mark.parametrize('retired_state', ['cancelled', 'expired'])
@pytest.mark.parametrize('replacement', [None, 'new-candidate'])
def test_explicit_candidate_change_retires_retry_history(store, retired_state, replacement):
    storage, connect = store
    storage.set_candidate('A', 'old-candidate', 1, 'a' * 64)
    old = storage.issue_grant()
    if retired_state == 'cancelled':
        assert storage.cancel_grant(old['grant_id'], 'A') == 'cancelled'
    else:
        with connect() as conn:
            conn.execute("UPDATE controller_grants SET expires_at=clock_timestamp()-INTERVAL '1 second' WHERE grant_id=%s",
                         (old['grant_id'],))
        assert storage.reconcile_expired()['expired'] == 1
    if replacement:
        assert storage.set_candidate('A', replacement, 2, 'b' * 64)['status'] == 'updated'
    else:
        assert storage.set_candidate('A')['status'] == 'withdrawn'
    for _ in range(2):
        assert storage.reconcile_expired()['requeued'] == 0
    assert storage.get_grant_status(old['grant_id'])['status'] == 'superseded'
    assert storage.acknowledge_grant(old['grant_id'], 'A') == 'invalid_state'
    assert [t['event_id'] for t in storage.list_tickets()] == ([replacement] if replacement else [])
    with connect() as conn:
        assert conn.execute("""SELECT previous_status,new_status FROM controller_grant_audit
            WHERE grant_id=%s AND action='candidate_change'""", (old['grant_id'],)).fetchall() == [
                (retired_state, 'superseded')]


def test_concurrent_expiry_sweep_only_requeues_once(store):
    storage, connect = store
    storage.add_ticket('A', 'A-RACE', 1)
    gid = storage.issue_grant()['grant_id']
    with connect() as conn:
        conn.execute('UPDATE controller_grants SET expires_at=NOW()-INTERVAL \'1 second\' WHERE grant_id=%s', (gid,))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: ControllerStorage().reconcile_expired(), range(4)))
    assert len(storage.list_tickets()) == 1
    assert storage.get_grant_status(gid)['status'] == 'expired'


@pytest.mark.parametrize('action', ['complete', 'cancel', 'resolve_applied', 'resolve_unapplied'])
def test_http_lifecycle_with_real_storage(store, monkeypatch, action):
    storage, connect = store
    monkeypatch.setattr(main, 'storage', storage)
    monkeypatch.setenv('CONTROLLER_CLOUD_TOKEN', 'isolated-test-token')
    trusted = {'X-Cloud-Token': 'isolated-test-token'}
    with TestClient(main.app, headers={"X-Cloud-Token": "isolated-test-token"}) as client:
        assert client.post('/tickets', json={'hospital_id': 'A', 'event_id': 'HTTP', 'priority': 2}).json()['added']
        gid = client.post('/grants/next').json()['grant_id']
        assert client.get(f'/grants/by-id/{gid}').json()['status'] == 'issued'
        assert client.post(f'/grants/{gid}/complete', json={'hospital_id': 'A'}, headers={'X-Cloud-Token': ''}).status_code in (401, 503)
        assert client.post('/grants/reconcile-expired', headers={'X-Cloud-Token': ''}).status_code in (401, 503)
        assert client.post(f'/grants/{gid}/resolve', json={'applied': True}, headers={'X-Cloud-Token': ''}).status_code in (401, 503)
        if action == 'cancel':
            for _ in range(2):
                assert client.post(f'/grants/{gid}/cancel', json={'hospital_id': 'A'}).json() == {'status': 'cancelled'}
            expected = 'cancelled'
        else:
            assert client.post(f'/grants/{gid}/acknowledge', json={'hospital_id': 'B'}).status_code == 404
            for _ in range(2):
                assert client.post(f'/grants/{gid}/acknowledge', json={'hospital_id': 'A'}).json() == {'status': 'acknowledged'}
            if action == 'complete':
                for _ in range(2):
                    assert client.post(f'/grants/{gid}/complete', json={'hospital_id': 'A'}, headers=trusted).json() == {'status': 'completed'}
                expected = 'completed'
            else:
                with connect() as conn:
                    conn.execute("UPDATE controller_grants SET expires_at=NOW()-INTERVAL '1 second' WHERE grant_id=%s", (gid,))
                assert client.post('/grants/reconcile-expired', headers=trusted).json()['needs_reconciliation'] == 1
                assert client.get('/tickets').json() == []
                applied = action == 'resolve_applied'
                expected = 'completed' if applied else 'expired'
                assert client.post(f'/grants/{gid}/resolve', json={'applied': applied}, headers=trusted).json() == {'status': expected}
        assert client.get(f'/grants/by-id/{gid}').json()['status'] == expected
        if expected in ('expired', 'cancelled'):
            assert client.post('/grants/next').json()['grant_id'] != gid
        else:
            assert not client.post('/tickets', json={'hospital_id': 'A', 'event_id': 'HTTP', 'priority': 2}).json()['added']


def test_failed_audit_rolls_back_acknowledgement(store):
    storage, connect = store
    storage.add_ticket('A', 'AUDIT-ROLLBACK', 1)
    gid = storage.issue_grant()['grant_id']
    with connect() as conn:
        conn.execute("ALTER TABLE controller_grant_audit ADD CONSTRAINT reject_ack CHECK (new_status <> 'acknowledged')")
    with pytest.raises(psycopg.errors.CheckViolation):
        storage.acknowledge_grant(gid, 'A')
    assert storage.get_grant_status(gid)['status'] == 'issued'
    with connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM controller_grant_audit WHERE grant_id=%s', (gid,)).fetchone()[0] == 1


def test_concurrent_acknowledgements_and_completions_are_audited_once(store):
    storage, connect = store
    storage.add_ticket('A', 'ACK-RACE', 1)
    gid = storage.issue_grant()['grant_id']
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(lambda _: storage.acknowledge_grant(gid, 'A'), range(16))) == {'acknowledged'}
        assert set(pool.map(lambda _: storage.complete_grant(gid, 'A'), range(16))) == {'completed'}
    with connect() as conn:
        assert conn.execute('SELECT new_status FROM controller_grant_audit WHERE grant_id=%s ORDER BY id', (gid,)).fetchall() == [('issued',), ('acknowledged',), ('completed',)]


@pytest.fixture
def integrated_store(store, monkeypatch):
    from cloud.database import db
    storage, connect = store
    monkeypatch.setattr(db, 'get_connection', connect)
    db.init_db()
    monkeypatch.setenv('CONTROLLER_HOSPITAL_TOKENS', '{"Hospital_A":"' + 'a'*40 + '","Hospital_B":"' + 'b'*40 + '"}')
    monkeypatch.setenv('METAGUARD_REQUIRED', '1')
    return storage, connect


def test_real_replay_switches_unsent_candidate_and_preserves_dependency_order(
    integrated_store, tmp_path, monkeypatch, caplog
):
    import json
    import logging
    import sqlite3
    from edge.database import db as edge_db
    from edge.capture_api import grant_client, scheduled_replay
    from cloud.patient_service.main import app as patient_app
    from cloud.metaguard import event_digest
    storage, connect = integrated_store
    monkeypatch.setattr(main, 'storage', storage)
    monkeypatch.setattr(edge_db, 'DB_PATH', tmp_path / 'hospital.db')
    monkeypatch.setenv('METAGUARD_ENABLED', '1')
    monkeypatch.setenv('SITE_ID', 'Hospital_A')
    edge_db.init_db()
    for name, priority, deps in [('A', 1, []), ('B', 5, ['A']), ('C', 3, [])]:
        edge_db.save_event(dict(event_id=name, site_id='Hospital_A', event_type='patient_registration',
                                base_priority=priority, prerequisite_ids=deps, payload={}))
    a = edge_db.get_events()[0]
    storage.set_candidate('Hospital_A', 'A', 5, event_digest(a))
    old = storage.issue_grant()
    grant_client._journal('A', None, 'registered')
    with sqlite3.connect(edge_db.DB_PATH) as conn:
        conn.execute("UPDATE recovery_state SET retry_after=? WHERE event_id='A'",
                     ((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),))
    sent = []
    with TestClient(main.app, headers={'Authorization': 'Bearer ' + 'a'*40}) as controller, TestClient(patient_app) as cloud:
        def request(method, path, body=None):
            response = controller.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        def send(event, *, grant_id):
            response = cloud.post('/events', json=event, headers={
                'Authorization': 'Bearer ' + 'a'*40, 'X-MetaGuard-Grant': grant_id})
            response.raise_for_status()
            sent.append(event['event_id'])
            return response.json()

        monkeypatch.setattr(grant_client, '_request', request)
        monkeypatch.setattr(scheduled_replay, 'send_to_cloud', send)
        with caplog.at_level(logging.INFO, logger=scheduled_replay.logger.name):
            assert scheduled_replay.replay_scheduled_events()[0]['event_id'] == 'C'
            assert sent == ['C']
            assert storage.get_grant_status(old['grant_id'])['status'] == 'superseded'
            assert storage.list_tickets() == []
            with sqlite3.connect(edge_db.DB_PATH) as conn:
                conn.execute("UPDATE recovery_state SET retry_after=NULL WHERE event_id='A'")
            scheduled_replay.replay_scheduled_events()
    assert sent == ['C', 'A', 'B']
    assert all(e['status'] == 'applied' for e in edge_db.get_events())
    selections = [json.loads(r.message.split(' ', 1)[1])['event_id'] for r in caplog.records
                  if r.message.startswith('scheduler.selection ')]
    assert selections == sent
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM events').fetchone()[0] == 3


def integrated_event(hospital='Hospital_A', kind='patient_registration'):
    return {'event_id': 'INTEGRATED-' + uuid4().hex, 'site_id': hospital, 'event_type': kind,
            'base_priority': 2, 'prerequisite_ids': [], 'payload': {'patient_id': 'synthetic'}}


def acknowledged_attempt(storage, event):
    from cloud.metaguard import event_digest
    assert storage.add_ticket(event['site_id'], event['event_id'], 2, event_digest(event))
    gid = storage.issue_grant()['grant_id']
    assert storage.acknowledge_grant(gid, event['site_id']) == 'acknowledged'
    return gid


@pytest.mark.parametrize('hospital', ['Hospital_A', 'Hospital_B'])
@pytest.mark.parametrize('service,kind', [('patient', 'patient_registration'), ('emergency', 'emergency_record'), ('admin', 'patient_discharge')])
def test_hospital_authenticated_cloud_completion(integrated_store, hospital, service, kind):
    import importlib
    storage, connect = integrated_store
    event = integrated_event(hospital, kind)
    gid = acknowledged_attempt(storage, event)
    token = ('a' if hospital == 'Hospital_A' else 'b') * 40
    headers = {'Authorization': 'Bearer ' + token, 'X-MetaGuard-Grant': gid}
    cloud = importlib.import_module(f'cloud.{service}_service.main')
    with TestClient(cloud.app) as client:
        assert client.post('/events', json=event).status_code == 401
        assert client.post('/events', json=event, headers={**headers, 'X-MetaGuard-Grant': str(uuid4())}).status_code == 409
        assert client.post('/events', json=event, headers=headers).status_code == 201
    assert ControllerStorage().reconcile_authoritative(gid, hospital, complete_only=True) == 'completed'
    with connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM events WHERE event_id=%s', (event['event_id'],)).fetchone()[0] == 1


def test_authoritative_absence_fences_late_send_and_reissues(integrated_store):
    from fastapi import HTTPException
    from cloud.database.db import save_cloud_event
    storage, _ = integrated_store
    event = integrated_event()
    old = acknowledged_attempt(storage, event)
    assert storage.reconcile_authoritative(old, 'Hospital_A') == 'expired'
    with pytest.raises(HTTPException) as error:
        save_cloud_event(event, grant_id=old)
    assert error.value.status_code == 409
    new = storage.issue_grant()['grant_id']
    assert new != old
    storage.acknowledge_grant(new, 'Hospital_A')
    assert save_cloud_event(event, grant_id=new) == 'applied'
    assert storage.reconcile_authoritative(new, 'Hospital_A') == 'completed'


def test_payload_and_identity_are_bound_to_grant(integrated_store):
    from fastapi import HTTPException
    from cloud.database.db import save_cloud_event
    storage, _ = integrated_store
    event = integrated_event()
    gid = acknowledged_attempt(storage, event)
    for changed in ({**event, 'site_id': 'Hospital_B'}, {**event, 'payload': {'patient_id': 'different'}}):
        with pytest.raises(HTTPException):
            save_cloud_event(changed, grant_id=gid)
    assert storage.reconcile_authoritative(gid, 'Hospital_A', complete_only=True) == 'unresolved'


def test_cross_hospital_id_collision_requires_operator_resolution(integrated_store):
    from cloud.database.db import ConflictingEventError, save_cloud_event
    storage, connect = integrated_store
    event_b = integrated_event('Hospital_B')
    gid_b = acknowledged_attempt(storage, event_b)
    assert save_cloud_event(event_b, grant_id=gid_b) == 'applied'
    assert storage.reconcile_authoritative(gid_b, 'Hospital_B') == 'completed'

    event_a = {**event_b, 'site_id': 'Hospital_A'}
    gid_a = acknowledged_attempt(storage, event_a)
    with pytest.raises(ConflictingEventError):
        save_cloud_event(event_a, grant_id=gid_a)
    with connect() as conn:
        conn.execute("UPDATE controller_grants SET expires_at=NOW()-INTERVAL '1 second' WHERE grant_id=%s", (gid_a,))
    assert storage.reconcile_expired()['needs_reconciliation'] == 1
    assert storage.reconcile_authoritative(gid_a, 'Hospital_A') == 'unresolved'
    assert storage.get_grant_status(gid_a)['status'] == 'reconciliation_required'
    assert storage.set_candidate('Hospital_A', 'OTHER', 2, 'a'*64) == {
        'status': 'pinned', 'event_id': event_a['event_id']}

    # The operator has confirmed the Cloud row belongs to B. Applied=False
    # means A was not applied; the existing authenticated route records this
    # decision and fences the old grant before the local A row is quarantined.
    assert storage.resolve_uncertain(gid_a, False) == 'expired'
    assert storage.set_candidate('Hospital_A', 'OTHER', 2, 'a'*64)['status'] == 'updated'
    assert storage.issue_grant()['event_id'] == 'OTHER'
    with connect() as conn:
        assert conn.execute('SELECT site_id,status FROM events WHERE event_id=%s',
                            (event_b['event_id'],)).fetchone() == ('Hospital_B', 'applied')
        assert conn.execute('SELECT new_status FROM controller_grant_audit WHERE grant_id=%s ORDER BY id',
                            (gid_a,)).fetchall() == [
                                ('issued',), ('acknowledged',),
                                ('reconciliation_required',), ('expired',), ('superseded',)]


def test_reconciliation_waits_for_in_flight_cloud_commit(integrated_store, monkeypatch):
    import time
    from cloud.database import db
    storage, connect = integrated_store
    event = integrated_event()
    gid = acknowledged_attempt(storage, event)
    app_name = 'cp4_' + uuid4().hex
    def writer_connection():
        conn = connect()
        conn.execute('SELECT set_config(\'application_name\', %s, false)', (app_name,))
        conn.commit()
        return conn
    monkeypatch.setattr(db, 'get_connection', writer_connection)
    with connect() as conn:
        conn.execute("""CREATE FUNCTION pause_apply() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM pg_sleep(0.8); RETURN NEW; END $$""")
        conn.execute('CREATE TRIGGER pause_apply BEFORE INSERT ON events FOR EACH ROW EXECUTE FUNCTION pause_apply()')
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(db.save_cloud_event, event, grant_id=gid)
        deadline = time.monotonic() + 10
        while True:
            with connect() as conn:
                paused = conn.execute("SELECT COUNT(*) FROM pg_stat_activity WHERE application_name=%s AND wait_event='PgSleep'", (app_name,)).fetchone()[0]
            if paused:
                break
            assert time.monotonic() < deadline
            time.sleep(.02)
        reconciler = pool.submit(storage.reconcile_authoritative, gid, 'Hospital_A')
        assert writer.result() == 'applied'
        assert reconciler.result() == 'completed'


def test_ack_request_lost_before_commit_can_be_safely_retired(integrated_store):
    from cloud.metaguard import event_digest
    storage, _ = integrated_store
    event = integrated_event()
    storage.add_ticket('Hospital_A', event['event_id'], 2, event_digest(event))
    gid = storage.issue_grant()['grant_id']
    assert storage.reconcile_authoritative(gid, 'Hospital_A') == 'expired'
    assert storage.acknowledge_grant(gid, 'Hospital_A') == 'invalid_state'


def test_permanent_rejection_does_not_block_hospital_slot(integrated_store):
    storage, _ = integrated_store
    event = integrated_event()
    gid = acknowledged_attempt(storage, event)
    assert storage.reconcile_authoritative(gid, 'Hospital_A', abandon=True) == 'rejected'
    storage.reconcile_expired()
    assert storage.list_tickets() == []
    assert storage.add_ticket('Hospital_A', 'OTHER', 1)


@pytest.mark.parametrize('number', [1.0, -0.0, 1e23, 1e-7, 10**70 + 12345])
def test_jsonb_numeric_representation_keeps_event_binding(integrated_store, number):
    from cloud.database.db import save_cloud_event
    storage, _ = integrated_store
    event = integrated_event()
    event['payload']['number'] = number
    gid = acknowledged_attempt(storage, event)
    assert save_cloud_event(event, grant_id=gid) == 'applied'
    assert storage.reconcile_authoritative(gid, 'Hospital_A') == 'completed'


@pytest.fixture
def admission_store(store, monkeypatch):
    from psycopg.types.json import Jsonb
    from tests.unit.test_admission import metrics
    storage, connect = store
    monkeypatch.setenv('CONTROLLER_ADMISSION_ENABLED', '1')
    with connect() as conn:
        conn.execute("""UPDATE controller_admission SET state='Normal',
            observed_at=clock_timestamp(), state_since=clock_timestamp()-INTERVAL '1 minute', metrics=%s""",
            (Jsonb(metrics(datetime.now(timezone.utc))),))
    return storage, connect


def test_experimental_pressure_policy_persists_health_and_fails_closed(admission_store, monkeypatch):
    from cloud.controller.admission import Settings
    from tests.unit.test_pressure_health import pressure_metrics
    storage, connect = admission_store
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY', 'aimd')
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES', '1')
    monkeypatch.setenv('CONTROLLER_ADMISSION_HEALTH_POLICY', 'pressure-experimental')
    config = Settings()
    with connect() as conn:
        conn.execute("UPDATE controller_admission SET state='Recovery', rate_rps=.2, healthy_count=0, observed_at=NULL")

    def observe(changes=None):
        with connect() as conn:
            conn.execute("UPDATE controller_admission SET next_poll_at=NULL, state_since=clock_timestamp()-INTERVAL '1 minute'")
        version = storage.claim_admission_poll(config)
        data = pressure_metrics(datetime.now(timezone.utc))
        if changes:
            data['patient'].update(changes)
        storage.update_admission_metrics(data, version, config)
        return ControllerStorage().admission_status()

    for _ in range(6):
        status = observe()
    assert status['state'] == 'Normal' and status['rate_rps'] == pytest.approx(.6)
    assert status['health_policy'] == 'pressure-experimental' and status['telemetry_fresh']
    status = observe({'executor_queue_depth': 80, 'executor_queue_pressure': 2})
    assert status['state'] == 'Recovery' and status['rate_rps'] == pytest.approx(.3)
    assert status['reason'] == 'patient:executor_queue_pressure'
    status = observe({'live_p95_latency_ms': None})
    assert status['state'] == 'Recovery' and not status['telemetry_fresh']
    storage.add_ticket('A', 'NO-UNOBSERVED-GRANT', 1)
    assert storage.issue_grant() is None
    assert storage.list_tickets()[0]['event_id'] == 'NO-UNOBSERVED-GRANT'


def test_admission_concurrent_allocators_share_one_durable_slot(admission_store, monkeypatch):
    monkeypatch.setenv('CONTROLLER_ADMISSION_NORMAL_RPS', '.1')
    monkeypatch.setenv('CONTROLLER_ADMISSION_CAUTION_RPS', '.05')
    monkeypatch.setenv('CONTROLLER_ADMISSION_RECOVERY_RPS', '.01')
    storage, connect = admission_store
    for i in range(8):
        storage.add_ticket(str(i), 'E'+str(i), i+1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        grants = list(pool.map(lambda _: ControllerStorage().issue_grant(), range(16)))
    assert sum(grant is not None for grant in grants) == 1
    assert len(storage.list_tickets()) == 7
    with connect() as conn:
        before = conn.execute('SELECT next_issue_at, allowed FROM controller_admission').fetchone()
    fresh = ControllerStorage()
    fresh.init_db()
    assert fresh.issue_grant() is None
    with connect() as conn:
        assert conn.execute('SELECT next_issue_at, allowed FROM controller_admission').fetchone() == before


def test_admission_denial_preserves_bypass_fairness(admission_store):
    storage, connect = admission_store
    storage.add_ticket('A', 'LOW', 1)
    storage.add_ticket('B', 'HIGH', 5)
    assert storage.issue_grant()['hospital_id'] == 'B'
    storage.add_ticket('B', 'HIGH2', 5)
    before = storage.list_tickets()
    assert storage.issue_grant() is None
    assert storage.list_tickets() == before
    assert before[0]['bypass_count'] == 1
    with connect() as conn:
        conn.execute("UPDATE controller_admission SET last_issue_at=clock_timestamp()-INTERVAL '1 second', next_issue_at=clock_timestamp()-INTERVAL '1 second'")
    assert storage.issue_grant()['hospital_id'] == 'A'


def test_admission_clock_and_counter_roll_back_on_grant_failure(admission_store):
    storage, connect = admission_store
    storage.add_ticket('A', 'ROLLBACK', 1)
    with connect() as conn:
        conn.execute('ALTER TABLE controller_grants ADD CONSTRAINT fail_allocation CHECK (false)')
    with pytest.raises(psycopg.errors.CheckViolation):
        storage.issue_grant()
    with connect() as conn:
        assert conn.execute('SELECT last_issue_at, next_issue_at, allowed FROM controller_admission').fetchone() == (None, None, 0)
    assert len(storage.list_tickets()) == 1


def test_recovery_ack_capacity_and_rate_are_shared_and_idempotent(admission_store, monkeypatch):
    storage, connect = admission_store
    monkeypatch.setenv('CONTROLLER_ADMISSION_ENABLED', '0')
    grants = []
    for hospital in ('A', 'B'):
        storage.add_ticket(hospital, hospital, 1)
        grants.append(storage.issue_grant()['grant_id'])
    monkeypatch.setenv('CONTROLLER_ADMISSION_ENABLED', '1')
    with connect() as conn:
        conn.execute("UPDATE controller_admission SET state='Recovery'")
    assert storage.acknowledge_grant(grants[0], 'A') == 'acknowledged'
    assert storage.acknowledge_grant(grants[0], 'A') == 'acknowledged'
    assert storage.acknowledge_grant(grants[1], 'B') == 'admission_deferred'
    assert storage.get_grant_status(grants[1])['status'] == 'issued'
    storage.complete_grant(grants[0], 'A')
    assert storage.acknowledge_grant(grants[1], 'B') == 'admission_deferred'
    with connect() as conn:
        conn.execute("UPDATE controller_admission SET last_ack_at=clock_timestamp()-INTERVAL '6 seconds', next_ack_at=clock_timestamp()-INTERVAL '6 seconds'")
    assert storage.acknowledge_grant(grants[1], 'B') == 'acknowledged'


def test_stale_telemetry_downgrades_even_without_poller(admission_store):
    storage, connect = admission_store
    with connect() as conn:
        conn.execute("UPDATE controller_admission SET observed_at=clock_timestamp()-INTERVAL '20 seconds'")
    status = ControllerStorage().admission_status()
    assert status['state'] == 'Recovery' and status['rate_rps'] == .2
    storage.add_ticket('A', 'A', 1)
    storage.add_ticket('B', 'B', 1)
    before = storage.list_tickets()
    assert storage.issue_grant() is None
    assert storage.issue_grant() is None
    assert storage.list_tickets() == before


def test_poll_lease_and_sample_versions_prevent_duplicate_recovery_votes(admission_store):
    from cloud.controller.admission import Settings
    from tests.unit.test_admission import metrics
    storage, connect = admission_store
    config = Settings()
    with ThreadPoolExecutor(max_workers=4) as pool:
        versions = list(pool.map(lambda _: ControllerStorage().claim_admission_poll(config), range(8)))
    assert sum(version is not None for version in versions) == 1
    version = next(version for version in versions if version is not None)
    with connect() as conn:
        conn.execute("UPDATE controller_admission SET state='Recovery', healthy_count=0, observed_at=NULL")
    sample = metrics(datetime.now(timezone.utc))
    storage.update_admission_metrics(sample, version+1, config)
    assert storage.admission_status()['healthy_samples'] == 0
    storage.update_admission_metrics(sample, version, config)
    assert storage.admission_status()['healthy_samples'] == 1
    storage.update_admission_metrics(sample, version, config)
    assert storage.admission_status()['state'] == 'Recovery'
    assert storage.admission_status()['healthy_samples'] == 0


def test_invalid_nan_metrics_persist_recovery_not_healthy_cache(admission_store):
    from cloud.controller.admission import Settings
    from tests.unit.test_admission import metrics
    storage, _ = admission_store
    version = storage.claim_admission_poll(Settings())
    sample = metrics(datetime.now(timezone.utc))
    sample['patient']['average_latency_ms'] = float('nan')
    storage.update_admission_metrics(sample, version, Settings())
    assert storage.admission_status()['state'] == 'Recovery'

@pytest.mark.parametrize('phase', ['issue', 'ack'])
def test_stale_telemetry_preserves_queue_and_rejects_new_work(admission_store, phase):
    storage, connect=admission_store
    storage.add_ticket('A','STALE',1)
    gid=storage.issue_grant()['grant_id'] if phase=='ack' else None
    before=storage.list_tickets()
    with connect() as conn:
        conn.execute("UPDATE controller_admission SET observed_at=clock_timestamp()-INTERVAL '1 minute'")
    if phase=='ack':
        assert storage.acknowledge_grant(gid,'A')=='admission_deferred'
        assert storage.get_grant_status(gid)['status']=='issued'
    else:
        assert storage.issue_grant() is None
    assert storage.list_tickets()==before
    snapshot=storage.admission_status()
    assert snapshot['telemetry_fresh'] is False
    assert snapshot['last_decision']['reason']=='telemetry_unavailable'


def test_aimd_rate_and_partial_healthy_window_survive_storage_restart(admission_store,monkeypatch):
    from cloud.controller.admission import Settings
    from tests.unit.test_admission import metrics
    storage,connect=admission_store
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY','aimd')
    config=Settings()
    with connect() as conn:
        conn.execute('UPDATE controller_admission SET rate_rps=1.6, rate_healthy_count=0')
    def sample(overloaded):
        with connect() as conn:
            conn.execute('UPDATE controller_admission SET next_poll_at=NULL')
        version=storage.claim_admission_poll(config)
        data=metrics(datetime.now(timezone.utc))
        if overloaded:data['patient']['average_latency_ms']=300
        storage.update_admission_metrics(data,version,config)
    sample(True)
    assert storage.admission_status()['rate_rps']==.8
    sample(False)
    fresh=ControllerStorage();fresh.init_db()
    assert fresh.admission_status()['rate_rps']==.8
    with connect() as conn:
        assert conn.execute('SELECT rate_healthy_count FROM controller_admission').fetchone()[0]==1
    sample(False);sample(False)
    assert fresh.admission_status()['rate_rps']==1.0
    storage.add_ticket('A','PACED',1)
    assert storage.issue_grant() is not None
    storage.add_ticket('B','WAIT',5)
    assert storage.issue_grant() is None
