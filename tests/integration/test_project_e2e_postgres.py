"""Exercise the E2E SQL client against the opt-in isolated PostgreSQL database.

Only the Docker transport is replaced; the real client program, identity proof,
read-only mode, parameter binding and transaction handling all execute unchanged.
"""
import json
import os
import subprocess
import sys

import pytest

from infrastructure.scripts import project_e2e as e2e
from tests.integration.test_controller_postgres import (
    store, integrated_store, integrated_event, acknowledged_attempt,
)
from tests.unit.test_project_e2e import provisioned_stack, stack


@pytest.fixture
def sql_client(store, monkeypatch, tmp_path):
    _, connect = store
    with connect() as conn:
        identity = conn.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0]
        schema = conn.execute('SELECT current_schema()').fetchone()[0]
    stack = e2e.Stack(tmp_path, {'project': 'cg-e2e-' + 'a' * 24, 'nonce': 'a' * 32,
        'settings': {}, 'owned': {'containers': {'dashboard': 'isolated-test-client'}},
        'postgres_system_identifier': identity},
        {'checks': [], 'http': [], 'phase': 'test', 'revision': 'test'})
    monkeypatch.setattr(stack, 'fault_gate', lambda: stack.state['owned'])
    run = subprocess.run
    calls = []

    def local_transport(command, **kwargs):
        assert command[:6] == ['docker', 'exec', '-i', 'isolated-test-client', 'python', '-c']
        calls.append(json.loads(kwargs['input']))
        environment = os.environ.copy()
        environment['DATABASE_URL'] = os.environ['CONTROLLER_TEST_DATABASE_URL']
        return run([sys.executable, '-c', command[-1]], input=kwargs['input'],
                   env=environment, capture_output=True, text=True, timeout=20)

    monkeypatch.setattr(e2e.subprocess, 'run', local_transport)
    return stack, schema, calls


def test_percent_literal_function_and_transactional_fault_recovery(sql_client):
    stack, schema, _ = sql_client
    table, function = schema + '.fixture_events', schema + '.e2e_transient'
    stack.query(f'CREATE TABLE {table} (event_id TEXT PRIMARY KEY)', fixture=True)
    stack.query(f"""CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.event_id LIKE '%transient-db-error' THEN RAISE EXCEPTION 'isolated transient write failure'
        USING ERRCODE='08006'; END IF; RETURN NEW; END $$""", fixture=True)
    stack.query(f'CREATE TRIGGER e2e_transient BEFORE INSERT ON {table} '
                f'FOR EACH ROW EXECUTE FUNCTION {function}()', fixture=True)
    stack.query(f'INSERT INTO {table} VALUES (%s)', ('good',), fixture=True)
    with pytest.raises(e2e.IsolationError, match='ConnectionFailure'):
        stack.query(f'INSERT INTO {table} VALUES (%s)', ('test-transient-db-error',), fixture=True)
    failure = stack.report['sql_failures'][-1]
    assert (failure['sqlstate'], failure['phase'], failure['transaction_status']) == ('08006', 'query', 'INERROR')
    assert stack.query(f'SELECT event_id FROM {table}') == [('good',)]
    stack.query(f'DROP TRIGGER e2e_transient ON {table}', fixture=True)
    stack.query(f'DROP FUNCTION {function}()', fixture=True)
    stack.query(f'INSERT INTO {table} VALUES (%s)', ('test-transient-db-error',), fixture=True)
    assert stack.query(f'SELECT count(*) FROM {table}') == [(2,)]


def test_sql_client_rejects_wrong_server_identity_before_mutation(sql_client):
    stack, schema, calls = sql_client
    identity = stack.state['postgres_system_identifier']
    stack.state['postgres_system_identifier'] = 'wrong-server'
    with pytest.raises(e2e.IsolationError, match='RuntimeError'):
        stack.query(f'CREATE TABLE {schema}.forbidden (id INTEGER)', fixture=True)
    assert len(calls) == 1
    assert stack.report['sql_failures'][-1]['phase'] == 'identity'
    stack.state['postgres_system_identifier'] = identity
    assert stack.query('SELECT to_regclass(%s)', (schema + '.forbidden',)) == [(None,)]


def test_sql_evidence_connection_remains_read_only(sql_client):
    stack, schema, _ = sql_client
    stack.query(f'CREATE TABLE {schema}.guarded (id INTEGER)', fixture=True)
    with pytest.raises(e2e.IsolationError, match='ReadOnlySqlTransaction'):
        stack.query(f'SELECT * FROM {schema}.guarded FOR UPDATE')
    assert stack.report['sql_failures'][-1]['sqlstate'] == '25006'


