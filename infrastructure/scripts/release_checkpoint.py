"""Prepare/verify application-image rollback artifacts; never activate a release.

Requires Docker and Python standard library. Does not stop/start containers,
change configuration, restore databases or create AWS resources. See
docs/runbooks/DEPLOYMENT_UPDATES.md for backups, compatibility review and activation.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
IMAGE_ID = re.compile(r'sha256:[a-f0-9]{64}')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(args):
    result = subprocess.run(args, cwd=ROOT, stdin=subprocess.DEVNULL, text=True,
                            capture_output=True, timeout=60)
    require(result.returncode == 0, 'Docker command failed; private output suppressed')
    return result.stdout.strip()


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as output:
        json.dump(value, output, indent=2)
        output.write('\n')


def compose_args(state):
    args = ['docker', 'compose', '--project-name', state['project'], '--env-file', state['env_file']]
    for path in state['compose_files']:
        args += ['-f', path]
    return args


def image_id(reference):
    value = json.loads(run(['docker', 'image', 'inspect', reference]))[0]['Id']
    require(bool(IMAGE_ID.fullmatch(value)), 'Expected a local immutable image ID')
    return value


def inspect_service(compose, service, project=None, *, healthy=False, environment=None):
    cid = run(compose + ['ps', '--all', '-q', service])
    require(bool(re.fullmatch(r'[a-f0-9]{12,64}', cid)), 'Expected exactly one existing service container')
    info = json.loads(run(['docker', 'inspect', cid]))[0]
    actual_project = info['Config']['Labels'].get('com.docker.compose.project')
    require(bool(actual_project) and (project is None or actual_project == project), 'Compose project identity differs')
    require(bool(IMAGE_ID.fullmatch(info['Image'])), 'Container image ID is unavailable')
    if environment is not None:
        actual = dict(value.split('=', 1) for value in info['Config'].get('Env', []) if '=' in value)
        require(all(actual.get(key) == (str(value) if value is not None else None)
                    for key, value in environment.items()),
                'Running environment differs from selected Compose settings')
    if healthy:
        require(info['State'].get('Running') is True and info['State'].get('Health', {}).get('Status') == 'healthy',
                'Prepare requires running healthy containers')
    # Never persist Config.Env, command arguments or host mount paths.
    mounts = sorted([{'type': m['Type'], 'name': m.get('Name'), 'destination': m['Destination']}
                     for m in info['Mounts']], key=lambda m: m['destination'])
    return {'container': info['Id'], 'image': info['Image'], 'mounts': mounts, 'project': actual_project}


def volume_identity(name):
    value = json.loads(run(['docker', 'volume', 'inspect', name]))[0]
    return {key: value[key] for key in ('Name', 'CreatedAt', 'Driver')}


def resolved_config(compose):
    # Contains secrets in memory only; persist only its canonical SHA-256.
    config = json.loads(run(compose + ['config', '--format', 'json']))
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return config, digest


def data_volume(service, destination):
    matches = [m for m in service['mounts'] if m['destination'] == destination and m['type'] == 'volume' and m['name']]
    require(len(matches) == 1, 'This procedure requires the existing named database volume')
    return matches[0]['name']


def prepare(role, env_file, files, directory, *, project=None, hospital_service='hospital', revision='unknown'):
    require(role in ('cloud', 'hospital'), 'Invalid host role')
    require(role != 'hospital' or bool(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', hospital_service))
            and hospital_service != 'postgres', 'Invalid application service')
    env_file = Path(env_file).resolve()
    files = [Path(path).resolve() for path in files]
    require(env_file.is_file() and files and all(path.is_file() for path in files), 'Selected configuration file does not exist')
    require(revision == 'unknown' or bool(re.fullmatch(r'[a-fA-F0-9]{40,64}', revision)), 'Use a complete source revision or unknown')
    directory = Path(directory).resolve()
    require(not directory.exists(), 'Checkpoint directory already exists; use a fresh path')
    base = ['docker', 'compose', '--env-file', str(env_file)]
    if project:
        base += ['--project-name', project]
    for path in files:
        base += ['-f', str(path)]
    services = ['patient', 'emergency', 'admin'] if role == 'cloud' else [hospital_service]
    config, _ = resolved_config(base)
    previous = {name: inspect_service(base, name, project, healthy=True,
                environment=config['services'][name].get('environment', {})) for name in services}
    project = next(iter(previous.values()))['project']
    require(all(item['project'] == project for item in previous.values()), 'Application containers belong to different projects')
    database = inspect_service(base, 'postgres', project, healthy=True,
                environment=config['services']['postgres'].get('environment', {})) if role == 'cloud' else None
    name = data_volume(database or previous[hospital_service], '/var/lib/postgresql/data' if database else '/data')
    state = {'version': 1, 'created_at_utc': datetime.now(timezone.utc).isoformat(), 'role': role,
             'project': project, 'source_revision': revision, 'env_file': str(env_file),
             'compose_files': list(map(str, files)), 'configuration_sha256': {str(p): checksum(p) for p in [env_file, *files]},
             'services': previous, 'database': database, 'volume': volume_identity(name), 'protected_tags': []}
    _, state['resolved_configuration_sha256'] = resolved_config(compose_args(state))
    # All preflight reads must succeed before writing artifacts or adding tags.
    directory.mkdir(mode=0o700)
    token = uuid.uuid4().hex
    for service, saved in previous.items():
        require(image_id(saved['image']) == saved['image'], 'Previous image is unavailable locally')
        tag = f'crisisguard-rollback:{token}-{service}'
        run(['docker', 'image', 'tag', saved['image'], tag])
        state['protected_tags'].append(tag)
    write_json(directory / 'rollback.compose.json', {'services': {
        service: {'image': saved['image'], 'pull_policy': 'never'} for service, saved in previous.items()}})
    state['rollback_sha256'] = checksum(directory / 'rollback.compose.json')
    write_json(directory / 'checkpoint.json', state)
    return state


def verify(directory, *, expected=None):
    require(expected in (None, 'candidate', 'previous'), 'Invalid release target')
    directory = Path(directory).resolve()
    state = json.loads((directory / 'checkpoint.json').read_text(encoding='utf-8'))
    require(state.get('version') == 1 and state.get('role') in ('cloud', 'hospital'), 'Unsupported checkpoint')
    require(all(checksum(path) == digest for path, digest in state['configuration_sha256'].items()),
            'Configuration/env file changed; do not activate or rollback with this checkpoint')
    require(checksum(directory / 'rollback.compose.json') == state['rollback_sha256'], 'Rollback override changed')
    rollback = json.loads((directory / 'rollback.compose.json').read_text(encoding='utf-8'))
    require(rollback == {'services': {service: {'image': saved['image'], 'pull_policy': 'never'}
                                    for service, saved in state['services'].items()}}, 'Unexpected rollback override')
    allowed = {name: {saved['image']} for name, saved in state['services'].items()}
    candidate = directory / 'candidate.json'
    candidate_image = None
    if candidate.exists():
        metadata = json.loads(candidate.read_text(encoding='utf-8'))
        candidate_image = metadata['image']
        require(bool(IMAGE_ID.fullmatch(metadata['image'])), 'Invalid candidate image ID')
        require(checksum(directory / 'candidate.compose.json') == metadata['override_sha256'], 'Candidate override changed')
        expected_override = {'services': {name: {'image': metadata['image'], 'pull_policy': 'never'} for name in allowed}}
        require(json.loads((directory / 'candidate.compose.json').read_text(encoding='utf-8')) == expected_override,
                'Candidate override must change application images only')
        for images in allowed.values():
            images.add(metadata['image'])
    compose = compose_args(state)
    config, digest = resolved_config(compose)
    require(digest == state['resolved_configuration_sha256'], 'Resolved configuration changed (including shell overrides)')
    for service, saved in state['services'].items():
        require(image_id(saved['image']) == saved['image'], 'Previous image has been removed; rollback cannot proceed')
        current = inspect_service(compose, service, state['project'],
                                  environment=config['services'][service].get('environment', {}))
        require(current['mounts'] == saved['mounts'], 'Application volume/bind mounts changed')
        require(current['image'] in allowed[service], 'Unrecorded application image is running')
        if expected:
            target = candidate_image if expected == 'candidate' else saved['image']
            require(target is not None and current['image'] == target, 'Running image is not the requested release target')
    if state['database']:
        current = inspect_service(compose, 'postgres', state['project'], healthy=True,
                                  environment=config['services']['postgres'].get('environment', {}))
        require(current == state['database'], 'PostgreSQL container/image/mounts changed; separate review required')
    require(volume_identity(state['volume']['Name']) == state['volume'], 'Named data volume identity changed')
    return state


def candidate(directory, reference, revision, compatibility_note):
    require(bool(compatibility_note.strip()), 'Document the reviewed schema/API/event compatibility before staging')
    require(bool(re.fullmatch(r'[a-fA-F0-9]{40,64}', revision)), 'Use the reviewed candidate source revision')
    directory = Path(directory).resolve()
    require(not (directory / 'candidate.json').exists() and not (directory / 'candidate.compose.json').exists(),
            'Candidate already staged; use a new checkpoint for another attempt')
    state = verify(directory)
    image = image_id(reference)
    tag = 'crisisguard-candidate:' + uuid.uuid4().hex
    run(['docker', 'image', 'tag', image, tag])
    write_json(directory / 'candidate.compose.json', {'services': {
        name: {'image': image, 'pull_policy': 'never'} for name in state['services']}})
    metadata = {'image': image, 'source_revision': revision, 'protected_tag': tag,
                'compatibility': 'operator-reviewed declaration; not an automatic schema check',
                'compatibility_note': compatibility_note,
                'override_sha256': checksum(directory / 'candidate.compose.json')}
    write_json(directory / 'candidate.json', metadata)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('role', choices=['cloud', 'hospital'])
    prep.add_argument('--env-file', type=Path, required=True)
    prep.add_argument('--compose-file', type=Path, action='append', required=True)
    prep.add_argument('--project-name')
    prep.add_argument('--hospital-service', default='hospital')
    prep.add_argument('--revision', default='unknown')
    for name in ('prepare', 'candidate', 'check'):
        command = prep if name == 'prepare' else commands.add_parser(name)
        command.add_argument('--directory', type=Path, required=True)
        if name == 'candidate':
            command.add_argument('--image', required=True)
            command.add_argument('--revision', required=True)
            command.add_argument('--compatibility-note', required=True)
        elif name == 'check':
            command.add_argument('--expect', choices=['candidate', 'previous'], help='Also require this exact active image')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            prepare(args.role, args.env_file, args.compose_file, args.directory, project=args.project_name,
                    hospital_service=args.hospital_service, revision=args.revision)
        elif args.action == 'candidate':
            candidate(args.directory, args.image, args.revision, args.compatibility_note)
        else:
            verify(args.directory, expected=args.expect)
        print(json.dumps({'result': 'passed', 'action': args.action, 'containers_changed': False,
                          'scope': 'Artifact preparation or identity check only; no database restore or activation'}))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
        print(json.dumps({'result': 'failed', 'action': args.action,
                          'error': 'Checkpoint precondition failed; inspect private artifacts/configuration. No activation performed.'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
