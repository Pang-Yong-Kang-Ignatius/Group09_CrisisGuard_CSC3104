import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from cloud.controller.experimental import choose, controller_settings, replay_settings
from edge.scheduler.scheduler import select_next_event
from edge.capture_api import scheduled_replay, grant_client
from edge.database import db


def tickets():
    return [dict(hospital_id='A', event_id='a', priority=1, bypass_count=0),
            dict(hospital_id='B', event_id='b', priority=5, bypass_count=0)]


def test_controller_baselines_and_ties():
    rows = tickets()
    assert choose(rows, 'fifo', 1)[0] == 0
    assert choose(rows, 'bounded', 0)[0] == 0
    assert choose(rows, 'bounded', 1)[0] == 1
    rows[0]['bypass_count'] = 1
    assert choose(rows, 'bounded', 1)[0] == 0
    assert choose(rows, 'priority', 1)[0] == 1
    rows[0]['priority'] = 5
    assert choose(rows, 'priority', 1)[0] == 0


def test_drr_accounts_variable_cost_and_survives_serialization():
    state = None
    counts = [0, 0]
    for _ in range(110):
        index, state = choose(tickets(), 'drr', 1, state, {'a': 10, 'b': 1}, 1)
        counts[index] += 1
        state = json.loads(json.dumps(state))
    assert counts == [10, 100]
    # Disappearance discards that site's residual credit.
    index, state = choose(tickets()[1:], 'drr', 1, state, {'b': 1}, 1)
    assert index == 0 and state['queue'] == ['B'] and 'A' not in state['deficits']


@pytest.mark.parametrize('key,value', [('REPLAY_INHERITANCE', 'false'), ('REPLAY_SELECTION_POLICY', 'random'),
                                      ('CONTROLLER_SCHEDULING_POLICY', 'random'), ('CONTROLLER_BYPASS_LIMIT', '-1'),
                                      ('CONTROLLER_DRR_EVENT_COSTS', '{"a":0}'), ('CONTROLLER_DRR_EVENT_COSTS', '{"a":true}'),
                                      ('CONTROLLER_DRR_QUANTUM', '0')])
def test_invalid_settings_fail_closed(monkeypatch, key, value):
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES', '1')
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        replay_settings() if key.startswith('REPLAY') else controller_settings()


def test_defaults_and_opt_in(monkeypatch):
    assert replay_settings() == (True, 'priority')
    assert controller_settings()[:2] == ('bounded', 1)
    monkeypatch.setenv('REPLAY_INHERITANCE', '0')
    with pytest.raises(ValueError):
        replay_settings()
    monkeypatch.setenv('CONTROLLER_SCHEDULING_POLICY', 'drr')
    with pytest.raises(ValueError):
        controller_settings()


@pytest.mark.parametrize('inheritance,policy,expected', [('1', 'priority', ['A', 'B', 'C']),
                                                       ('0', 'priority', ['C', 'A', 'B']),
                                                       ('0', 'fifo', ['A', 'B', 'C'])])
def test_real_replay_policy_respects_dependencies(tmp_path, monkeypatch, inheritance, policy, expected):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'hospital.db')
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES', '1')
    monkeypatch.setenv('REPLAY_INHERITANCE', inheritance)
    monkeypatch.setenv('REPLAY_SELECTION_POLICY', policy)
    monkeypatch.setenv('METAGUARD_ENABLED', '1')
    db.init_db()
    for eid, priority, deps in [('A', 1, []), ('B', 5, ['A']), ('C', 3, [])]:
        db.save_event(dict(event_id=eid, site_id='Hospital_A', event_type='patient_registration',
                           base_priority=priority, prerequisite_ids=deps, payload={}))
    monkeypatch.setattr(grant_client, 'pinned_event', lambda: None)
    monkeypatch.setattr(grant_client, 'advertise_candidate', Mock())
    sent = []
    def transmit(event, send):
        assert all(next(e for e in db.get_events() if e['event_id'] == parent)['status'] == 'applied'
                   for parent in event['prerequisite_ids'])
        sent.append(event['event_id'])
        return {'event_id': event['event_id'], 'status': 'applied'}
    monkeypatch.setattr(grant_client, 'transmit', transmit)
    assert all(r['status'] == 'applied' for r in scheduled_replay.replay_scheduled_events())
    assert sent == expected


def test_fifo_keeps_priority_in_advertised_candidate():
    now = datetime.now(timezone.utc)
    events = [dict(event_id='a', base_priority=1, prerequisite_ids=[], created_at=now, status='pending'),
              dict(event_id='b', base_priority=5, prerequisite_ids=['a'], created_at=now, status='pending')]
    selected = select_next_event(events, set(), now=now, selection_policy='fifo', include_priority=True)
    assert selected['event_id'] == 'a' and selected['effective_priority'] == 5


@pytest.mark.parametrize('inheritance,policy', [('0', 'priority'), ('0', 'fifo')])
def test_experimental_modes_cannot_bypass_uncertain_attempt(tmp_path, monkeypatch, inheritance, policy):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'hospital.db')
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES', '1')
    monkeypatch.setenv('REPLAY_INHERITANCE', inheritance)
    monkeypatch.setenv('REPLAY_SELECTION_POLICY', policy)
    monkeypatch.setenv('METAGUARD_ENABLED', '1')
    db.init_db()
    for eid in ('uncertain', 'independent'):
        db.save_event(dict(event_id=eid, site_id='Hospital_A', event_type='patient_registration',
                           base_priority=5, prerequisite_ids=[], payload={}))
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE recovery_state SET retry_after=? WHERE event_id='uncertain'",
                     ((datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),))
    monkeypatch.setattr(grant_client, 'pinned_event', lambda: 'uncertain')
    from edge.scheduler.live_preview import inspect_hospital_queue
    preview = inspect_hospital_queue()
    assert preview['next_event_id'] is None
    assert preview['replay_policy']['selection_policy'] == policy
    transmitter, candidate = Mock(), Mock()
    monkeypatch.setattr(grant_client, 'transmit', transmitter)
    monkeypatch.setattr(grant_client, 'advertise_candidate', candidate)
    assert scheduled_replay.replay_scheduled_events() == []
    transmitter.assert_not_called()
    candidate.assert_not_called()
