from datetime import datetime, timedelta, timezone
import sqlite3
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from cloud.metaguard import event_digest
from edge.capture_api import admission_retry, grant_client, scheduled_replay
from edge.database import db
from tests.unit.test_metaguard_replay import local_event, authenticated


PROOF = {'reason': 'rate_limit', 'retry_after_seconds': 1}


@pytest.fixture
def opt_in(monkeypatch):
    monkeypatch.setenv('METAGUARD_ENABLED', '1')
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES', '1')
    monkeypatch.setenv('METAGUARD_RETRY_POLICY', 'bounded-deferral')


def state():
    with sqlite3.connect(db.DB_PATH) as conn:
        return conn.execute("SELECT in_flight,retry_count,retry_after,claim_token FROM recovery_state WHERE event_id='E1'").fetchone()


def expire_gate():
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE recovery_state SET retry_after=NULL")
        conn.execute("UPDATE metaguard_deferrals SET retry_after='2000-01-01T00:00:00+00:00'")


@pytest.mark.parametrize('mode,experimental,metaguard', [
    ('invalid', '1', '1'), ('bounded-deferral', '0', '1'), ('bounded-deferral', '1', '0')])
def test_invalid_or_unapproved_mode_fails_before_replay(monkeypatch, mode, experimental, metaguard):
    monkeypatch.setenv('METAGUARD_RETRY_POLICY', mode)
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES', experimental)
    monkeypatch.setenv('METAGUARD_ENABLED', metaguard)
    with pytest.raises(ValueError):
        scheduled_replay.replay_scheduled_events()


@pytest.mark.parametrize('kind', ['issue', 'ack'])
def test_deferral_then_grant_sends_once_and_preserves_dependencies(local_event, opt_in, monkeypatch, kind):
    db.save_event({**local_event, 'event_id': 'CHILD', 'prerequisite_ids': ['E1'], 'base_priority': 5})
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE recovery_state SET retry_count=3 WHERE event_id='E1'")
    grants, deferred, sent = {}, [False], []
    current = ['E1']
    def request(method, path, body=None):
        if path.endswith('/candidate'):
            if body:
                current[0] = body['event_id']
            return {'status': 'updated'}
        if method == 'GET':
            from urllib.parse import parse_qs, urlsplit
            eid = parse_qs(urlsplit(path).query)['event_id'][0]
            return {'grant': grants.get(eid)}
        if path.endswith('/grants/next'):
            if kind == 'issue' and not deferred[0]:
                deferred[0] = True
                return {'waiting': True, 'admission_deferral': PROOF}
            event = next(e for e in db.get_events() if e['event_id'] == current[0])
            grants[current[0]] = dict(grant_id=str(uuid4()), status='issued', event_digest=event_digest(event))
            return grants[current[0]]
        if path.endswith('/acknowledge'):
            if kind == 'ack' and not deferred[0]:
                deferred[0] = True
                response = httpx.Response(429, json={'detail': 'Replay admission deferred'},
                    headers={'Retry-After': '1', 'X-CrisisGuard-Admission-Reason': 'rate_limit'},
                    request=httpx.Request('POST', 'http://controller'+path))
                response.raise_for_status()
            return {'status': 'acknowledged'}
        if path.endswith('/complete'):
            return {'status': 'completed'}
        raise AssertionError(path)
    def send(event, *, grant_id):
        assert not event['prerequisite_ids'] or next(e for e in db.get_events() if e['event_id']=='E1')['status']=='applied'
        sent.append(event['event_id'])
        return {'status': 'applied', 'event_id': event['event_id']}
    remote = Mock(side_effect=request)
    monkeypatch.setattr(grant_client, '_request', remote)
    monkeypatch.setattr(scheduled_replay, 'send_to_cloud', send)
    result = scheduled_replay.replay_scheduled_events()
    assert result[0]['status'] == 'admission_deferred' and not sent
    assert state()[0:2] == (0, 3)  # Genuine failure history is unchanged.
    calls = remote.call_count
    db.init_db()  # Crash/restart retains cooldown and registered journal.
    for _ in range(20):
        assert scheduled_replay.replay_scheduled_events() == []
    assert remote.call_count == calls
    expire_gate()
    scheduled_replay.replay_scheduled_events()
    assert sent == ['E1', 'CHILD']
    assert all(e['status']=='applied' for e in db.get_events())
    assert state()[1] == 3


@pytest.mark.parametrize('response', [
    {'status': 'waiting_for_grant'}, {'status': 'waiting_for_admission'},
    {'status': 'waiting_for_candidate'}, {'status': 'awaiting_reconciliation'},
    {'status': 'waiting_for_admission', 'admission_deferral': {'reason': 'telemetry_unavailable', 'retry_after_seconds': 1}},
    {'status': 'waiting_for_grant', 'admission_deferral': {'reason': 'rate_limit', 'retry_after_seconds': 0}},
])
def test_unproven_or_stale_deferrals_keep_failure_backoff(local_event, opt_in, monkeypatch, response):
    monkeypatch.setattr(grant_client, 'transmit', Mock(return_value=response))
    scheduled_replay.replay_scheduled_events()
    assert state()[1] == 1
    assert not admission_retry.waiting()


