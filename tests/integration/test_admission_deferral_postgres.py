"""Experimental hints/replay on an isolated PostgreSQL schema, never load tests."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from fastapi.testclient import TestClient

from cloud.controller import main
from edge.capture_api import grant_client, scheduled_replay, admission_retry
from edge.database import db as edge_db
from tests.integration.test_controller_postgres import store, admission_store, integrated_store


def test_feedback_is_transactional_and_cannot_bypass_fairness_or_staleness(admission_store):
    storage, connect = admission_store
    storage.add_ticket('A', 'FIRST', 5)
    storage.add_ticket('B', 'WAITING', 1)
    assert storage.issue_grant()['hospital_id'] == 'A'
    before = storage.list_tickets()
    feedback = {}
    assert storage.issue_grant(admission_feedback=feedback) is None
    assert feedback == {'reason': 'rate_limit', 'retry_after_seconds': 1}
    assert storage.list_tickets() == before
    with connect() as conn:
        conn.execute("UPDATE controller_admission SET observed_at=clock_timestamp()-INTERVAL '1 minute'")
    feedback = {}
    assert storage.issue_grant(admission_feedback=feedback) is None
    assert feedback == {} and storage.list_tickets() == before
    with connect() as conn:
        conn.execute("""UPDATE controller_admission SET state='Normal', observed_at=clock_timestamp(),
            last_issue_at=clock_timestamp()-INTERVAL '1 minute', next_issue_at=clock_timestamp()-INTERVAL '1 minute'""")
    assert storage.issue_grant(admission_feedback={})['hospital_id'] == 'B'


def test_authenticated_ack_hints_exclude_stale_telemetry(admission_store, monkeypatch):
    storage, connect = admission_store
    monkeypatch.setattr(main, 'storage', storage)
    monkeypatch.setenv('CONTROLLER_HOSPITAL_TOKENS', json.dumps({'Hospital_A':'a'*40}))
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES','1')
    monkeypatch.setenv('CONTROLLER_ADMISSION_ENABLED','0')
    storage.add_ticket('Hospital_A','ACK',1)
    gid=storage.issue_grant()['grant_id']
    with TestClient(main.app, headers={'Authorization':'Bearer '+'a'*40,
                                      'X-CrisisGuard-Retry-Policy':'bounded-deferral'}) as client:
        monkeypatch.setenv('CONTROLLER_ADMISSION_ENABLED','1')
        with connect() as conn:
            conn.execute("""UPDATE controller_admission SET last_ack_at=clock_timestamp(),
                next_ack_at=clock_timestamp()+INTERVAL '2 seconds'""")
        response=client.post('/grants/'+gid+'/acknowledge',json={'hospital_id':'Hospital_A'})
        assert response.status_code==429
        assert response.headers['X-CrisisGuard-Admission-Reason']=='rate_limit'
        assert 1 <= int(response.headers['Retry-After']) <= 2
        assert storage.get_grant_status(gid)['status']=='issued'
        with connect() as conn:
            conn.execute("UPDATE controller_admission SET observed_at=clock_timestamp()-INTERVAL '1 minute'")
        response=client.post('/grants/'+gid+'/acknowledge',json={'hospital_id':'Hospital_A'})
        assert response.status_code==429 and 'X-CrisisGuard-Admission-Reason' not in response.headers
        assert client.post('/grants/'+gid+'/acknowledge',json={'hospital_id':'Hospital_A'},
                           headers={'Authorization':'Bearer wrong'}).status_code==401


def test_real_deferral_restart_then_cloud_apply_once_with_dependency_order(integrated_store, tmp_path, monkeypatch):
    from cloud.patient_service.main import app as patient_app
    storage, connect = integrated_store
    monkeypatch.setattr(main, 'storage', storage)
    monkeypatch.setattr(edge_db,'DB_PATH',tmp_path/'edge.db')
    monkeypatch.setenv('SITE_ID','Hospital_A')
    monkeypatch.setenv('METAGUARD_ENABLED','1')
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES','1')
    monkeypatch.setenv('METAGUARD_RETRY_POLICY','bounded-deferral')
    monkeypatch.setenv('CONTROLLER_ADMISSION_ENABLED','0')
    edge_db.init_db()
    for eid,deps in [('PARENT',[]),('CHILD',['PARENT'])]:
        edge_db.save_event(dict(event_id=eid,site_id='Hospital_A',event_type='patient_registration',
                               base_priority=5 if deps else 1,prerequisite_ids=deps,payload={}))
    sent=[]
    with TestClient(main.app,headers={'Authorization':'Bearer '+'a'*40,
                                    'X-CrisisGuard-Retry-Policy':'bounded-deferral'}) as controller, TestClient(patient_app) as cloud:
        monkeypatch.setenv('CONTROLLER_ADMISSION_ENABLED','1')
        with connect() as conn:
            conn.execute("""UPDATE controller_admission SET state='Normal', observed_at=clock_timestamp(),
                last_issue_at=clock_timestamp(),next_issue_at=clock_timestamp()+INTERVAL '1 second'""")
        def request(method,path,body=None):
            response=controller.request(method,path,json=body)
            response.raise_for_status()
            return response.json()
        def send(event,*,grant_id):
            response=cloud.post('/events',json=event,headers={'Authorization':'Bearer '+'a'*40,'X-MetaGuard-Grant':grant_id})
            response.raise_for_status()
            sent.append(event['event_id'])
            return response.json()
        monkeypatch.setattr(grant_client,'_request',request)
        monkeypatch.setattr(scheduled_replay,'send_to_cloud',send)
        assert scheduled_replay.replay_scheduled_events()[0]['status']=='admission_deferred'
        edge_db.init_db()
        assert admission_retry.waiting() and scheduled_replay.replay_scheduled_events()==[] and sent==[]
        for _ in range(4):
            with sqlite3.connect(edge_db.DB_PATH) as conn:
                conn.execute('UPDATE recovery_state SET retry_after=NULL')
                conn.execute("UPDATE metaguard_deferrals SET retry_after='2000-01-01T00:00:00+00:00'")
            with connect() as conn:
                conn.execute("""UPDATE controller_admission SET observed_at=clock_timestamp(),
                    last_issue_at=clock_timestamp()-INTERVAL '1 minute',next_issue_at=NULL,
                    last_ack_at=clock_timestamp()-INTERVAL '1 minute',next_ack_at=NULL""")
            scheduled_replay.replay_scheduled_events()
            if len(sent)==2: break
    assert sent==['PARENT','CHILD']
    assert all(e['status']=='applied' for e in edge_db.get_events())
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM events').fetchone()[0]==2
        assert conn.execute("SELECT count(*) FROM controller_grants WHERE status='completed'").fetchone()[0]==2
    with sqlite3.connect(edge_db.DB_PATH) as conn:
        assert conn.execute('SELECT SUM(retry_count) FROM recovery_state').fetchone()[0]==0