def test_e2e_dashboard_live_reads_use_real_records_and_authenticated_metrics(
    integrated_store, provisioned_stack, monkeypatch,
):
    """Real Dashboard/Cloud ASGI routes and PostgreSQL, no network or event writes by the generator."""
    import importlib
    import time
    from collections import deque
    import httpx
    from fastapi.testclient import TestClient
    from dashboard import main as dashboard, test_traffic as traffic

    storage, connect = integrated_store
    environment = provisioned_stack.config['services']['dashboard']['environment']
    for key in ('CLOUD_LIVE_TELEMETRY_TOKEN', 'CLOUD_REPLAY_TELEMETRY_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, str(value))
    for key in ('AWS_REGION', 'AWS_DEFAULT_REGION', 'AWS_EXECUTION_ENV', 'EC2_INSTANCE_ID',
                'APP_ENV', 'ENVIRONMENT', 'DEPLOYMENT_ENV'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('CLOUD_LIVE_SLO_MS', '250')
    monkeypatch.setattr(traffic, 'get_connection', connect)
    services = {name: importlib.import_module(f'cloud.{name}_service.main')
                for name in ('patient', 'emergency', 'admin')}
    for module in services.values():
        monkeypatch.setattr(module.app.state.telemetry, 'classified_requests', deque())
        monkeypatch.setattr(module.app.state.telemetry, 'live_slo_threshold_ms', 250)
    assert not dashboard.app.state.test_traffic.lock.locked()
    monkeypatch.setattr(dashboard.app.state.test_traffic, 'job', None)
    for name in services:
        monkeypatch.setitem(dashboard.SERVICES, name, f'http://{name}:8000')
    for name, kind in (('patient', 'patient_registration'), ('emergency', 'emergency_record'),
                       ('admin', 'patient_discharge')):
        event = integrated_event(kind=kind)
        gid = acknowledged_attempt(storage, event)
        with TestClient(services[name].app) as cloud:
            response = cloud.post('/events', json=event, headers={
                'Authorization': 'Bearer ' + 'a' * 40, 'X-MetaGuard-Grant': gid})
            assert response.status_code == 201
        assert storage.reconcile_authoritative(gid, event['site_id'], complete_only=True) == 'completed'
    with connect() as conn:
        before = conn.execute('SELECT * FROM events ORDER BY event_id').fetchall()
    baseline = {name: module.app.state.telemetry.snapshot()['live_latency_sample_count']
                for name, module in services.items()}
    async_client = httpx.AsyncClient

    def cloud_routes(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        assert kwargs['limits'].max_connections == 3
        return async_client(mounts={dashboard.SERVICES[name]: httpx.ASGITransport(app=module.app)
                                    for name, module in services.items()}, **kwargs)

    monkeypatch.setattr(traffic.httpx, 'AsyncClient', cloud_routes)
    action = {'X-CrisisGuard-Local-Action': 'test-traffic'}
    with TestClient(dashboard.app, base_url='http://localhost', client=('127.0.0.1', 1234)) as client:
        assert client.post('/api/test-traffic', json={'count': 10}).status_code == 403
        assert client.post('/api/test-traffic', json={'count': 1}, headers=action).status_code == 422
        # Reproduce the failed configuration while preserving the endpoint's veto.
        monkeypatch.delenv('CLOUD_REPLAY_TELEMETRY_TOKEN')
        denied = client.post('/api/test-traffic', json={'count': 10}, headers=action)
        assert denied.status_code == 503
        assert denied.json() == {'detail': 'Distinct live telemetry credentials are unavailable'}
        assert client.get('/api/test-traffic').json()['run'] is None
        monkeypatch.setenv('CLOUD_REPLAY_TELEMETRY_TOKEN', environment['CLOUD_REPLAY_TELEMETRY_TOKEN'])
        assert client.post('/api/test-traffic', json={'count': 10}, headers=action).status_code == 202
        assert client.post('/api/test-traffic', json={'count': 10}, headers=action).status_code == 409
        deadline = time.monotonic() + 10
        while (result := client.get('/api/test-traffic').json())['active']:
            assert time.monotonic() < deadline, 'Bounded live read job did not finish'
            time.sleep(.02)
        assert result['run']['status'] == 'completed'
        assert all(row['succeeded'] == 10 and row['failed'] == 0
                   for row in result['run']['services'].values())
        assert all(secret not in str(result) for secret in
                   (environment['CLOUD_LIVE_TELEMETRY_TOKEN'], environment['CLOUD_REPLAY_TELEMETRY_TOKEN']))
    with connect() as conn:
        assert conn.execute('SELECT * FROM events ORDER BY event_id').fetchall() == before
    for name, module in services.items():
        with TestClient(module.app) as cloud:
            sample = cloud.get('/metrics').json()
        assert sample['database_healthy'] is True
        assert sample['live_latency_sample_count'] == baseline[name] + 10
        assert isinstance(sample['live_p95_latency_ms'], (float, int))
        assert sample['live_slo_violation_rate'] == pytest.approx(
            sample['live_slo_violation_count'] / sample['live_latency_sample_count'], abs=.000051)
