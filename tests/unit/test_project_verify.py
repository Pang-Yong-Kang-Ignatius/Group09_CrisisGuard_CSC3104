"""Runner safety decisions and evidence accounting; no real services invoked."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from infrastructure.scripts import project_verify as verify


def test_environment_cannot_inherit_production_targets_or_plugins(monkeypatch, tmp_path):
    for key in ('DATABASE_URL', 'CONTROLLER_TEST_DATABASE_URL', 'CLOUD_URL',
                'HOSPITAL_DB_PATH', 'PYTEST_ADDOPTS', 'NODE_OPTIONS', 'COVERAGE_PROCESS_START'):
        monkeypatch.setenv(key, 'private-deployment-sentinel')
    env = verify.test_environment(tmp_path)
    assert 'private-deployment-sentinel' not in env.values()
    assert env['AUTO_REPLAY'] == '0'
    assert env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'
    assert Path(env['HOSPITAL_DB_PATH']).parent == tmp_path


def test_junit_counts_failures_errors_and_skips_without_double_counting(tmp_path):
    path = tmp_path / 'junit.xml'
    path.write_text('<testsuites><testsuite><testcase/><testcase><failure/></testcase>'
                    '<testcase><error/></testcase><testcase><skipped/></testcase></testsuite></testsuites>')
    counts = verify.junit_counts(path)
    assert counts == {'total': 4, 'passed': 1, 'failed': 2, 'skipped': 1}
    assert verify.suite_status(0, counts) == 'FAIL'
    assert verify.suite_status(0, {'total': 1, 'passed': 0, 'failed': 0, 'skipped': 1}) == 'SKIP'
    assert verify.suite_status(5, None) == 'FAIL'


def test_zip_source_has_no_verified_git_revision(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, 'ROOT', tmp_path)
    monkeypatch.setattr(verify, 'run', lambda *a, **k: pytest.fail('Git must not run for a ZIP'))
    assert verify.repository_revision() is None


def test_revision_must_belong_to_the_source_tree(monkeypatch, tmp_path):
    (tmp_path / '.git').mkdir()
    monkeypatch.setattr(verify, 'ROOT', tmp_path)
    head = 'a' * 40
    def run(command, **kwargs):
        value = str(tmp_path.parent) if command[-1] == '--show-toplevel' else head
        return SimpleNamespace(returncode=0, stdout=value + '\n')
    monkeypatch.setattr(verify, 'run', run)
    assert verify.repository_revision() is None
    def owned_run(command, **kwargs):
        value = str(tmp_path) if command[-1] == '--show-toplevel' else head
        return SimpleNamespace(returncode=0, stdout=value + '\n')
    monkeypatch.setattr(verify, 'run', owned_run)
    assert verify.repository_revision() == head


def test_evaluation_gate_tracks_archived_report_and_missing_files(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, 'ROOT', tmp_path)
    assert 'docs/archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md' in verify.EVALUATION_DOCUMENTS
    for relative in verify.EVALUATION_DOCUMENTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic evidence')
    assert verify.missing_evaluation_documents() == []
    (tmp_path / verify.EVALUATION_DOCUMENTS[0]).unlink()
    assert verify.missing_evaluation_documents() == [verify.EVALUATION_DOCUMENTS[0]]


@pytest.mark.parametrize('target', [None, '', 'localhost', 'test_database'])
def test_integration_requires_explicit_container_identity_before_any_command(monkeypatch, target):
    monkeypatch.setattr(verify, 'run', lambda *a, **k: pytest.fail('No command allowed without identity'))
    with pytest.raises(ValueError):
        verify.postgres_target(target)


@pytest.mark.parametrize('fault', ['no_label', 'bad_nonce', 'persistent_mount', 'stopped', 'wildcard_port'])
def test_integration_rejects_unproven_isolation_before_connecting(monkeypatch, fault):
    monkeypatch.setattr(verify, 'require_local_docker', lambda: None)
    data = {'Config': {'Labels': {'org.crisisguard.test-isolation': 'ef7df18b-fde0-4cfb-a109-70474a39d385'}},
            'Mounts': [], 'State': {'Running': True},
            'NetworkSettings': {'Ports': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '65431'}]}}}
    if fault == 'no_label':
        data['Config']['Labels'] = {}
    elif fault == 'bad_nonce':
        data['Config']['Labels']['org.crisisguard.test-isolation'] = 'test'
    elif fault == 'persistent_mount':
        data['Mounts'] = [{'Type': 'volume', 'Name': 'existing-user-data'}]
    elif fault == 'stopped':
        data['State']['Running'] = False
    else:
        data['NetworkSettings']['Ports']['5432/tcp'][0]['HostIp'] = '0.0.0.0'
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps([data]))
    monkeypatch.setattr(verify, 'run', run)
    with pytest.raises(ValueError):
        verify.postgres_target('a' * 64)
    assert calls == [['docker', 'inspect', 'a' * 64]]


def test_route_evidence_cannot_cross_cloud_service_boundaries():
    routes = [{'service': 'cloud/patient_service/main.py', 'method': 'GET', 'route': '/metrics'}]
    evidence = [{'module': 'cloud.telemetry', 'service_module': 'cloud.emergency_service.main',
                 'method': 'GET', 'route': '/metrics', 'status': 200, 'test': 'wrong-service'}]
    matrix = verify.route_matrix(routes, evidence)
    assert matrix[0]['status'] == 'NOT VERIFIED'
    assert matrix[0]['test_references'] == []
    assert 'unauthorized' not in matrix[0]['missing_scenarios']


def test_nonzero_check_fails_overall_but_missing_coverage_does_not(monkeypatch, tmp_path):
    monkeypatch.setattr(verify, 'ROOT', tmp_path)
    monkeypatch.setattr(verify.shutil, 'which', lambda name: None)
    monkeypatch.setattr(verify, 'junit_counts', lambda path: {'total': 1, 'passed': 0, 'failed': 1, 'skipped': 0})
    def run(command, **kwargs):
        return SimpleNamespace(returncode=1 if 'pytest' in command else 0, stdout='test revision', stderr='')
    monkeypatch.setattr(verify, 'run', run)
    monkeypatch.chdir(tmp_path)
    assert verify.main(['--quick']) == 1
    summary = json.loads(next(tmp_path.rglob('summary.json')).read_text())
    assert summary['revision'] is None
    assert summary['checks']['Source revision'] == 'NOT VERIFIED'
    assert summary['checks']['Python unit tests'] == 'FAIL'
    assert summary['checks']['JavaScript unit tests'] == 'SKIP'
    assert summary['checks']['Proposal acceptance'] == 'NOT VERIFIED'


def test_safe_allowlist_excludes_live_write_and_fault_integrations():
    assert set(verify.MOCKED) == {'tests/integration/test_cloud_apis.py',
                                  'tests/integration/test_manual_replay_security.py'}
    assert all('postgres' in name for name in verify.POSTGRES)
    assert 'test_concurrency.py' not in ' '.join(verify.MOCKED + verify.POSTGRES)


@pytest.mark.parametrize('host,allowed', [('npipe:////./pipe/docker_engine', True),
                                         ('unix:///var/run/docker.sock', True),
                                         ('tcp://127.0.0.1:2375', False),
                                         ('ssh://remote-host', False)])
def test_docker_verification_never_targets_network_context(monkeypatch, host, allowed):
    monkeypatch.setenv('DOCKER_HOST', host)
    monkeypatch.setattr(verify, 'run', lambda *a, **k: pytest.fail('Explicit endpoint requires no discovery'))
    if allowed:
        verify.require_local_docker()
    else:
        with pytest.raises(ValueError):
            verify.require_local_docker()


@pytest.mark.parametrize('failure', [None, 'identity', 'objects', 'schemas'])
def test_postgres_proof_uses_read_only_matching_empty_server(monkeypatch, failure):
    import psycopg
    monkeypatch.setattr(verify, 'require_local_docker', lambda: None)
    data = {'Id': 'a' * 64, 'Config': {
        'Labels': {'org.crisisguard.test-isolation': 'ef7df18b-fde0-4cfb-a109-70474a39d385'},
        'Env': ['POSTGRES_USER=scratch', 'POSTGRES_DB=scratch', 'POSTGRES_PASSWORD=synthetic']},
        'Mounts': [{'Type': 'tmpfs'}], 'State': {'Running': True},
        'NetworkSettings': {'Ports': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '65431'}]}}}
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps([data]) if command[1] == 'inspect' else 'server-one\n')
    monkeypatch.setattr(verify, 'run', run)
    queries = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, query):
            queries.append(query)
            if 'system_identifier' in query:
                value = 'server-two' if failure == 'identity' else 'server-one'
            elif 'pg_class' in query:
                value = 1 if failure == 'objects' else 0
            else:
                value = 1 if failure == 'schemas' else 0
            return SimpleNamespace(fetchone=lambda: (value,))
    def connect(url, **kwargs):
        assert kwargs == {'options': '-c default_transaction_read_only=on'}
        return Connection()
    monkeypatch.setattr(psycopg, 'connect', connect)
    if failure:
        with pytest.raises(ValueError):
            verify.postgres_target('a' * 64)
    else:
        url, proof = verify.postgres_target('a' * 64)
        assert '65431' in url
        assert 'synthetic' not in json.dumps(proof)
        assert proof['persistent_mounts'] == 0
    assert all(query.startswith('SELECT') for query in queries)
    assert commands[0] == ['docker', 'inspect', 'a' * 64]
    assert commands[1][:3] == ['docker', 'exec', 'a' * 64]


def test_subset_status_does_not_promote_unexecuted_security_checks(tmp_path):
    path = tmp_path / 'suite.xml'
    path.write_text('<testsuites><testcase classname="tests.unit.other"/>'
                    '<testcase classname="tests.unit.security"><skipped/></testcase></testsuites>')
    assert verify.subset_status(path, ('security',)) == 'SKIP'
    assert verify.subset_status(path, ('missing',)) == 'NOT VERIFIED'


def test_source_discovery_expands_registered_routes_without_importing_apps():
    inventory, routes = verify.source_inventory()
    assert any(item['file'] == 'cloud/controller/storage.py' for item in inventory)
    metrics = [r for r in routes if r['route'] == '/metrics']
    assert {r['service'] for r in metrics} == {
        'cloud/patient_service/main.py', 'cloud/emergency_service/main.py', 'cloud/admin_service/main.py'}
    traffic = [r for r in routes if r['route'] == '/api/test-traffic']
    assert {r['method'] for r in traffic} == {'GET', 'POST'}
    assert all(r['service'] == 'dashboard/main.py' for r in traffic)


def test_public_reads_do_not_require_irrelevant_credential_scenarios():
    route = {'service': 'edge/capture_api/main.py', 'method': 'POST', 'route': '/capture'}
    assert 'unauthorized' not in verify.relevant_scenarios(route)
    assert 'forbidden' in verify.relevant_scenarios(route)  # Wrong hospital identity.
    route = {'service': 'cloud/patient_service/main.py', 'method': 'GET', 'route': '/events/{event_id}'}
    assert verify.relevant_scenarios(route) == ['success', 'missing resource', 'dependency failure']