@pytest.mark.parametrize('code', [401, 403, 429, 503])
def test_http_failures_keep_exponential_backoff(local_event, opt_in, monkeypatch, code):
    response = httpx.Response(code, request=httpx.Request('POST','http://controller/grants'))
    error = httpx.HTTPStatusError('controller rejected', request=response.request, response=response)
    monkeypatch.setattr(grant_client, '_request', Mock(side_effect=error))
    send = Mock()
    monkeypatch.setattr(scheduled_replay, 'send_to_cloud', send)
    scheduled_replay.replay_scheduled_events()
    assert state()[1] == 1 and not admission_retry.waiting()
    send.assert_not_called()


@pytest.mark.parametrize('error', [httpx.ConnectError('offline'), httpx.ReadTimeout('lost response')])
def test_controller_outage_remains_failure(local_event, opt_in, monkeypatch, error):
    monkeypatch.setattr(grant_client, '_request', Mock(side_effect=error))
    scheduled_replay.replay_scheduled_events()
    assert state()[1] == 1 and not admission_retry.waiting()


@pytest.mark.parametrize('phase', ['ack_pending', 'sent', 'unknown'])
def test_deferral_cannot_release_ambiguous_journal(local_event, opt_in, phase):
    token = db.claim_event('E1')
    grant_client._journal('E1', str(uuid4()), phase)
    assert admission_retry.record('E1', token, PROOF) is None
    assert state()[0] == 1 and not admission_retry.waiting()


