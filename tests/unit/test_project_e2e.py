"""Safety/control-flow regressions for E2E; never invoke Docker or live HTTP."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from infrastructure.scripts import project_e2e as e2e
from infrastructure.scripts import project_verify
from tests.unit.test_project_docker_guard import valid_baseline


def safe_config():
    nonce = 'a' * 32
    project = 'cg-e2e-' + nonce[:24]
    config = {'name': project, 'volumes': {}, 'networks': {}, 'services': {}}
    for name in ('postgres-data', 'hospital-a-data', 'hospital-b-data'):
        config['volumes'][name] = {'name': project + '_' + name, 'labels': {e2e.LABEL: nonce}}
    for name in ('default', 'control'):
        config['networks'][name] = {'name': project + '_' + name, 'internal': True, 'labels': {e2e.LABEL: nonce}}
    for index, name in enumerate(e2e.SERVICES):
        config['services'][name] = {'labels': {e2e.LABEL: nonce}, 'networks': {'default': {}},
            'environment': {}, 'image': 'owned:test', 'ports': [{'published': str(40000 + index), 'target': 8000, 'host_ip': '127.0.0.1'}]}
    for name in ('patient', 'emergency', 'admin'):
        config['services'][name]['environment']['METAGUARD_REQUIRED'] = '1'
    for name in ('hospital-a', 'hospital-b'):
        config['services'][name]['environment']['METAGUARD_ENABLED'] = '1'
        config['services'][name]['volumes'] = [{'type': 'volume', 'source': name + '-data', 'target': '/data'}]
    return config, project, nonce


def test_config_proof_is_read_only_and_accepts_only_disposable_topology():
    config, project, nonce = safe_config()
    original = deepcopy(config)
    e2e.audit_config(config, project, nonce)
    assert config == original


@pytest.mark.parametrize('fault', ['foreign_volume', 'external_network', 'egress', 'bind', 'privileged',
    'docker_socket', 'extra_hosts', 'foreign_url', 'aws', 'host_port', 'same_sqlite', 'reuse_volume',
    'unprotected_cloud', 'unprotected_hospital', 'missing_label', 'build', 'container_name'])
def test_rejects_unsafe_plan_before_any_docker_mutation(fault):
    config, project, nonce = safe_config()
    baseline = []
    service = config['services']['hospital-a']
    if fault == 'foreign_volume':
        config['volumes']['postgres-data']['name'] = 'crisisguard-local_postgres-data'
    elif fault == 'external_network':
        config['networks']['default']['external'] = True
    elif fault == 'egress':
        config['networks']['default']['internal'] = False
    elif fault == 'bind':
        service['volumes'][0]['type'] = 'bind'
    elif fault == 'privileged':
        service['privileged'] = True
    elif fault == 'docker_socket':
        service['volumes'].append({'type': 'bind', 'source': '/var/run/docker.sock', 'target': '/var/run/docker.sock'})
    elif fault == 'extra_hosts':
        service['extra_hosts'] = ['host.docker.internal:host-gateway']
    elif fault == 'foreign_url':
        service['environment']['CLOUD_URL'] = 'http://production.example/events'
    elif fault == 'aws':
        service['environment']['AWS_REGION'] = 'ap-southeast-1'
    elif fault == 'host_port':
        service['ports'][0]['host_ip'] = '0.0.0.0'
    elif fault == 'same_sqlite':
        config['services']['hospital-b']['volumes'][0]['source'] = 'hospital-a-data'
    elif fault == 'reuse_volume':
        baseline = [config['volumes']['postgres-data']['name']]
    elif fault == 'unprotected_cloud':
        config['services']['patient']['environment']['METAGUARD_REQUIRED'] = '0'
    elif fault == 'unprotected_hospital':
        service['environment']['METAGUARD_ENABLED'] = '0'
    elif fault == 'missing_label':
        service['labels'] = {}
    elif fault == 'build':
        service['build'] = {'context': '.'}
    else:
        service['container_name'] = 'crisisguard-local-hospital-a-1'
    with pytest.raises(e2e.IsolationError):
        e2e.audit_config(config, project, nonce, baseline)


@pytest.fixture
def stack(tmp_path):
    state = {'project': 'cg-e2e-' + 'a' * 24, 'nonce': 'a' * 32, 'settings': {'MANUAL_REPLAY_TOKEN': 'generated-secret'}, 'ports': {'patient': 40000}}
    return e2e.Stack(tmp_path, state, {'checks': [], 'http': [], 'phase': 'test', 'revision': 'test'})


@pytest.fixture
def provisioned_stack(stack, monkeypatch):
    """Run real provisioning configuration assembly without Docker operations."""
    config, _, _ = safe_config()
    config['services'].pop('fault-proxy')
    stack.settings.update({key: str(40000 + i) for i, key in enumerate(e2e.PORT_KEYS)})
    stack.settings.update(DASHBOARD_REPLAY_OPERATOR_TOKEN='operator-' * 8,
                          CLOUD_LIVE_TELEMETRY_TOKEN='live-' * 16,
                          CLOUD_REPLAY_TELEMETRY_TOKEN='replay-' * 16)
    stack.state.update(image='owned:test', baseline=valid_baseline())
    for name in ('patient', 'emergency', 'admin', 'dashboard'):
        config['services'][name]['environment'].update(
            DATABASE_URL='postgresql://postgres:5432/isolated',
            CLOUD_LIVE_TELEMETRY_TOKEN=stack.settings['CLOUD_LIVE_TELEMETRY_TOKEN'])
    for name in ('patient', 'emergency', 'admin', 'hospital-a', 'hospital-b'):
        config['services'][name]['environment']['CLOUD_REPLAY_TELEMETRY_TOKEN'] = stack.settings['CLOUD_REPLAY_TELEMETRY_TOKEN']
    config['services']['dashboard']['environment']['METAGUARD_REQUIRED'] = '1'
    monkeypatch.setattr(stack, 'capture_baseline', lambda: stack.state['baseline'])
    monkeypatch.setattr(stack, 'read_docker', lambda *args: 'owned-image-id' if args[1:2] == ('inspect',) else '')

    def docker(*args, **kwargs):
        if args[0] == 'compose' and args[-3:] == ('config', '--format', 'json'):
            return json.dumps(config)
        assert args[0] == 'build'
        return ''

    monkeypatch.setattr(stack, 'docker', docker)
    monkeypatch.setattr(stack, 'compose', lambda *args, **kwargs: None)
    monkeypatch.setattr(e2e, 'establish_database', lambda owned: owned)
    return e2e.provision(stack)


def test_provision_wires_distinct_dashboard_telemetry_credentials(provisioned_stack):
    stack = provisioned_stack
    services = stack.config['services']
    environment = services['dashboard']['environment']
    live, replay = [environment.get(key, '') for key in
                    ('CLOUD_LIVE_TELEMETRY_TOKEN', 'CLOUD_REPLAY_TELEMETRY_TOKEN')]
    assert len(live) >= 32 and len(replay) >= 32 and live != replay
    assert all(services[name]['environment']['CLOUD_REPLAY_TELEMETRY_TOKEN'] == replay
               for name in ('patient', 'emergency', 'admin', 'hospital-a', 'hospital-b'))
    assert all(services[name]['environment']['CLOUD_LIVE_TELEMETRY_TOKEN'] == live
               for name in ('patient', 'emergency', 'admin'))
    assert live not in json.dumps(stack.report) and replay not in json.dumps(stack.report)


def test_faults_and_cleanup_refused_without_confirmation_before_proof_or_command(monkeypatch, stack):
    monkeypatch.setattr(stack, 'prove_ownership', lambda **kwargs: pytest.fail('Must gate before Docker inspection'))
    monkeypatch.setattr(stack, 'docker', lambda *a, **k: pytest.fail('Must not mutate Docker'))
    with pytest.raises(e2e.IsolationError, match='confirmation'):
        stack.cleanup()
    with pytest.raises(e2e.IsolationError, match='confirmation'):
        with stack.disconnected(['hospital-a']):
            pass
    with pytest.raises(e2e.IsolationError, match='confirmation'):
        with stack.stopped(['controller']):
            pass


def test_confirmation_binds_to_runtime_project_and_run_uuid(monkeypatch, stack):
    monkeypatch.setattr(stack, 'require_preserved', lambda: None)
    monkeypatch.setattr(stack, 'prove_ownership', lambda: {'project': stack.project})
    monkeypatch.setattr(stack, 'save', lambda: None)
    with pytest.raises(e2e.IsolationError):
        stack.confirm('yes')
    assert not stack.confirmed
    stack.confirm(stack.project + ':' + stack.nonce)
    assert stack.confirmed
    assert stack.report['confirmation']['project'] == stack.project


def test_resume_refuses_paths_outside_local_results(tmp_path):
    with pytest.raises(e2e.IsolationError, match='results'):
        e2e.load_stack(tmp_path)


def test_http_rejects_remote_targets_and_never_records_payload_or_credentials(monkeypatch, stack):
    stack.state['owned'] = {'containers': {'dashboard': 'owned-dashboard-id'}}
    def request(command, **kwargs):
        assert command[:4] == ['docker', 'exec', '-i', 'owned-dashboard-id']
        data = json.loads(kwargs['input'])
        assert data['base'] == 'http://patient:8000' and data['requests'][0]['path'] == '/events'
        assert 'trust_env=False,follow_redirects=False' in command[-1]
        return SimpleNamespace(returncode=0, stdout=json.dumps([{'status': 201, 'text': '{"event_id":"synthetic"}', 'elapsed_ms': 1}]))
    monkeypatch.setattr(e2e.subprocess, 'run', request)
    with pytest.raises(e2e.IsolationError):
        stack.http('patient', 'GET', '//remote.example/')
    with pytest.raises(e2e.IsolationError):
        stack.http('production', 'GET', '/')
    stack.http('patient', 'POST', '/events', {'payload': {'clinical': 'must-not-record'}},
               headers={'Authorization': 'Bearer generated-secret'}, expected=201)
    serialized = json.dumps(stack.report)
    assert 'must-not-record' not in serialized and 'generated-secret' not in serialized


def test_network_partition_restores_on_scenario_exception(monkeypatch, stack):
    stack.confirmed = True
    stack.state['owned'] = {'containers': {'hospital-a': 'owned-a'}, 'networks': {'owned_backend': 'network-id'}}
    stack.config = {'networks': {'default': {'name': 'owned_backend'}}}
    monkeypatch.setattr(stack, 'fault_gate', lambda: stack.state['owned'])
    monkeypatch.setattr(stack, 'save', lambda: None)
    monkeypatch.setattr(stack, 'wait_ready', lambda services: None)
    commands = []
    monkeypatch.setattr(stack, 'docker', lambda *args: commands.append(args))
    with pytest.raises(RuntimeError, match='scenario failed'):
        with stack.disconnected(['hospital-a']):
            raise RuntimeError('scenario failed')
    assert commands == [('network', 'disconnect', 'network-id', 'owned-a'),
                        ('network', 'connect', '--alias', 'hospital-a', 'network-id', 'owned-a')]
    assert stack.active_faults == []


def test_cleanup_cannot_remove_a_volume_whose_ownership_changed(monkeypatch, stack):
    stack.confirmed = True
    stack.state['baseline'] = {'volumes': [], 'containers': {}, 'networks': {}}
    monkeypatch.setattr(stack, 'fault_gate', lambda **kwargs: {'containers': {}, 'volumes': ['owned-volume'], 'networks': {}})
    monkeypatch.setattr(stack, 'collect', lambda: None)
    monkeypatch.setattr(stack, 'save', lambda: None)
    monkeypatch.setattr(stack, 'compose', lambda *args: None)
    monkeypatch.setattr(stack, 'inspect', lambda *args: [{'Labels': {e2e.LABEL: 'someone-else'}}])
    monkeypatch.setattr(stack, 'docker', lambda *args: pytest.fail('Volume deletion must be refused'))
    with pytest.raises(e2e.IsolationError, match='ownership'):
        stack.cleanup()


def test_e2e_dispatch_does_not_run_unit_or_live_existing_integrations(monkeypatch, tmp_path):
    monkeypatch.setattr(e2e, 'run_e2e', lambda **kwargs: (2, tmp_path))
    monkeypatch.setattr(project_verify, 'run', lambda *a, **k: pytest.fail('Standalone E2E must dispatch directly'))
    assert project_verify.main(['--end-to-end']) == 2


def test_sanitization_removes_generated_credentials_and_database_urls():
    raw = 'Bearer generated-secret postgresql://user:password@postgres/test generated-secret'
    result = e2e.sanitized(raw, ['generated-secret'])
    assert 'generated-secret' not in result and 'user:password' not in result


@pytest.mark.parametrize('fault', [None, 'bind', 'image', 'environment', 'foreign_peer', 'foreign_label'])
def test_runtime_proof_checks_actual_mounts_images_environment_and_peers(monkeypatch, tmp_path, fault):
    config, project, nonce = safe_config()
    target = tmp_path / 'compose.private.json'
    target.write_text(json.dumps(config))
    state = {'project': project, 'nonce': nonce, 'settings': {}, 'config_sha256': e2e.digest(target),
             'ports': {name: 40000 + i for i, name in enumerate(e2e.SERVICES)},
             'baseline': valid_baseline(),
             'app_image_id': 'owned-app-image', 'postgres_image_id': 'owned-pg-image'}
    stack = e2e.Stack(tmp_path, state, {})
    monkeypatch.setattr(project_verify, 'require_local_docker', lambda: None)
    monkeypatch.setattr(stack, 'save', lambda: None)
    rows = []
    for index, service in enumerate(e2e.SERVICES):
        actual = config['services'][service]
        mounts = [{'Type': 'volume', 'Name': config['volumes'][m['source']]['name'], 'Destination': m['target']} for m in actual.get('volumes', [])]
        rows.append({'Id': 'owned-' + service, 'Config': {'Labels': {e2e.LABEL: nonce, 'com.docker.compose.service': service, 'com.docker.compose.project': project},
                     'Env': [k + '=' + str(v) for k, v in actual['environment'].items()]},
                     # Docker places named-volume specifications here too.
                     'HostConfig': {'Privileged': False, 'Binds': ['owned-volume:/data:rw'] if mounts else []},
                     'Image': 'owned-pg-image' if service == 'postgres' else 'owned-app-image',
                     'Mounts': mounts, 'NetworkSettings': {'Networks': {project + '_default': {}},
                         'Ports': {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(40000 + index)}]}}})
    if fault == 'bind':
        rows[5]['Mounts'][0]['Type'] = 'bind'
    elif fault == 'image':
        rows[1]['Image'] = 'production-image'
    elif fault == 'environment':
        rows[2]['Config']['Env'] = ['METAGUARD_REQUIRED=0']
    elif fault == 'foreign_label':
        rows[1]['Config']['Labels'][e2e.LABEL] = 'other-owner'
    def docker(*args, **kwargs):
        if args[0] == 'ps':
            return '\n'.join(r['Id'] for r in rows)
        resources = config['volumes'] if args[0] == 'volume' else config['networks']
        return '\n'.join(v['name'] for v in resources.values())
    def inspect(kind, identifiers):
        if kind == 'container':
            return rows
        if kind == 'volume':
            return [{'Name': name, 'Labels': {e2e.LABEL: nonce, 'com.docker.compose.project': project}} for name in identifiers]
        return [{'Name': name, 'Id': 'new-network-' + str(index), 'Internal': True,
                 'Labels': {e2e.LABEL: nonce, 'com.docker.compose.project': project}, 'Containers': {'foreign': {}} if fault == 'foreign_peer' else {r['Id']: {} for r in rows}}
                for index, name in enumerate(identifiers)]
    monkeypatch.setattr(stack, 'read_docker', docker)
    monkeypatch.setattr(stack, 'inspect', inspect)
    if fault:
        with pytest.raises(e2e.IsolationError):
            stack.prove_ownership()
    else:
        proof = stack.prove_ownership()
        assert len(proof['containers']) == 9
        assert proof['existing_volume_names_disjoint']


def test_sql_reads_use_owned_database_identity_and_no_host_fallback(monkeypatch, stack):
    stack.state.update(owned={'containers': {'dashboard': 'owned-client'}}, postgres_system_identifier='server-id')
    def run(command, **kwargs):
        assert command[:4] == ['docker', 'exec', '-i', 'owned-client']
        query = json.loads(kwargs['input'])
        assert query['identity'] == 'server-id' and not query['fixture']
        assert 'default_transaction_read_only=on' in command[-1]
        return SimpleNamespace(returncode=0, stdout='[[1]]')
    monkeypatch.setattr(e2e.subprocess, 'run', run)
    assert stack.query('SELECT count(*) FROM events') == [(1,)]
    with pytest.raises(e2e.IsolationError):
        stack.query('DELETE FROM events')


def test_failed_sql_transport_is_not_silently_retried_against_localhost(monkeypatch, stack):
    stack.state.update(owned={'containers': {'dashboard': 'owned-client'}}, postgres_system_identifier='server-id')
    calls = []
    monkeypatch.setattr(e2e.subprocess, 'run', lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=1))
    with pytest.raises(e2e.IsolationError, match='no external fallback'):
        stack.query('SELECT 1')
    assert len(calls) == 1


def test_sql_failure_records_only_safe_diagnostics(monkeypatch, stack):
    stack.state.update(owned={'containers': {'dashboard': 'owned-client'}}, postgres_system_identifier='server-id')
    detail = {'error_type': 'ProgrammingError', 'phase': 'query', 'transaction_status': 'INTRANS',
              'sqlstate': None, 'message': 'generated-secret private SQL payload'}
    monkeypatch.setattr(e2e.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=1, stderr=json.dumps(detail)))
    with pytest.raises(e2e.IsolationError, match='ProgrammingError'):
        stack.query("SELECT 'private SQL payload'")
    failure = stack.report['sql_failures'][0]
    assert failure['error_type'] == 'ProgrammingError'
    assert failure['phase'] == 'query' and failure['transaction_status'] == 'INTRANS'
    assert len(failure['query_sha256']) == 64
    published = (stack.directory / 'report.json').read_text()
    assert 'private SQL payload' not in published and 'generated-secret' not in published


def test_unstructured_sql_stderr_is_never_published(monkeypatch, stack):
    stack.state.update(owned={'containers': {'dashboard': 'owned-client'}}, postgres_system_identifier='server-id')
    monkeypatch.setattr(e2e.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=1, stderr='generated-secret private SQL payload'))
    with pytest.raises(e2e.IsolationError, match='no external fallback'):
        stack.query('SELECT 1')
    assert set(stack.report['sql_failures'][0]) == {'query_sha256', 'fixture'}
    assert 'generated-secret' not in (stack.directory / 'report.json').read_text()


def test_windows_tty_eof_leaves_confirmation_pending(monkeypatch, stack):
    monkeypatch.setattr(e2e.sys.stdin, 'isatty', lambda: True)
    def eof(prompt):
        raise EOFError()
    monkeypatch.setattr('builtins.input', eof)
    assert e2e.prompt_confirmation(stack) is None
    assert not stack.confirmed


@pytest.mark.parametrize('options', [['--confirm-isolation', 'stale-token'], ['--e2e-cleanup-only']])
def test_confirmation_options_without_a_reviewed_run_never_provision(monkeypatch, options):
    monkeypatch.setattr(e2e, 'create_stack', lambda: pytest.fail('No reviewed run: must not provision'))
    with pytest.raises(SystemExit) as error:
        project_verify.main(['--end-to-end', *options])
    assert error.value.code == 2


def test_preservation_difference_remains_a_failed_check(monkeypatch, stack):
    stack.state['baseline'] = valid_baseline()
    stack.state['baseline']['volumes']['old-volume'] = {'Name': 'old-volume'}
    monkeypatch.setattr(stack, 'inventory', valid_baseline)
    monkeypatch.setattr(stack, 'save', lambda: None)
    with pytest.raises(e2e.IsolationError, match='investigation required'):
        stack.verify_preservation()
    assert stack.report['checks'][-1]['status'] == 'FAIL'
    assert stack.report['preservation']['changes']['changed_original_volume_names'] == ['old-volume']
    assert stack.state['preservation_block']


def test_fault_intent_persists_without_implicitly_granting_confirmation(stack):
    stack.active_faults.append({'kind': 'stopped', 'services': ['controller']})
    stack.save()
    state = json.loads((stack.directory / 'session.private.json').read_text())
    restored = e2e.Stack(stack.directory, state, stack.report)
    assert restored.active_faults == [{'kind': 'stopped', 'services': ['controller']}]
    assert not restored.confirmed
    with pytest.raises(e2e.IsolationError, match='confirmation'):
        restored.restore_faults()
