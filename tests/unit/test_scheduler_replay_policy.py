"""Local candidate selection, Inspector parity and executed trace regressions."""
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from edge.database import db
from edge.capture_api import grant_client, scheduled_replay
from edge.scheduler.live_preview import inspect_hospital_queue


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'hospital.db')
    monkeypatch.setenv('METAGUARD_ENABLED', '1')
    monkeypatch.setenv('SITE_ID', 'Hospital_A')
    db.init_db()
    for event_id, priority, deps in [('A', 1, []), ('B', 5, ['A']), ('C', 3, [])]:
        db.save_event(dict(event_id=event_id, site_id='Hospital_A',
                           event_type='patient_registration', base_priority=priority,
                           prerequisite_ids=deps, payload={}))
    monkeypatch.setattr(grant_client, 'advertise_candidate', Mock(return_value={'status': 'updated'}))
    return db.get_events()


def delay_a():
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE recovery_state SET retry_after=? WHERE event_id='A'", (deadline,))


@pytest.mark.parametrize('phase', ['ack_pending', 'sent', 'unknown_future_phase'])
def test_uncertain_attempt_pins_preview_and_replay(queue, monkeypatch, phase):
    grant_client._journal('A', 'grant-a', phase)
    delay_a()
    before = db.get_events()
    preview = inspect_hospital_queue()
    assert preview['scheduler_next_event_id'] == 'C'
    assert preview['next_event_id'] is None
    assert preview['outstanding_event_id'] == 'A'
    assert 'A' in preview['replay_wait_reason']
    c = next(e for e in preview['events'] if e['event_id'] == 'C')
    assert c['dependency_eligible'] and not c['eligible']
    assert 'MetaGuard' in c['reason']
    transmit = Mock()
    monkeypatch.setattr(grant_client, 'transmit', transmit)
    assert scheduled_replay.replay_scheduled_events() == []
    transmit.assert_not_called()
    grant_client.advertise_candidate.assert_not_called()
    assert db.get_events() == before


def test_registered_retry_does_not_block_independent_work(queue, monkeypatch, caplog):
    grant_client._journal('A', None, 'registered')
    delay_a()
    assert inspect_hospital_queue()['next_event_id'] == 'C'
    assert grant_client.pinned_event() is None
    sent = []

    def transmit(event, send):
        sent.append(event['event_id'])
        return {'status': 'applied', 'event_id': event['event_id']}

    monkeypatch.setattr(grant_client, 'transmit', transmit)
    with caplog.at_level(logging.INFO, logger=scheduled_replay.logger.name):
        results = scheduled_replay.replay_scheduled_events()
    assert sent == ['C']  # B still requires A's central acknowledgement.
    assert results[0]['status'] == 'applied'
    trace = [json.loads(r.message.split(' ', 1)[1]) for r in caplog.records
             if r.message.startswith('scheduler.selection ')]
    assert [r['event_id'] for r in trace] == sent
    assert trace[0]['effective_priority'] == 3
    assert any('scheduler.outcome ' in r.message and 'applied' in r.message for r in caplog.records)


def test_selectable_pin_wins_over_higher_priority_independent_event(queue):
    grant_client._journal('C', 'grant-c', 'sent')
    preview = inspect_hospital_queue()
    assert preview['scheduler_next_event_id'] == 'A'
    assert preview['next_event_id'] == 'C'
    assert next(e for e in preview['events'] if e['event_id'] == 'C')['selected']


def test_legacy_mode_ignores_grant_journal(queue, monkeypatch):
    grant_client._journal('C', 'grant-c', 'sent')
    monkeypatch.delenv('METAGUARD_ENABLED')
    preview = inspect_hospital_queue()
    assert preview['next_event_id'] == 'A'
    assert preview['outstanding_event_id'] is None


def test_no_eligible_work_withdraws_unsent_candidate(queue):
    grant_client._journal('A', None, 'registered')
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute('UPDATE recovery_state SET retry_after=?',
                     ((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),))
    assert scheduled_replay.replay_scheduled_events() == []
    grant_client.advertise_candidate.assert_called_once_with()


def test_missing_authoritative_grant_never_resends_uncertain_attempt(queue, monkeypatch):
    grant_client._journal('A', 'grant-a', 'sent')
    monkeypatch.setattr(grant_client, '_request', Mock(return_value={'grant': None}))
    sender = Mock()
    assert grant_client.transmit(queue[0], sender)['status'] == 'awaiting_reconciliation'
    sender.assert_not_called()
    grant_client.advertise_candidate.assert_not_called()
