"""Release artifacts must preserve identity and reject drift without activation."""
import hashlib
import json

import pytest

from infrastructure.scripts import release_checkpoint as release


def image(name):
    return 'sha256:' + hashlib.sha256(name.encode()).hexdigest()


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    env = tmp_path / 'private.env'
    compose = tmp_path / 'compose.yaml'
    env.write_text('PASSWORD=PRIVATE_VALUE\n')
    compose.write_text('services: {}\n')
    state = {'commands': [], 'services': {}, 'config': {'services': {}},
             'volume': {'Name': 'team_data', 'CreatedAt': 'original-time', 'Driver': 'local'}}
    for name in ('hospital', 'patient', 'emergency', 'admin', 'postgres'):
        cid = hashlib.sha256(name.encode()).hexdigest()
        mounts = [{'Type': 'volume', 'Name': 'team_data', 'Destination': '/data' if name == 'hospital' else '/var/lib/postgresql/data'}] if name in ('hospital', 'postgres') else []
        state['services'][name] = {'Id': cid, 'Image': image('postgres' if name == 'postgres' else 'previous'),
            'Config': {'Labels': {'com.docker.compose.project': 'team'}, 'Env': ['PASSWORD=PRIVATE_VALUE']},
            'State': {'Running': True, 'Health': {'Status': 'healthy'}}, 'Mounts': mounts}
        state['config']['services'][name] = {'environment': {'PASSWORD': 'PRIVATE_VALUE'}}
    def fake(args):
        state['commands'].append(args)
        if args[:2] == ['docker', 'compose']:
            if 'config' in args: return json.dumps(state['config'])
            return state['services'][args[-1]]['Id']
        if args[:2] == ['docker', 'inspect']:
            return json.dumps([next(v for v in state['services'].values() if v['Id'] == args[-1])])
        if args[:3] == ['docker', 'volume', 'inspect']: return json.dumps([state['volume']])
        if args[:3] == ['docker', 'image', 'inspect']:
            return json.dumps([{'Id': args[-1] if args[-1].startswith('sha256:') else image('candidate')}])
        if args[:3] == ['docker', 'image', 'tag']: return ''
        pytest.fail('Unexpected mutating Docker command: ' + str(args[:3]))
    monkeypatch.setattr(release, 'run', fake)
    state.update(env=env, compose=compose, path=tmp_path / 'checkpoint')
    return state


def prepare(runtime, role='hospital'):
    return release.prepare(role, runtime['env'], [runtime['compose']], runtime['path'], project='team')


@pytest.mark.parametrize('role', ['hospital', 'cloud'])
def test_prepare_pins_actual_images_and_excludes_secrets(runtime, role):
    state = prepare(runtime, role)
    assert state['volume']['Name'] == 'team_data'
    assert 'PRIVATE_VALUE' not in (runtime['path'] / 'checkpoint.json').read_text()
    override = json.loads((runtime['path'] / 'rollback.compose.json').read_text())
    assert set(override['services']) == ({'hospital'} if role == 'hospital' else {'patient','emergency','admin'})
    assert 'postgres' not in override['services']
    assert all(v == {'image': image('previous'), 'pull_policy': 'never'} for v in override['services'].values())
    release.verify(runtime['path'])
    assert not any(command in args for args in runtime['commands'] for command in ('up','down','stop','rm','restart'))


def test_existing_checkpoint_cannot_be_overwritten(runtime):
    prepare(runtime)
    original = (runtime['path'] / 'checkpoint.json').read_bytes()
    with pytest.raises(RuntimeError, match='already exists'):
        prepare(runtime)
    assert (runtime['path'] / 'checkpoint.json').read_bytes() == original


@pytest.mark.parametrize('fault', ['env_file', 'resolved_env', 'runtime_env', 'mount', 'volume_recreated', 'unrecorded_image', 'override'])
def test_drift_blocks_use_of_checkpoint(runtime, fault):
    prepare(runtime)
    if fault == 'env_file': runtime['env'].write_text('PASSWORD=changed\n')
    elif fault == 'resolved_env': runtime['config']['services']['hospital']['environment']['PASSWORD'] = 'changed'
    elif fault == 'runtime_env': runtime['services']['hospital']['Config']['Env'] = ['PASSWORD=changed']
    elif fault == 'mount': runtime['services']['hospital']['Mounts'][0]['Name'] = 'other_data'
    elif fault == 'volume_recreated': runtime['volume']['CreatedAt'] = 'new-time'
    elif fault == 'unrecorded_image': runtime['services']['hospital']['Image'] = image('unrelated')
    else: (runtime['path'] / 'rollback.compose.json').write_text('{}')
    with pytest.raises(RuntimeError):
        release.verify(runtime['path'])


def test_running_configuration_must_match_selected_settings(runtime):
    runtime['services']['hospital']['Config']['Env'] = ['PASSWORD=not-the-env-file']
    with pytest.raises(RuntimeError, match='environment differs'):
        prepare(runtime)
    assert not runtime['path'].exists()


def test_database_container_cannot_be_recreated_in_this_procedure(runtime):
    prepare(runtime, 'cloud')
    runtime['services']['postgres']['Id'] = 'b' * 64
    with pytest.raises(RuntimeError, match='PostgreSQL container'):
        release.verify(runtime['path'])


def test_candidate_is_image_only_and_failed_container_can_be_checked_for_backout(runtime):
    prepare(runtime)
    staged = release.candidate(runtime['path'], 'candidate:tag', 'a'*40, 'Reviewed compatible app-only release')
    runtime['services']['hospital']['Image'] = staged['image']
    runtime['services']['hospital']['State'] = {'Running': False}
    release.verify(runtime['path'])  # Health remains a separate mandatory gate.
    override = json.loads((runtime['path'] / 'candidate.compose.json').read_text())
    assert override == {'services': {'hospital': {'image': image('candidate'), 'pull_policy': 'never'}}}
    with pytest.raises(RuntimeError, match='already staged'):
        release.candidate(runtime['path'], 'another:tag', 'b'*40, 'another attempt')


def test_no_compatibility_note_prevents_candidate_staging(runtime):
    prepare(runtime)
    with pytest.raises(RuntimeError, match='compatibility'):
        release.candidate(runtime['path'], 'candidate:tag', 'a'*40, '')
    assert not (runtime['path'] / 'candidate.json').exists()


@pytest.mark.parametrize('active,expected', [('previous','candidate'), ('candidate','previous')])
def test_gate_rejects_wrong_release_even_when_it_is_a_known_image(runtime, active, expected):
    prepare(runtime)
    staged = release.candidate(runtime['path'], 'candidate:tag', 'a'*40, 'Reviewed compatibility')
    runtime['services']['hospital']['Image'] = staged['image'] if active == 'candidate' else image('previous')
    with pytest.raises(RuntimeError, match='requested release target'):
        release.verify(runtime['path'], expected=expected)
