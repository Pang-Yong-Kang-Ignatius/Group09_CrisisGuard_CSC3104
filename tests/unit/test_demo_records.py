"""Local Monitoring demo capture must use the hospital and durable replay path."""
import httpx
import pytest
from fastapi.testclient import TestClient
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit

from dashboard import main, test_traffic
from cloud.metaguard import event_digest
from edge.capture_api import grant_client, scheduled_replay
from edge.database import db
from edge.capture_api.main import Event as HospitalEvent
from edge.capture_api.cloud_client import EVENT_ROUTES


@pytest.fixture
def demo(monkeypatch):
    for key in ('AWS_REGION', 'AWS_DEFAULT_REGION', 'AWS_EXECUTION_ENV', 'EC2_INSTANCE_ID',
                'APP_ENV', 'ENVIRONMENT', 'DEPLOYMENT_ENV'):
        monkeypatch.delenv(key, raising=False)
    for key, value in {'CRISISGUARD_DEPLOYMENT_MODE': 'local-development',
                       'DASHBOARD_TEST_TRAFFIC_ENABLED': '1', 'METAGUARD_REQUIRED': '1',
                       'DATABASE_URL': 'postgresql://postgres:5432/test',
                       'CLOUD_LIVE_TELEMETRY_TOKEN': 'local-live-token-' * 4,
                       'CLOUD_REPLAY_TELEMETRY_TOKEN': 'local-replay-token-' * 4}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(test_traffic.Path, 'read_text', lambda self: 'Local development host')
    for name in ('patient', 'emergency', 'admin'):
        monkeypatch.setitem(main.SERVICES, name, f'http://{name}:8000')
    monkeypatch.setitem(main.HOSPITALS, 'hospital-a', 'http://hospital-a:8000')

    local, cloud, posts = {}, {}, []

    def request(method, url, **kwargs):
        assert url.startswith('http://hospital-a:8000/')
        if method == 'GET' and url.endswith('/events'):
            return httpx.Response(200, json=list(local.values()), request=httpx.Request(method, url))
        assert method == 'POST' and url.endswith('/capture')
        event = kwargs['json']
        posts.append(event)
        assert event['event_id'] not in local
        local[event['event_id']] = {**event, 'status': 'pending'}
        return httpx.Response(201, json={'status': 'pending', 'event_id': event['event_id']},
                              request=httpx.Request(method, url))

    monkeypatch.setattr(test_traffic.httpx, 'request', request)
    monkeypatch.setattr(test_traffic, 'get_cloud_event', lambda event_id: cloud.get(event_id))
    with TestClient(main.app, base_url='http://localhost', client=('127.0.0.1', 1234)) as client:
        yield client, local, cloud, posts


ACTION = {'X-CrisisGuard-Local-Action': 'prepare-demo-records'}


def test_demo_chain_passes_hospital_validation_and_routes_to_all_cloud_apis():
    events = test_traffic.DEMO_EVENTS
    assert [EVENT_ROUTES[HospitalEvent.model_validate(event).event_type] for event in events] == [
        'patient', 'emergency', 'admin']
    assert [event['prerequisite_ids'] for event in events] == [[],
        [events[0]['event_id']], [events[1]['event_id']]]


def test_demo_chain_replay_requires_consecutive_metaguard_grants(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'hospital.db')
    monkeypatch.setenv('SITE_ID', 'Hospital_A')
    monkeypatch.setenv('METAGUARD_ENABLED', '1')
    monkeypatch.setenv('METAGUARD_RETRY_POLICY', 'failure-backoff')
    db.init_db()
    for event in test_traffic.DEMO_EVENTS:
        db.save_event(event)
    candidate = [None]
    grants = {}
    sends = []

    def controller(method, path, body=None):
        if path.endswith('/candidate'):
            candidate[0] = body['event_id'] if body else None
            return {'status': 'updated'}
        if method == 'GET':
            event_id = parse_qs(urlsplit(path).query)['event_id'][0]
            return {'grant': grants.get(event_id)}
        if path.endswith('/grants/next'):
            event = next(row for row in db.get_events() if row['event_id'] == candidate[0])
            grants[candidate[0]] = {'grant_id': str(uuid4()), 'status': 'issued',
                                     'event_digest': event_digest(event)}
            return grants[candidate[0]]
        if path.endswith('/acknowledge'):
            return {'status': 'acknowledged'}
        if path.endswith('/complete'):
            return {'status': 'completed'}
        raise AssertionError(path)

    def cloud_send(event, *, grant_id):
        assert grant_id == grants[event['event_id']]['grant_id']
        sends.append(EVENT_ROUTES[event['event_type']])
        return {'status': 'applied', 'event_id': event['event_id']}

    monkeypatch.setattr(grant_client, '_request', controller)
    monkeypatch.setattr(scheduled_replay, 'send_to_cloud', cloud_send)
    assert [result['status'] for result in scheduled_replay.replay_scheduled_events()] == [
        'applied', 'applied', 'applied']
    assert sends == ['patient', 'emergency', 'admin']
    assert len({grant['grant_id'] for grant in grants.values()}) == 3
    assert scheduled_replay.replay_scheduled_events() == []
    assert len(sends) == 3


