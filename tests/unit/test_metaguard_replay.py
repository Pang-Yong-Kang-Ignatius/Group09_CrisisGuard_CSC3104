import json
import sqlite3
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from cloud.controller import main
from cloud.metaguard import event_digest
from edge.capture_api import grant_client, scheduled_replay, auto_replay
from edge.database import db


@pytest.fixture
def authenticated(monkeypatch):
    monkeypatch.setenv('CONTROLLER_HOSPITAL_TOKENS', json.dumps({'Hospital_A': 'a'*40, 'Hospital_B': 'b'*40}))
    monkeypatch.setenv('CONTROLLER_CLOUD_TOKEN', 'operator-only')
    storage = Mock()
    storage.get_grant_status.return_value = {'hospital_id': 'Hospital_B'}
    monkeypatch.setattr(main, 'storage', storage)
    with TestClient(main.app, headers={'Authorization': 'Bearer ' + 'a'*40}) as client:
        yield client, storage


@pytest.mark.parametrize('method,path,body', [
    ('POST', '/hospitals/Hospital_B/candidate', {}),
    ('POST', '/tickets', {'hospital_id': 'Hospital_B', 'event_id': 'x', 'priority': 2}),
    ('GET', '/grants/Hospital_B/x', None),
    ('GET', '/hospitals/Hospital_B/events/x/grant', None),
    ('POST', '/hospitals/Hospital_B/grants/next', None),
    ('GET', '/tickets', None), ('POST', '/grants/next', None),
    *[('POST', '/grants/GID/' + action, {'hospital_id': 'Hospital_A'})
      for action in ('acknowledge', 'cancel', 'complete', 'reconcile', 'abandon')],
    ('GET', '/grants/by-id/GID', None),
])
def test_hospital_cannot_operate_other_hospital_or_operator_routes(authenticated, method, path, body):
    client, storage = authenticated
    path = path.replace('GID', str(uuid4()))
    response = client.request(method, path, json=body)
    assert response.status_code == 403
    storage.add_ticket.assert_not_called()
    storage.acknowledge_grant.assert_not_called()
    storage.complete_grant.assert_not_called()
    storage.cancel_grant.assert_not_called()
    storage.reconcile_authoritative.assert_not_called()


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer wrong'}, {'X-Cloud-Token': 'wrong'}])
def test_missing_or_bad_credentials_fail_closed(authenticated, headers):
    _, storage = authenticated
    with TestClient(main.app) as client:
        assert client.get('/tickets', headers=headers).status_code == 401
        assert client.get('/health').status_code == 200
    storage.list_tickets.assert_not_called()


@pytest.mark.parametrize('config', ['', '{}', '{bad', '{"A":"short"}', json.dumps({'A': 'a'*40, 'B': 'a'*40})])
def test_invalid_auth_configuration_fails_closed(authenticated, monkeypatch, config):
    client, _ = authenticated
    monkeypatch.setenv('CONTROLLER_HOSPITAL_TOKENS', config)
    assert client.get('/tickets').status_code == 503


@pytest.fixture
def local_event(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'hospital.db')
    monkeypatch.setenv('METAGUARD_ENABLED', '1')
    monkeypatch.setenv('SITE_ID', 'Hospital_A')
    db.init_db()
    event = {'event_id': 'E1', 'site_id': 'Hospital_A', 'event_type': 'patient_registration',
             'base_priority': 2, 'prerequisite_ids': [], 'payload': {'patient_id': 'synthetic'}}
    db.save_event(event)
    return event


@pytest.mark.parametrize('phase,state', [('ack_pending', 'issued'), ('sent', 'acknowledged'),
                                         ('sent', 'reconciliation_required')])
def test_ambiguous_attempt_never_resends(local_event, monkeypatch, phase, state):
    gid = str(uuid4())
    grant_client._journal('E1', gid, phase)
    db.init_db()  # Journal survives restart/schema initialization.
    calls = []
    def request(method, path, body=None):
        calls.append(path)
        if path == f'/grants/by-id/{gid}':
            return {'grant_id': gid, 'hospital_id': 'Hospital_A', 'event_id': 'E1',
                    'event_digest': event_digest(local_event), 'status': state}
        if method == 'GET':
            return {'grant': {'grant_id': gid, 'status': state, 'event_digest': event_digest(local_event)}}
        return {'status': 'unresolved'}
    monkeypatch.setattr(grant_client, '_request', request)
    send = Mock()
    assert grant_client.transmit(local_event, send)['status'] == 'awaiting_reconciliation'
    assert calls[-1] == f'/grants/{gid}/reconcile'
    send.assert_not_called()