def test_obsolete_token_cannot_release_new_claim_or_shorten_deadline(local_event, opt_in):
    old = db.claim_event('E1')
    assert db.release_event('E1', old)
    current = db.claim_event('E1')
    grant_client._journal('E1', None, 'registered')
    assert admission_retry.record('E1', old, PROOF) is None
    assert state()[3] == current
    future = (datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat()
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE recovery_state SET retry_after=? WHERE event_id='E1'",(future,))
    assert admission_retry.record('E1', current, PROOF) == future
    assert state()[2] == future and state()[1] == 0


def test_default_still_backs_off_even_for_valid_proof(local_event, monkeypatch):
    monkeypatch.delenv('METAGUARD_RETRY_POLICY', raising=False)
    monkeypatch.setattr(grant_client, 'transmit', Mock(return_value={'status':'waiting_for_admission','admission_deferral':PROOF}))
    result = scheduled_replay.replay_scheduled_events()
    assert result[0]['status'] == 'pending' and 1 <= result[0]['retry_delay_seconds'] <= 2
    assert state()[1] == 1
    with sqlite3.connect(db.DB_PATH) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='metaguard_deferrals'").fetchone()


@pytest.mark.parametrize('reason,delay,detail', [
    ('telemetry_unavailable','1','Replay admission deferred'),
    ('rate_limit','0','Replay admission deferred'), ('rate_limit','31','Replay admission deferred'),
    ('rate_limit','NaN','Replay admission deferred'), ('rate_limit','1','Unknown throttle'),
])
def test_unproven_ack_response_preserves_exponential_failure_history(local_event, opt_in, monkeypatch, reason, delay, detail):
    gid=str(uuid4())
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute('UPDATE recovery_state SET retry_count=3')
    def request(method,path,body=None):
        if method=='GET':return {'grant':dict(grant_id=gid,status='issued',event_digest=event_digest(local_event))}
        if path.endswith('/candidate'):return {'status':'issued'}
        response=httpx.Response(429,json={'detail':detail},request=httpx.Request('POST','http://controller'+path),
            headers={'Retry-After':delay,'X-CrisisGuard-Admission-Reason':reason})
        response.raise_for_status()
    monkeypatch.setattr(grant_client,'_request',request)
    sender=Mock();monkeypatch.setattr(scheduled_replay,'send_to_cloud',sender)
    result=scheduled_replay.replay_scheduled_events()
    assert state()[1]==4 and 8<=result[0]['retry_delay_seconds']<=16
    assert not admission_retry.waiting()
    sender.assert_not_called()


def test_controller_request_keeps_authentication_and_adds_only_opt_in_header(opt_in, monkeypatch):
    monkeypatch.setenv('CONTROLLER_URL','http://controller')
    monkeypatch.setenv('HOSPITAL_CONTROLLER_TOKEN','private-test-placeholder')
    request=Mock(return_value=httpx.Response(200,json={'waiting':True},request=httpx.Request('POST','http://controller/next')))
    monkeypatch.setattr(grant_client.httpx,'request',request)
    grant_client._request('POST','/next')
    headers=request.call_args.kwargs['headers']
    assert headers['Authorization']=='Bearer private-test-placeholder'
    assert headers['X-CrisisGuard-Retry-Policy']=='bounded-deferral'
    monkeypatch.setenv('METAGUARD_RETRY_POLICY','failure-backoff')
    grant_client._request('POST','/next')
    assert 'X-CrisisGuard-Retry-Policy' not in request.call_args.kwargs['headers']


def test_concurrent_experimental_replay_does_not_poll_or_claim(local_event, opt_in, monkeypatch):
    remote=Mock(side_effect=AssertionError('Concurrent replay must not poll'))
    monkeypatch.setattr(grant_client,'_request',remote)
    assert admission_retry.replay_lock.acquire(blocking=False)
    try:
        assert scheduled_replay.replay_scheduled_events()==[]
        assert state()[0:2]==(0,0)
    finally:
        admission_retry.replay_lock.release()
    remote.assert_not_called()


@pytest.mark.parametrize('fresh,inflight,seconds,expected', [
    (True,0,.2,{'reason':'rate_limit','retry_after_seconds':1}),
    (True,4,.2,{'reason':'inflight_limit','retry_after_seconds':1}),
    (True,0,60,{'reason':'rate_limit','retry_after_seconds':30}),
    (False,0,.2,{}), (False,4,.2,{}),
])
def test_transactional_hint_is_bounded_and_never_emitted_for_stale_data(monkeypatch, fresh, inflight, seconds, expected):
    from cloud.controller import admission
    now=datetime.now(timezone.utc)
    config=admission.Settings()
    row=dict(state='Normal',observed_at=now if fresh else None,
             last_issue_at=now,next_issue_at=now+timedelta(seconds=seconds))
    monkeypatch.setattr(admission,'settings',lambda:config)
    monkeypatch.setattr(admission,'load',lambda cur,cfg:(row,now))
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY','fixed')
    cur=Mock();cur.fetchone.return_value={'count':inflight}
    feedback={}
    assert not admission.reserve(cur,'issue',feedback=feedback)
    assert feedback==expected


@pytest.mark.parametrize('loss', ['ack', 'cloud'])
def test_ack_loss_and_cloud_response_loss_never_resend(local_event, opt_in, monkeypatch, loss):
    gid = str(uuid4())
    remote_state = ['issued']
    def request(method, path, body=None):
        if path.endswith('/candidate'): return {'status':'issued'}
        if path == f'/grants/by-id/{gid}':
            return {'grant_id': gid, 'hospital_id': 'Hospital_A', 'event_id': 'E1',
                    'event_digest': event_digest(local_event), 'status': remote_state[0]}
        if method == 'GET': return {'grant': {'grant_id':gid,'status':remote_state[0],'event_digest':event_digest(local_event)}}
        if path.endswith('/acknowledge'):
            remote_state[0]='acknowledged'
            if loss == 'ack':
                raise httpx.ReadTimeout('ACK committed; response lost')
            return {'status':'acknowledged'}
        if path.endswith('/reconcile'): return {'status':'expired' if loss=='ack' else 'completed'}
        raise AssertionError(path)
    monkeypatch.setattr(grant_client,'_request',request)
    sender=Mock(side_effect=httpx.ReadTimeout('Cloud response lost after send'))
    monkeypatch.setattr(scheduled_replay,'send_to_cloud',sender)
    scheduled_replay.replay_scheduled_events()
    assert state()[1] == 1 and not admission_retry.waiting()
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as conn: conn.execute('UPDATE recovery_state SET retry_after=NULL')
    scheduled_replay.replay_scheduled_events()
    assert state()[1] == (2 if loss=='ack' else 1)
    assert sender.call_count == (0 if loss=='ack' else 1)


def test_hint_requires_opt_in_and_owner_authorization(authenticated, monkeypatch):
    client, storage = authenticated
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES','1')
    storage.issue_grant.return_value = None
    def issue(*, admission_feedback):
        admission_feedback.update(PROOF)
        return None
    storage.issue_grant.side_effect=issue
    response=client.post('/hospitals/Hospital_A/grants/next',headers={'X-CrisisGuard-Retry-Policy':'bounded-deferral'})
    assert response.json()['admission_deferral']==PROOF
    storage.issue_grant.reset_mock()
    assert client.post('/hospitals/Hospital_B/grants/next',headers={'X-CrisisGuard-Retry-Policy':'bounded-deferral'}).status_code==403
    storage.issue_grant.assert_not_called()


def test_legacy_controller_response_has_no_hint(authenticated, monkeypatch):
    client, storage=authenticated
    storage.issue_grant.return_value=None
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES','0')
    assert client.post('/hospitals/Hospital_A/grants/next',headers={'X-CrisisGuard-Retry-Policy':'bounded-deferral'}).json()=={'waiting':True}
    storage.issue_grant.assert_called_once_with()
