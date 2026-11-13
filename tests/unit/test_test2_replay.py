"""Test 2's actual self-cycle and the intended two-event MetaGuard replay."""
import sqlite3
from unittest.mock import Mock
from uuid import uuid4

import pytest

from cloud.metaguard import event_digest
from edge.capture_api import grant_client, scheduled_replay
from edge.database import db
from edge.scheduler.live_preview import inspect_hospital_queue


@pytest.fixture
def hospital_b(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'hospital-b.db')
    monkeypatch.setenv('SITE_ID', 'Hospital_B')
    monkeypatch.setenv('METAGUARD_ENABLED', '1')
    monkeypatch.setenv('METAGUARD_RETRY_POLICY', 'failure-backoff')
    db.init_db()
    registration = dict(event_id='CG-LECT-B-REG', site_id='Hospital_B',
                        event_type='patient_registration', base_priority=1,
                        prerequisite_ids=[], payload={'patient_id': 'synthetic-test-2'})
    encounter = dict(event_id='CG-LECT-B-ENC', site_id='Hospital_B',
                     event_type='encounter_creation', base_priority=5,
                     prerequisite_ids=[registration['event_id']],
                     payload={'patient_id': 'synthetic-test-2'})
    db.save_event(registration)
    db.save_event(encounter)
    return registration, encounter


def test_self_dependent_encounter_is_intentionally_unschedulable(hospital_b, monkeypatch):
    registration, encounter = hospital_b
    token = db.claim_event(registration['event_id'])
    assert db.acknowledge_event(registration['event_id'], token)
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute('UPDATE events SET prerequisite_ids=? WHERE event_id=?',
                     ('["CG-LECT-B-ENC"]', encounter['event_id']))
    request = Mock(return_value={'status': 'withdrawn'})
    monkeypatch.setattr(grant_client, '_request', request)

    preview = inspect_hospital_queue()
    assert preview['next_event_id'] is None
    assert preview['outstanding_event_id'] is None
    assert db.count_replayable_events() == 1
    assert preview['events'][0]['reason'] == 'Dependency cycle or downstream of cycle'
    assert scheduled_replay.replay_scheduled_events() == []
    assert next(e for e in db.get_events() if e['event_id'] == encounter['event_id'])['status'] == 'pending'
    request.assert_called_once_with('POST', '/hospitals/Hospital_B/candidate', {})


@pytest.mark.parametrize('first_grant_deferred', [False, True])
def test_correct_chain_replays_in_order_with_consecutive_grants(
        hospital_b, monkeypatch, first_grant_deferred):
    registration, encounter = hospital_b
    preview = inspect_hospital_queue()
    assert preview['next_event_id'] == registration['event_id']
    assert next(e for e in preview['events'] if e['event_id'] == registration['event_id'])['effective_priority'] == 5
    grants = {}
    candidate = [None]
    deferred = [False]
    sends = []

    def request(method, path, body=None):
        if path.endswith('/candidate'):
            candidate[0] = body['event_id'] if body else None
            return {'status': 'updated'}
        if method == 'GET':
            from urllib.parse import parse_qs, urlsplit
            event_id = parse_qs(urlsplit(path).query)['event_id'][0]
            return {'grant': grants.get(event_id)}
        if path.endswith('/grants/next'):
            if first_grant_deferred and not deferred[0]:
                deferred[0] = True
                return {'waiting': True}
            event = next(e for e in db.get_events() if e['event_id'] == candidate[0])
            assert event['status'] == 'pending'
            grant = dict(grant_id=str(uuid4()), status='issued',
                         event_digest=event_digest(event))
            grants[candidate[0]] = grant
            return grant
        if path.endswith('/acknowledge'):
            assert any(g['grant_id'] in path for g in grants.values())
            return {'status': 'acknowledged'}
        if path.endswith('/complete'):
            assert any(g['grant_id'] in path for g in grants.values())
            return {'status': 'completed'}
        raise AssertionError(path)

    def send(event, *, grant_id):
        assert grants[event['event_id']]['grant_id'] == grant_id
        if event['event_id'] == encounter['event_id']:
            assert next(e for e in db.get_events() if e['event_id'] == registration['event_id'])['status'] == 'applied'
        sends.append(event['event_id'])
        return {'status': 'applied', 'event_id': event['event_id']}

    monkeypatch.setattr(grant_client, '_request', request)
    monkeypatch.setattr(scheduled_replay, 'send_to_cloud', send)
    if first_grant_deferred:
        first = scheduled_replay.replay_scheduled_events()
        assert first[0]['event_id'] == registration['event_id']
        assert first[0]['status'] == 'pending'
        assert sends == []
        assert grant_client.pinned_event() is None
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute('UPDATE recovery_state SET retry_after=NULL WHERE event_id=?',
                         (registration['event_id'],))

    results = scheduled_replay.replay_scheduled_events()
    assert [r['status'] for r in results] == ['applied', 'applied']
    assert sends == [registration['event_id'], encounter['event_id']]
    assert grants[registration['event_id']]['grant_id'] != grants[encounter['event_id']]['grant_id']
    assert all(e['status'] == 'applied' for e in db.get_events())
    assert scheduled_replay.replay_scheduled_events() == []
    assert len(sends) == 2