def test_lost_cloud_response_reconciles_without_second_send(local_event, monkeypatch):
    gid = str(uuid4())
    state = {'value': 'issued', 'applied': False}
    def request(method, path, body=None):
        if path.endswith('/candidate'):
            return {'status': 'issued'}
        if path == f'/grants/by-id/{gid}':
            return {'grant_id': gid, 'hospital_id': 'Hospital_A', 'event_id': 'E1',
                    'event_digest': event_digest(local_event), 'status': state['value']}
        if method == 'GET':
            return {'grant': {'grant_id': gid, 'status': state['value'], 'event_digest': event_digest(local_event)}}
        if path.endswith('/acknowledge'):
            state['value'] = 'acknowledged'
            return {'status': 'acknowledged'}
        if path.endswith('/reconcile'):
            assert state['applied']
            return {'status': 'completed'}
        raise AssertionError(path)
    def send(event, *, grant_id):
        state['applied'] = True
        raise httpx.ReadTimeout('Response lost after durable apply')
    monkeypatch.setattr(grant_client, '_request', request)
    sender = Mock(side_effect=send)
    with pytest.raises(httpx.ReadTimeout):
        grant_client.transmit(local_event, sender)
    assert grant_client.transmit(local_event, sender)['status'] == 'applied'
    assert sender.call_count == 1


@pytest.mark.parametrize('old_result,expected_sent', [
    ('completed', ['E2']), ('expired', ['E1', 'E2']),
    ('invalid_state', ['E1', 'E2']),
    ('already_expired', ['E1', 'E2']),
])
def test_retired_outstanding_grant_recovers_and_replays_dependency_chain(
        local_event, monkeypatch, old_result, expected_sent):
    """A newer grant must not hide the outcome of a durable old send."""
    second = {**local_event, 'event_id': 'E2', 'prerequisite_ids': ['E1']}
    db.save_event(second)
    old_gid, new_gid, second_gid = (str(uuid4()) for _ in range(3))
    grant_client._journal('E1', old_gid, 'sent')
    calls = []
    state = {'second_issued': False, 'old_reconciled': old_result == 'already_expired'}

    def request(method, path, body=None):
        calls.append((method, path))
        if path == f'/grants/{old_gid}/reconcile':
            state['old_reconciled'] = True
            return {'status': old_result}
        if path == f'/grants/by-id/{old_gid}':
            return {'grant_id': old_gid, 'hospital_id': 'Hospital_A',
                    'event_id': 'E1', 'event_digest': event_digest(local_event),
                    'status': ('expired' if state['old_reconciled'] else 'acknowledged')}
        if method == 'GET' and 'event_id=E1' in path:
            return {'grant': {'grant_id': new_gid, 'status': 'issued',
                              'event_digest': event_digest(local_event)}}
        if method == 'GET' and 'event_id=E2' in path:
            return {'grant': ({'grant_id': second_gid, 'status': 'issued',
                               'event_digest': event_digest(second)}
                              if state['second_issued'] else None)}
        if path.endswith('/candidate'):
            return {'status': 'issued' if body and body['event_id'] == 'E1' else 'updated'}
        if path.endswith('/grants/next'):
            state['second_issued'] = True
            return {'waiting': False}
        if path.endswith('/acknowledge'):
            return {'status': 'acknowledged'}
        if path.endswith('/complete'):
            return {'status': 'completed'}
        raise AssertionError(path)

    sent = []
    def send(event, *, grant_id):
        sent.append(event['event_id'])
        return {'status': 'applied', 'event_id': event['event_id']}

    monkeypatch.setattr(grant_client, '_request', request)
    monkeypatch.setattr(scheduled_replay, 'send_to_cloud', send)
    results = scheduled_replay.replay_scheduled_events()
    if old_result != 'completed':
        assert results[0]['status'] == 'pending'
        assert grant_client.pinned_event() is None
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE recovery_state SET retry_after=NULL WHERE event_id='E1'")
        results = scheduled_replay.replay_scheduled_events()
    assert [result['status'] for result in results] == ['applied', 'applied']
    assert sent == expected_sent
    assert (('POST', f'/grants/{old_gid}/reconcile') in calls) == (old_result != 'already_expired')
    assert [event['status'] for event in db.get_events()] == ['applied', 'applied']


