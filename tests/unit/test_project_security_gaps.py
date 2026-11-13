"""High-risk unentered grant-fence decisions identified by the project audit.

These tests check decision/locking contracts with a cursor double. They do not
substitute for PostgreSQL transaction/concurrency evidence.
"""
from unittest.mock import Mock
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from cloud import metaguard


@pytest.fixture
def event():
    return {'event_id': 'AUDIT-E1', 'site_id': 'Hospital_A',
            'event_type': 'patient_registration', 'base_priority': 3,
            'prerequisite_ids': [], 'payload': {'patient_id': 'synthetic'}}


@pytest.mark.parametrize('violation', ['absent', 'hospital', 'event', 'issued', 'completed',
                                       'reconciliation_required', 'digest', 'expired'])
def test_sql_fence_rejects_each_invalid_binding_before_any_write(event, violation):
    row = ['Hospital_A', 'AUDIT-E1', 'acknowledged', metaguard.event_digest(event), True]
    if violation == 'hospital':
        row[0] = 'Hospital_B'
    elif violation == 'event':
        row[1] = 'OTHER'
    elif violation in ('issued', 'completed', 'reconciliation_required'):
        row[2] = violation
    elif violation == 'digest':
        row[3] = '0' * 64
    elif violation == 'expired':
        row[4] = False
    cursor = Mock()
    cursor.fetchone.return_value = None if violation == 'absent' else row
    with pytest.raises(HTTPException) as error:
        metaguard.fence_cloud_write(cursor, event, str(uuid4()))
    assert error.value.status_code == 409
    queries = [call.args[0] for call in cursor.execute.call_args_list]
    assert len(queries) == 2
    assert 'pg_advisory_xact_lock' in queries[0]
    assert 'FOR UPDATE' in queries[1] and 'clock_timestamp()' in queries[1]
    assert all(query.lstrip().startswith('SELECT') for query in queries)


def test_sql_fence_accepts_only_current_acknowledged_immutable_binding(event):
    cursor = Mock()
    cursor.fetchone.return_value = ('Hospital_A', 'AUDIT-E1', 'acknowledged', metaguard.event_digest(event), True)
    grant = str(uuid4())
    metaguard.fence_cloud_write(cursor, event, grant)
    assert cursor.execute.call_args.args[1] == (grant,)


@pytest.mark.parametrize('service', ['patient', 'emergency', 'admin'])
@pytest.mark.parametrize('config', ['null', '[]', '{"Hospital_A":42}',
                                   '{"Hospital_A":"' + 'a' * 40 + '"}'])
def test_cloud_bad_or_operator_reused_hospital_config_never_reaches_storage(monkeypatch, service, config):
    import importlib
    module = importlib.import_module(f'cloud.{service}_service.main')
    monkeypatch.setenv('METAGUARD_REQUIRED', '1')
    monkeypatch.setenv('CONTROLLER_HOSPITAL_TOKENS', config)
    monkeypatch.setenv('CONTROLLER_CLOUD_TOKEN', 'a' * 40)
    save = Mock()
    monkeypatch.setattr(module, 'save_cloud_event', save)
    kind = {'patient': 'patient_registration', 'emergency': 'emergency_record', 'admin': 'patient_discharge'}[service]
    # No lifespan: initializing a real DB is outside this test's scope.
    client = TestClient(module.app)
    response = client.post('/events', json={'event_id': 'AUDIT-E2', 'site_id': 'Hospital_A',
                           'event_type': kind, 'base_priority': 2, 'payload': {}},
                           headers={'Authorization': 'Bearer ' + 'a' * 40, 'X-MetaGuard-Grant': str(uuid4())})
    assert response.status_code == 503
    save.assert_not_called()
    assert 'a' * 40 not in response.text