def test_prepare_captures_valid_chain_and_waits_for_authoritative_application(demo):
    client, local, cloud, posts = demo
    assert client.get('/api/demo-records').json()['status'] == 'not_prepared'
    result = client.post('/api/demo-records', headers=ACTION)
    assert result.status_code == 200 and result.json()['status'] == 'pending'
    assert [event['event_type'] for event in posts] == [
        'patient_registration', 'emergency_record', 'patient_discharge']
    assert [event['prerequisite_ids'] for event in posts] == [[],
        [posts[0]['event_id']], [posts[1]['event_id']]]
    assert len({event['event_id'] for event in posts}) == 3
    assert all(event['site_id'] == 'Hospital_A' and
               event['payload']['patient_id'].startswith('SYNTHETIC_') for event in posts)
    assert client.post('/api/demo-records', headers=ACTION).json()['status'] == 'pending'
    assert len(posts) == 3
    for event in posts:
        local[event['event_id']]['status'] = 'applied'
        cloud[event['event_id']] = {'event_id': event['event_id'],
            'site_id': event['site_id'], 'event_type': event['event_type'], 'status': 'applied'}
    assert client.get('/api/demo-records').json() == {'status': 'ready',
        'services': {'patient': 'applied', 'emergency': 'applied', 'admin': 'applied'}}
    assert client.post('/api/demo-records', headers=ACTION).json()['status'] == 'ready'
    assert len(posts) == 3


def test_ambiguous_capture_is_not_duplicated_on_repeat(demo, monkeypatch):
    client, local, _, posts = demo
    original = test_traffic.httpx.request
    def lost_response(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if method == 'POST':
            raise httpx.ReadTimeout('Synthetic lost acknowledgement')
        return response
    monkeypatch.setattr(test_traffic.httpx, 'request', lost_response)
    assert client.post('/api/demo-records', headers=ACTION).status_code == 503
    assert len(posts) == 1 and len(local) == 1
    monkeypatch.setattr(test_traffic.httpx, 'request', original)
    assert client.post('/api/demo-records', headers=ACTION).json()['status'] == 'pending'
    assert len(posts) == 3 and len(local) == 3


def test_conflicting_local_id_prevents_any_capture(demo):
    client, local, _, posts = demo
    expected = test_traffic.DEMO_EVENTS[0]
    local[expected['event_id']] = {**expected, 'payload': {'patient_id': 'different'}, 'status': 'pending'}
    assert client.get('/api/demo-records').json()['status'] == 'conflict'
    assert client.post('/api/demo-records', headers=ACTION).status_code == 409
    assert not posts


@pytest.mark.parametrize('key,value', [
    ('CRISISGUARD_DEPLOYMENT_MODE', 'production'), ('APP_ENV', 'staging'),
    ('AWS_REGION', 'test-region'), ('DATABASE_URL', 'postgresql://remote.example/db'),
    ('METAGUARD_REQUIRED', '0'), ('DASHBOARD_HOSPITAL_A_URL', 'http://remote.example')])
def test_preparation_disabled_outside_protected_local_stack(demo, monkeypatch, key, value):
    client, _, _, posts = demo
    if key == 'DASHBOARD_HOSPITAL_A_URL':
        monkeypatch.setitem(main.HOSPITALS, 'hospital-a', value)
    else:
        monkeypatch.setenv(key, value)
    assert client.get('/api/demo-records').status_code == 404
    assert client.post('/api/demo-records', headers=ACTION).status_code == 404
    assert not posts


def test_preparation_requires_local_action_header(demo):
    client, _, _, posts = demo
    assert client.post('/api/demo-records').status_code == 403
    assert not posts