def test_recorded_grant_binding_is_checked_before_reconciliation(local_event, monkeypatch):
    gid = str(uuid4())
    grant_client._journal('E1', gid, 'sent')
    calls = []
    def request(method, path, body=None):
        calls.append((method, path))
        if path == f'/grants/by-id/{gid}':
            return {'grant_id': gid, 'hospital_id': 'Hospital_A',
                    'event_id': 'OTHER', 'event_digest': event_digest(local_event),
                    'status': 'acknowledged'}
        return {'grant': None}
    monkeypatch.setattr(grant_client, '_request', request)
    send = Mock()
    with pytest.raises(RuntimeError, match='Recorded grant'):
        grant_client.transmit(local_event, send)
    assert all(method != 'POST' for method, _ in calls)
    send.assert_not_called()


def test_cross_hospital_id_collision_fails_closed_without_resending(local_event, monkeypatch):
    """A conflicting Cloud row leaves the A grant uncertain and its queue intact."""
    gid = str(uuid4())
    grant_client._journal('E1', gid, 'sent')
    db.save_event({**local_event, 'event_id': 'INDEPENDENT'})
    calls = []
    def request(method, path, body=None):
        calls.append((method, path))
        if path == f'/grants/by-id/{gid}':
            return {'grant_id': gid, 'hospital_id': 'Hospital_A', 'event_id': 'E1',
                    'event_digest': event_digest(local_event), 'status': 'reconciliation_required'}
        if path == f'/grants/{gid}/reconcile':
            return {'status': 'unresolved'}
        if method == 'GET':
            return {'grant': {'grant_id': gid, 'event_digest': event_digest(local_event),
                              'status': 'reconciliation_required'}}
        raise AssertionError(path)
    monkeypatch.setattr(grant_client, '_request', request)
    sender = Mock()
    monkeypatch.setattr(scheduled_replay, 'send_to_cloud', sender)
    result = scheduled_replay.replay_scheduled_events()
    assert [item['event_id'] for item in result] == ['E1']
    assert result[0]['status'] == 'pending'
    assert grant_client.pinned_event() == 'E1'
    assert [event['status'] for event in db.get_events()] == ['pending', 'pending']
    assert ('POST', f'/grants/{gid}/reconcile') in calls
    sender.assert_not_called()


def test_operator_quarantine_leaves_collision_and_replays_unrelated_chain(local_event, monkeypatch):
    """After an external authoritative resolution, only valid work advances."""
    gid = str(uuid4())
    grant_client._journal('E1', gid, 'sent')
    db.save_event({**local_event, 'event_id': 'INDEPENDENT'})
    db.save_event({**local_event, 'event_id': 'DEPENDENT',
                   'prerequisite_ids': ['INDEPENDENT']})
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("""UPDATE recovery_state SET quarantined=1,
            quarantine_reason='Cross-hospital event ID collision'
            WHERE event_id='E1' AND quarantined=0""")
    assert grant_client.pinned_event() is None
    grants = {}
    def request(method, path, body=None):
        if method == 'GET':
            event_id = path.split('event_id=', 1)[1]
            return {'grant': ({'grant_id': grants[event_id], 'status': 'issued',
                               'event_digest': event_digest(next(
                                   e for e in db.get_events() if e['event_id'] == event_id))}
                              if event_id in grants else None)}
        if path.endswith('/candidate'):
            return {'status': 'updated'}
        if path.endswith('/grants/next'):
            return {'waiting': False}
        if path.endswith('/acknowledge'):
            return {'status': 'acknowledged'}
        if path.endswith('/complete'):
            return {'status': 'completed'}
        raise AssertionError(path)
    def advertise(event=None):
        if event:
            grants[event['event_id']] = str(uuid4())
            return {'status': 'updated'}
        return {'status': 'withdrawn'}
    sent = []
    def send(event, *, grant_id):
        sent.append(event['event_id'])
        return {'status': 'applied', 'event_id': event['event_id']}
    monkeypatch.setattr(grant_client, '_request', request)
    monkeypatch.setattr(grant_client, 'advertise_candidate', advertise)
    monkeypatch.setattr(scheduled_replay, 'send_to_cloud', send)
    result = scheduled_replay.replay_scheduled_events()
    assert [item['status'] for item in result] == ['applied', 'applied']
    assert sent == ['INDEPENDENT', 'DEPENDENT']
    assert [(e['event_id'], e['status']) for e in db.get_events()] == [
        ('E1', 'pending'), ('INDEPENDENT', 'applied'), ('DEPENDENT', 'applied')]
    with sqlite3.connect(db.DB_PATH) as conn:
        assert conn.execute("SELECT grant_id,phase FROM metaguard_attempts WHERE event_id='E1'").fetchone() == (gid, 'sent')
        assert conn.execute("SELECT quarantined FROM recovery_state WHERE event_id='E1'").fetchone() == (1,)


