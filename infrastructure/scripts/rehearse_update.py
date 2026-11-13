"""Rehearse image-only updates/rollback in fresh local Docker projects.

Uses synthetic data, verifies backups in isolated targets, and removes only this
run's resources. No AWS access. Candidate images use the SAME application code:
this checks release mechanics, not arbitrary schema/migration compatibility.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

if __package__:
    from . import database_backup as backup, release_checkpoint as release, smoke_deployment as smoke
else:
    import database_backup as backup
    import release_checkpoint as release
    import smoke_deployment as smoke

ROOT = Path(__file__).resolve().parents[2]
SQLITE_FINGERPRINT = '''import hashlib,os,sqlite3
from pathlib import Path
with sqlite3.connect(Path(os.environ['HOSPITAL_DB_PATH']).resolve().as_uri()+'?mode=ro',uri=True) as conn:
    print(hashlib.sha256('\\n'.join(conn.iterdump()).encode()).hexdigest())
'''


def sqlite_fingerprint(project):
    return project.call('exec', '-T', 'hospital', 'python', '-c', SQLITE_FINGERPRINT, capture=True)


def base_files(project):
    return [Path(project.args[index + 1]) for index, item in enumerate(project.args) if item == '-f']


def settings_file(project):
    return Path(project.args[project.args.index('--env-file') + 1])


def activate(project, directory, filename, services, *, expect_failure=False):
    args = project.args + ['-f', str(directory / filename), 'up', '-d', '--no-build', '--no-deps',
                           '--force-recreate', '--wait', '--wait-timeout', '12' if expect_failure else '120', *services]
    result = subprocess.run(args, cwd=ROOT, env=project.env, text=True,
                            capture_output=True, timeout=180)
    smoke.check((result.returncode != 0) if expect_failure else (result.returncode == 0),
                'Unexpected application activation/health-gate result; private command output suppressed')


def capture(hospitals, run_id, phase):
    events = []
    for _, _, port, site in hospitals:
        event = {'event_id': f'{run_id}_{phase}_{site}', 'site_id': site,
                 'event_type': 'patient_registration', 'base_priority': 1, 'prerequisite_ids': [],
                 'payload': {'patient_id': f'SYNTHETIC_{phase}_{site}'}}
        smoke.request(port, '/capture', event, 201)
        events.append(event)
    return events


def settle(hospitals):
    for _, _, port, _ in hospitals:
        deadline = time.monotonic() + 60
        while True:
            smoke.wait_for_recovery(port, True, 0)
            if all(row['status'] == 'applied' for row in smoke.request(port, '/events')):
                break
            smoke.check(time.monotonic() < deadline, 'Timed out waiting for captured events to apply')
            time.sleep(0.5)


def backup_and_restore(project, role, directory, event_ids):
    path = directory / ('cloud.dump' if role == 'cloud' else project.name + '.sqlite3')
    saved = backup.backup('postgres' if role == 'cloud' else 'sqlite', project.args, path, env=project.env)
    backup.verify_checksum(path, saved['sha256'])
    restored = backup.restore_postgres(path, event_ids) if role == 'cloud' else backup.restore_sqlite(path, event_ids)
    return {'kind': saved['kind'], 'bytes': saved['bytes'], 'sha256': saved['sha256'],
            'restore_expected_events_found': restored['expected_events_found']}


def rehearse(directory, run_id, report):
    projects, built_images, tags = [], [], []
    peers = smoke.PeerNetwork(run_id + '-peers')
    base = f'crisisguard-update-test:{run_id}-base'
    candidate_image = f'crisisguard-update-test:{run_id}-candidate'
    broken_image = f'crisisguard-update-test:{run_id}-broken'
    try:
        smoke.run(['docker', 'build', '--label', 'crisisguard.update-rehearsal=' + run_id,
                   '-f', str(ROOT / 'infrastructure/docker/Dockerfile'), '-t', base, str(ROOT)])
        built_images.append(base)
        image_dir = directory / 'images'
        image_dir.mkdir()
        (image_dir / 'Dockerfile').write_text(f'FROM {base}\nLABEL crisisguard.release=candidate\n')
        smoke.run(['docker', 'build', '-t', candidate_image, str(image_dir)])
        built_images.append(candidate_image)
        (image_dir / 'Dockerfile').write_text(f'FROM {candidate_image}\nENTRYPOINT ["python","-c","raise SystemExit(1)"]\n')
        smoke.run(['docker', 'build', '-t', broken_image, str(image_dir)])
        built_images.append(broken_image)
        report['images'] = {name: release.image_id(ref) for name, ref in
                            [('baseline', base), ('candidate', candidate_image), ('broken', broken_image)]}
        smoke.check(len(set(report['images'].values())) == 3, 'Rehearsal requires distinct image IDs')
        a_port, b_port, patient, emergency, admin = smoke.free_ports(5)
        peers.create()
        common = {'POSTGRES_USER': 'crisisguard', 'POSTGRES_PASSWORD': 'release_test_only',
                  'POSTGRES_DB': 'crisisguard', 'PATIENT_PORT': patient, 'EMERGENCY_PORT': emergency,
                  'ADMIN_PORT': admin, 'CLOUD_PROJECT': run_id + '-cloud',
                  'CLOUD_BIND_ADDRESS': '127.0.0.1'}
        cloud = smoke.Project(directory, common['CLOUD_PROJECT'], smoke.DOCKER / 'compose.cloud.yaml',
                              common, base, ['patient', 'emergency', 'admin'], peer_network=peers.name)
        projects.append(cloud)
        hospitals = []
        for suffix, port, site in [('a', a_port, 'Hospital_A'), ('b', b_port, 'Hospital_B')]:
            settings = {'HOSPITAL_PROJECT': run_id + '-' + suffix, 'HOSPITAL_PORT': port,
                        'HOSPITAL_BIND_ADDRESS': '127.0.0.1', 'SITE_ID': site, 'AUTO_REPLAY': '1',
                        'AUTO_REPLAY_INTERVAL_SECONDS': '1',
                        **{key: f'http://{service}:8000/events' for key, service in
                           [('CLOUD_URL', 'patient'), ('EMERGENCY_URL', 'emergency'), ('ADMIN_URL', 'admin')]}}
            project = smoke.Project(directory, settings['HOSPITAL_PROJECT'], smoke.DOCKER / 'compose.hospital.yaml',
                                    settings, base, ['hospital'], peer_network=peers.name,
                                    peer_aliases={'hospital': 'hospital-' + suffix})
            projects.append(project)
            hospitals.append((project, 'hospital', port, site))
        # Include the actual AWS restart/logging policies in this local rehearsal.
        for index, project in enumerate(projects):
            override = ROOT / f'infrastructure/aws/compose.{"cloud" if index == 0 else "hospital"}.override.yaml'
            project.args += ['-f', str(override)]
            project.checker_args += ['--compose-file', str(override)]
            project.start()
        baseline = capture(hospitals, run_id, 'baseline')
        settle(hospitals)
        smoke.verify_health_checker(cloud, hospitals, baseline, directory, automatic=True)
        report['backups'] = [backup_and_restore(project, 'cloud' if index == 0 else 'hospital', directory,
                             [event['event_id'] for event in baseline if index == 0 or event['site_id'] == hospitals[index - 1][3]])
                             for index, project in enumerate(projects)]
        revision = smoke.run(['git', 'rev-parse', 'HEAD'], capture=True)
        checkpoints = []
        for index, project in enumerate(projects):
            path = directory / (project.name + '-checkpoint')
            state = release.prepare('cloud' if index == 0 else 'hospital', settings_file(project), base_files(project),
                                    path, project=project.name, revision=revision)
            tags.extend(state['protected_tags'])
            staged = release.candidate(path, candidate_image, revision,
                                       'Same application code; image-label-only candidate, no schema/API/event changes')
            tags.append(staged['protected_tag'])
            checkpoints.append(path)
        # A changed private env must be rejected before activation.
        env = settings_file(cloud)
        old_bytes = env.read_bytes()
        try:
            env.write_bytes(old_bytes + b'\n# changed after checkpoint\n')
            rejected = False
            try:
                release.verify(checkpoints[0])
            except RuntimeError:
                rejected = True
            smoke.check(rejected, 'Configuration-drift guard did not reject an edit')
        finally:
            env.write_bytes(old_bytes)
        report['configuration_drift_rejected'] = True
        for index, project in enumerate(projects):
            activate(project, checkpoints[index], 'candidate.compose.json',
                     ['patient', 'emergency', 'admin'] if index == 0 else ['hospital'])
            release.verify(checkpoints[index], expected='candidate')
        newer = capture(hospitals, run_id, 'after_update')
        events = baseline + newer
        settle(hospitals)
        smoke.verify_health_checker(cloud, hospitals, events, directory, automatic=True)
        print('PASS release update: compatible candidate healthy; pre-update records retained and new records applied', flush=True)
        cloud.call('stop', 'patient', 'emergency', 'admin')
        for _, _, port, _ in hospitals:
            smoke.wait_for_recovery(port, False, 0)
        pending = capture(hospitals, run_id, 'pending_at_rollback')
        events += pending
        for index, (project, _, port, _) in enumerate(hospitals, 1):
            before = sqlite_fingerprint(project)
            activate(project, checkpoints[index], 'rollback.compose.json', ['hospital'])
            release.verify(checkpoints[index], expected='previous')
            smoke.check(sqlite_fingerprint(project) == before, 'Hospital events/recovery state changed during offline rollback')
            smoke.check(sum(r['status'] == 'pending' for r in smoke.request(port, '/events')) == 1,
                        'Pending event lost during hospital rollback')
        activate(cloud, checkpoints[0], 'rollback.compose.json', ['patient', 'emergency', 'admin'])
        for path in checkpoints:
            release.verify(path, expected='previous')
        settle(hospitals)
        smoke.verify_health_checker(cloud, hospitals, events, directory, automatic=True)
        smoke.check(len(smoke.cloud_records(cloud)) == 6, 'Expected six retained/applied records after rollback')
        report['pending_sqlite_and_recovery_state_preserved'] = True
        report['records_after_compatible_rollback'] = 6
        print('PASS release rollback: prior image restored; baseline, post-update and pending records preserved', flush=True)
        # New failed attempt gets a fresh backup/checkpoint, not a reused candidate.
        a = hospitals[0][0]
        report['before_failed_attempt_backup'] = backup_and_restore(a, 'hospital', directory / 'failed-attempt-backup',
                                     [e['event_id'] for e in events if e['site_id'] == 'Hospital_A'])
        failed = directory / 'failed-checkpoint'
        saved = release.prepare('hospital', settings_file(a), base_files(a), failed, project=a.name, revision=revision)
        tags.extend(saved['protected_tags'])
        staged = release.candidate(failed, broken_image, revision,
                                   'Same source/schema; injected startup exit before any application code')
        tags.append(staged['protected_tag'])
        before_sqlite, before_cloud = sqlite_fingerprint(a), smoke.cloud_records(cloud)
        activate(a, failed, 'candidate.compose.json', ['hospital'], expect_failure=True)
        release.verify(failed, expected='candidate')
        result = subprocess.run([sys.executable, str(ROOT / 'infrastructure/scripts/verify_aws_recovery.py'),
                                 'hospital', *a.checker_args, '--require-drained'], cwd=ROOT,
                                env=a.env, capture_output=True, text=True, timeout=120)
        smoke.check(result.returncode != 0 and json.loads(result.stdout)['result'] == 'failed',
                    'Broken application was not rejected by the health checker')
        activate(a, failed, 'rollback.compose.json', ['hospital'])
        release.verify(failed, expected='previous')
        settle(hospitals)
        smoke.verify_health_checker(cloud, hospitals, events, directory, automatic=True)
        smoke.check(sqlite_fingerprint(a) == before_sqlite and smoke.cloud_records(cloud) == before_cloud,
                    'Failed-release backout changed database contents')
        report['failed_candidate_rejected_and_backed_out'] = True
        report['postgres_identity_preserved'] = True
        report['final_cloud_records'] = 6
        report['final_records_per_hospital'] = 3
        report['exact_target_images_verified'] = True
        print('PASS rejected release: health gate failed; prior app restored with databases unchanged', flush=True)
    finally:
        cleanup_errors = []
        for project in reversed(projects):
            try:
                project.call('down', '--volumes', '--remove-orphans')
            except subprocess.SubprocessError:
                cleanup_errors.append(project.name)
        try:
            peers.remove()
        except subprocess.SubprocessError:
            cleanup_errors.append('peer-network')
        for ref in reversed(tags + built_images):
            result = subprocess.run(['docker', 'image', 'rm', ref], cwd=ROOT, capture_output=True)
            if result.returncode:
                cleanup_errors.append('image')
        report['cleanup'] = 'passed' if not cleanup_errors else 'failed'
        smoke.check(not cleanup_errors, 'Rehearsal cleanup incomplete; inspect this run\'s generated resources')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='New sanitized JSON report; never overwritten')
    args = parser.parse_args()
    output = backup.private_file(args.report) if args.report else None
    run_id = 'cg-update-' + uuid.uuid4().hex[:12]
    report = {'run_id': run_id, 'scope': 'Local image-only maintenance rehearsal; same application schema/source',
              'aws_used': False, 'live_database_restoration': False}
    print('RUN ' + run_id, flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix=run_id + '-') as temp:
            # Backup helper requires the parent to exist.
            directory = Path(temp)
            (directory / 'failed-attempt-backup').mkdir()
            rehearse(directory, run_id, report)
        report['result'] = 'passed'
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        report.update(result='failed', error=type(error).__name__ + '; inspect local rehearsal output')
    encoded = (json.dumps(report, indent=2) + '\n').encode()
    if output:
        with output:
            output.write(encoded)
    print(encoded.decode(), end='')
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
