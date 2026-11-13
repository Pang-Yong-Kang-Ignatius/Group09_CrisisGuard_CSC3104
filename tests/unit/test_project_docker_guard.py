"""Direct live-engine and preservation contracts; no real Docker/WSL commands."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from infrastructure.scripts import project_docker_guard as guard
from infrastructure.scripts import project_e2e as e2e

EPOCH = '11111111-1111-1111-1111-111111111111|123|456'
ENGINE = {'daemon_id': 'local-engine', 'epoch': guard.parse_epoch(EPOCH), 'transport': 'direct-dockerd-unix-socket'}


def valid_baseline():
    return {'schema_version': 2, 'engine': deepcopy(ENGINE), 'containers': {}, 'volumes': {}, 'networks': {}}


def network(nid='bridge-id', *, builtin=True):
    return {'Id': nid, 'Name': 'bridge' if builtin else 'existing_default',
            'Created': '2026-11-02T12:14:15Z', 'Driver': 'bridge', 'Scope': 'local', 'Internal': False,
            'Options': {'com.docker.network.bridge.default_bridge': 'true'} if builtin else {},
            'Labels': {} if builtin else {'com.docker.compose.project': 'existing'}, 'Containers': {}}


class FakeDaemon:
    def __init__(self):
        self.calls = []
        self.info_count = 0
        self.network_lists = 0
        self.fail = False
        self.restart = False
        self.change_network = False
        self.stale_clock = False

    def execute(self, command, **kwargs):
        args = command[command.index('crisisguard-live-reader') + 1:]
        self.calls.append((command, args))
        if self.fail:
            return SimpleNamespace(returncode=71, stdout='', stderr='daemon asleep')
        if args[0] == 'info':
            self.info_count += 1
            body = json.dumps({'ID': 'local-engine', 'SystemTime': '2026-11-02T12:14:15.' +
                              f'{1 if self.stale_clock else self.info_count:06d}+00:00'})
        elif args[:2] == ['network', 'ls']:
            self.network_lists += 1
            body = 'new-bridge' if self.change_network and self.network_lists > 1 else 'bridge-id'
        elif args[:2] == ['network', 'inspect']:
            body = json.dumps([network(args[2])])
        elif args[0] == 'ps' or args[:2] == ['volume', 'ls']:
            body = ''
        else:
            pytest.fail('Unexpected direct-daemon request: ' + repr(args))
        return SimpleNamespace(returncode=0, stdout=EPOCH + '\0' + body + '\0' +
                               (EPOCH.replace('|123|', '|124|') if self.restart else EPOCH))


def reader(daemon):
    return guard.LiveDocker({}, execute=daemon.execute, platform='nt')


def test_readiness_and_inventory_bypass_windows_cache_after_engine_wake():
    daemon = FakeDaemon()
    daemon.fail = True
    live = reader(daemon)
    with pytest.raises(guard.DockerGuardError, match='no cached inventory fallback'):
        guard.stable_baseline(live)
    assert daemon.network_lists == 0
    # Once the engine is live, only the direct socket's new bridge is accepted.
    daemon.fail = False
    baseline = guard.stable_baseline(live)
    assert baseline['engine'] == ENGINE
    assert set(baseline['networks']) == {'bridge-id'}
    assert baseline['networks']['bridge-id']['kind'] == 'builtin'
    assert all(command[0] == 'wsl.exe' for command, _ in daemon.calls)
    script = daemon.calls[0][0][daemon.calls[0][0].index('-c') + 1]
    assert '--host "unix://$socket"' in script and '/proc/$pid/root/run/docker.sock' in script
    assert daemon.calls[0][1][0] == 'info'  # live verification precedes any inventory


@pytest.mark.parametrize('condition', ['restart', 'stale_clock', 'change_network'])
def test_unstable_engine_or_inventory_never_becomes_a_baseline(condition):
    daemon = FakeDaemon()
    setattr(daemon, condition, True)
    with pytest.raises(guard.DockerGuardError, match='investigation required'):
        guard.stable_baseline(reader(daemon))


def test_daemon_restart_is_detected_even_with_same_persisted_daemon_id():
    daemon = FakeDaemon()
    live = reader(daemon)
    live.ready()
    daemon.restart = True
    with pytest.raises(guard.DockerGuardError, match='restarted'):
        live.output('ps', '-aq')


@pytest.mark.parametrize('args', [('network', 'rm', 'existing'), ('start', 'existing'), ('system', 'prune')])
def test_live_reader_rejects_mutations_without_spawning_process(args):
    calls = []
    live = guard.LiveDocker({}, execute=lambda *a, **k: calls.append(a))
    with pytest.raises(guard.DockerGuardError, match='read-only'):
        live.output(*args)
    assert calls == []


def test_builtin_and_user_network_changes_are_distinct_but_both_fail():
    before = valid_baseline()
    before['networks'] = {'bridge-id': guard.network_snapshot(network()),
                          'user-id': guard.network_snapshot(network('user-id', builtin=False))}
    after = deepcopy(before)
    after['networks'].pop('bridge-id')
    after['networks']['user-id']['containers']['new-peer'] = {'Name': 'foreign'}
    changes = guard.preservation_changes(before, after)
    assert changes['changed_builtin_network_ids'] == ['bridge-id']
    assert changes['changed_user_network_ids'] == ['user-id']


def test_user_named_bridge_is_not_exempted_as_builtin():
    row = network(builtin=False)
    row['Name'] = 'bridge'
    assert guard.network_snapshot(row)['kind'] == 'user_defined'


@pytest.mark.parametrize('field', ['name', 'labels', 'containers', 'created'])
def test_preservation_detects_network_metadata_and_attachment_changes(field):
    before = valid_baseline()
    before['networks']['bridge-id'] = guard.network_snapshot(network())
    after = deepcopy(before)
    after['networks']['bridge-id'][field] = {'changed': True} if field in ('labels', 'containers') else 'changed'
    assert guard.preservation_changes(before, after)['changed_builtin_network_ids'] == ['bridge-id']


def test_legacy_resume_stops_without_touching_original_evidence_or_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(e2e, 'ROOT', tmp_path)
    directory = tmp_path / '.project-test-results' / ('e2e-' + 'a' * 32)
    directory.mkdir(parents=True)
    state = {'version': 1, 'nonce': 'a' * 32, 'project': 'cg-e2e-' + 'a' * 24,
             'baseline': {'containers': {}, 'volumes': [], 'networks': ['cached-old-id']}}
    (directory / 'session.private.json').write_text(json.dumps(state))
    (directory / 'report.json').write_text(json.dumps({'phase': 'awaiting isolation confirmation', 'checks': []}))
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    monkeypatch.setattr(e2e.subprocess, 'run', lambda *a, **k: pytest.fail('Legacy resume must not contact Docker'))
    code, result = e2e.run_e2e(resume=str(directory), confirmation='approved', cleanup_only=True)
    assert code == 1 and result == directory
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


@pytest.fixture
def stack(tmp_path):
    state = {'project': 'cg-e2e-' + 'a' * 24, 'nonce': 'a' * 32, 'settings': {}, 'ports': {}, 'baseline': valid_baseline()}
    value = e2e.Stack(tmp_path, state, {'checks': [], 'http': [], 'phase': 'test', 'revision': 'test'})
    value.confirmed = True
    return value


@pytest.mark.parametrize('action', ['restart', 'stop', 'start', 'cleanup', 'raw_rm', 'raw_network_rm', 'raw_volume_rm', 'configure', 'restore'])
def test_failed_preservation_blocks_every_disruptive_entrypoint(monkeypatch, stack, action):
    monkeypatch.setattr(stack, 'verify_preservation', lambda: False)
    monkeypatch.setattr(stack, 'command', lambda *a, **k: pytest.fail('A failed check must prevent every subprocess'))
    monkeypatch.setattr(stack, 'prove_ownership', lambda **k: pytest.fail('Preservation must pass first'))
    operations = {'restart': lambda: stack.compose('restart', 'hospital-a'),
        'stop': lambda: stack.compose('stop', 'controller'), 'start': lambda: stack.compose('start', 'controller'),
        'cleanup': stack.cleanup, 'raw_rm': lambda: stack.docker('rm', 'unowned'),
        'raw_network_rm': lambda: stack.docker('network', 'rm', 'unowned'),
        'raw_volume_rm': lambda: stack.docker('volume', 'rm', 'unowned'),
        'configure': lambda: stack.configure({'hospital-a': {'AUTO_REPLAY': '1'}}), 'restore': stack.restore_faults}
    with pytest.raises(e2e.IsolationError, match='Preservation check did not pass'):
        operations[action]()


@pytest.mark.parametrize('kind', ['container', 'network', 'volume'])
def test_foreign_destructive_target_rejected_after_gates(monkeypatch, stack, kind):
    proof = {'containers': {'hospital-a': 'owned-cid'}, 'networks': {'owned': 'owned-nid'}, 'volumes': ['owned-volume']}
    monkeypatch.setattr(stack, 'fault_gate', lambda **k: proof)
    monkeypatch.setattr(stack, 'command', lambda *a, **k: pytest.fail('Unowned target must not be executed'))
    args = ('rm', 'foreign') if kind == 'container' else (kind, 'rm', 'foreign')
    with pytest.raises(e2e.IsolationError, match='[Uu]nowned'):
        stack.docker(*args)


def test_latched_failure_cannot_be_cleared_by_a_later_matching_inventory(monkeypatch, stack):
    before = valid_baseline()
    before['networks']['bridge-id'] = guard.network_snapshot(network())
    stack.state['baseline'] = before
    changed = deepcopy(before)
    changed['networks'] = {}
    monkeypatch.setattr(stack, 'inventory', lambda: changed)
    with pytest.raises(e2e.IsolationError, match='investigation required'):
        stack.verify_preservation()
    assert stack.report['checks'][-1]['status'] == 'FAIL'
    monkeypatch.setattr(stack, 'inventory', lambda: before)
    with pytest.raises(e2e.IsolationError, match='latched'):
        stack.require_preserved()


def test_ownership_failure_prevents_restart_even_after_preservation_passes(monkeypatch, stack):
    monkeypatch.setattr(stack, 'require_preserved', lambda: None)
    monkeypatch.setattr(stack, 'prove_ownership', lambda **k: e2e.require(False, 'Unowned resource'))
    monkeypatch.setattr(stack, 'command', lambda *a, **k: pytest.fail('Ownership failure must stop restart'))
    with pytest.raises(e2e.IsolationError, match='Unowned'):
        stack.compose('restart', 'hospital-a')