def test_no_grant_means_no_cloud_send(local_event, monkeypatch):
    monkeypatch.setattr(grant_client, '_request', lambda method, path, body=None:
                        {'status': 'updated'} if path.endswith('/candidate') else {'grant': None})
    send = Mock()
    assert grant_client.transmit(local_event, send)['status'] == 'waiting_for_grant'
    send.assert_not_called()


def test_controller_outage_preserves_sqlite_capture_and_pending_attempt(local_event, monkeypatch):
    monkeypatch.setattr(grant_client, '_request', Mock(side_effect=httpx.ConnectError('unavailable')))
    send = Mock()
    monkeypatch.setattr(scheduled_replay, 'send_to_cloud', send)
    assert scheduled_replay.replay_scheduled_events()[0]['status'] == 'pending'
    assert db.get_events()[0]['status'] == 'pending'
    assert grant_client.outstanding_event() == 'E1'
    second = {**local_event, 'event_id': 'E2'}
    db.save_event(second)
    assert len(db.get_events()) == 2
    send.assert_not_called()


def test_legacy_startup_reconciler_is_bypassed_in_grant_mode(local_event, monkeypatch):
    lookup = Mock(side_effect=AssertionError('Legacy lookup must not release grants'))
    monkeypatch.setattr(auto_replay, 'get_cloud_event_status', lookup)
    db.claim_event('E1')
    auto_replay.reconcile_in_flight_events()
    lookup.assert_not_called()
    assert db.get_in_flight_events()[0]['event_id'] == 'E1'


def test_digest_preserves_json_types_and_exact_large_numbers(local_event):
    def digest(value):
        return event_digest({**local_event, 'payload': {'value': value}})
    assert digest(1) == digest(1.0)
    assert digest(0) == digest(-0.0)
    assert digest(10**23) == digest(1e23)
    assert digest(True) != digest(1) != digest('1')
    assert digest(10**70 + 1) != digest(10**70 + 2)


def test_grant_registration_uses_inherited_priority_without_mutating_event(local_event, monkeypatch):
    db.save_event({**local_event, 'event_id': 'URGENT', 'base_priority': 5, 'prerequisite_ids': ['E1']})
    calls = []
    def request(method, path, body=None):
        calls.append((path, body))
        return {'status': 'updated'} if path.endswith('/candidate') else {'grant': None}
    monkeypatch.setattr(grant_client, '_request', request)
    scheduled_replay.replay_scheduled_events()
    ticket = next(body for path, body in calls if path.endswith('/candidate') and body)
    assert ticket['event_id'] == 'E1' and ticket['priority'] == 5
    assert ticket['event_digest'] == event_digest(local_event)
    assert next(e for e in db.get_events() if e['event_id'] == 'E1')['base_priority'] == 2


@pytest.mark.parametrize('event_id', ['ward/42', '..', 'a?b#c', 'with space', '患者'])
def test_query_lookup_and_priority_preserve_valid_event_ids(authenticated, event_id):
    client, storage = authenticated
    storage.latest_grant.return_value = None
    storage.update_priority.return_value = True
    response = client.get('/hospitals/Hospital_A/grant', params={'event_id': event_id})
    assert response.status_code == 200
    storage.latest_grant.assert_called_once_with('Hospital_A', event_id)
    response = client.post('/hospitals/Hospital_A/priority', params={'event_id': event_id}, json={'priority': 5})
    assert response.status_code == 200
    storage.update_priority.assert_called_once_with('Hospital_A', event_id, 5)
