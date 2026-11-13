"""Verify local MetaGuard in a fresh eight-service Docker Compose project.

Requires requirements.txt and Docker Engine/Compose. Never uses existing project
resources. Removes only this run's containers/network and labelled new volumes;
retains its uniquely tagged image. --integrated-replay enables Checkpoint 4's
authenticated hospital grant/reconciliation scenarios. No AWS access.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

import httpx
import psycopg

if __package__:
    from . import smoke_deployment as smoke
else:
    import smoke_deployment as smoke

ROOT = Path(__file__).resolve().parents[2]


def wait_for(check, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, psycopg.OperationalError):
            pass
        time.sleep(.5)
    raise RuntimeError('Timed out waiting for MetaGuard verification condition')


def verify(report_path=None, *, extended_regressions=False, integrated_replay=False, adaptive_admission=False, reliability=False, regression_only=False, keep_data=False):
    adaptive_admission = adaptive_admission or reliability
    integrated_replay = integrated_replay or adaptive_admission or regression_only
    project = 'cg-metaguard-' + uuid.uuid4().hex[:12]
    parent = ROOT / '.venv' if (ROOT / '.venv').is_dir() else Path(tempfile.gettempdir())
    directory = parent / project
    directory.mkdir()  # Do not reuse a previous run's directory or resources.
    env_file, compose_file = directory / 'test.env', directory / 'compose.json'
    log_path = directory / 'docker.log'
    settings = dict(zip(
        ('POSTGRES_PORT', 'CONTROLLER_PORT', 'PATIENT_PORT', 'EMERGENCY_PORT',
         'ADMIN_PORT', 'HOSPITAL_A_PORT', 'HOSPITAL_B_PORT', 'DASHBOARD_PORT'),
        map(str, smoke.free_ports(8))))
    settings.update(POSTGRES_USER='cp3_verify', POSTGRES_DB='cp3_verify',
                    POSTGRES_PASSWORD=secrets.token_hex(24),
                    CONTROLLER_CLOUD_TOKEN=secrets.token_hex(32),
                    AUTO_REPLAY='1', AUTO_REPLAY_INTERVAL_SECONDS='5',
                    ENABLE_MANUAL_REPLAY='0', MANUAL_REPLAY_TOKEN='')
    if integrated_replay:
        settings.update(HOSPITAL_A_CONTROLLER_TOKEN=secrets.token_hex(32), HOSPITAL_B_CONTROLLER_TOKEN=secrets.token_hex(32))
    settings['CONTROLLER_ADMISSION_ENABLED'] = '1' if adaptive_admission else '0'
    if adaptive_admission:
        settings.update(CONTROLLER_ADMISSION_POLL_SECONDS='1', CONTROLLER_ADMISSION_MAX_AGE_SECONDS='5',
            CONTROLLER_ADMISSION_TIMEOUT_SECONDS='1', CONTROLLER_ADMISSION_DWELL_SECONDS='3',
            CONTROLLER_ADMISSION_HEALTHY_SAMPLES='2', CONTROLLER_ADMISSION_CAUTION_LATENCY_MS='80',
            CONTROLLER_ADMISSION_HEALTHY_LATENCY_MS='50', CONTROLLER_ADMISSION_RECOVERY_ERROR_RATE='0.15')
    if reliability:
        settings['AUTO_REPLAY_INTERVAL_SECONDS'] = '0.5'
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith('COMPOSE_'):
            environment.pop(key)
    environment.update(settings)
    env_file.write_text(''.join(f'{key}={value}\n' for key, value in settings.items()), encoding='utf-8')
    image = 'crisisguard-metaguard-test:' + project
    report = {'project': project, 'image': image, 'started_at': datetime.now(timezone.utc).isoformat(),
              'checks': [], 'ports': {key: int(settings[key]) for key in settings if key.endswith('_PORT')},
              'artifacts': str(directory), 'aws_used': False}

    def run(args, *, capture=False, env=None, timeout=600, allow_failure=False):
        result = subprocess.run(args, cwd=ROOT, env=env or environment, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if not capture:
            with log_path.open('a', encoding='utf-8') as log:
                log.write(result.stdout + result.stderr)
        if result.returncode and (not allow_failure or result.returncode != 1):
            # Config output includes synthetic secrets: never print it on error.
            raise RuntimeError(f'Command failed ({result.returncode}): {args[0:2]}; see {log_path}')
        return result.stdout.strip()

    def docker(*args):
        return run(['docker', *args], capture=True)

    base = ['docker', 'compose', '--env-file', str(env_file), '-p', project]
    compose = [*base, '-f', str(compose_file), '--profile', 'metaguard']

    def call(*args):
        return run([*compose, *args])

    def passed(name):
        report['checks'].append({'name': name, 'result': 'passed'})
        print('PASS ' + name, flush=True)

    def containers():
        ids = docker('ps', '-aq').splitlines()
        rows = json.loads(docker('inspect', *ids)) if ids else []
        stable_state = ('Status', 'Running', 'Paused', 'Restarting', 'OOMKilled',
                        'Dead', 'Pid', 'ExitCode', 'StartedAt', 'FinishedAt')
        # Running containers naturally append health logs; compare lifecycle state.
        return {row['Id']: {'name': row['Name'],
                            'state': {key: row['State'][key] for key in stable_state},
                            'restarts': row['RestartCount'], 'mounts': row['Mounts']} for row in rows}

    original_containers = containers()
    original_volumes = set(docker('volume', 'ls', '-q').splitlines())
    original_images = set(docker('image', 'ls', '--no-trunc', '--format', '{{.Repository}}:{{.Tag}} {{.ID}}').splitlines())
    assert not docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project)
    assert not docker('volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project)
    assert not docker('network', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project)
    assert not docker('image', 'ls', '-q', image)
    owns_project = False
    try:
        files = ['-f', str(ROOT / 'compose.yaml'), '-f', str(ROOT / 'compose.override.yaml')]
        if integrated_replay:
            files += ['-f', str(ROOT / 'infrastructure/docker/compose.metaguard.yaml')]
        resolved = json.loads(run([*base, *files, '--profile', 'metaguard', 'config', '--format', 'json'], capture=True))
        assert resolved['name'] == project
        services = resolved['services']
        assert set(services) == {'postgres', 'controller', 'patient', 'emergency', 'admin',
                                 'hospital-a', 'hospital-b', 'dashboard'}
        assert len(services['postgres']['ports']) == 1
        assert str(services['postgres']['ports'][0]['published']) == settings['POSTGRES_PORT']
        assert services['controller']['depends_on']['postgres']['condition'] == 'service_healthy'
        assert services['controller']['environment']['CONTROLLER_CLOUD_TOKEN'] == settings['CONTROLLER_CLOUD_TOKEN']
        for name, service in services.items():
            assert all(port['host_ip'] == '127.0.0.1' for port in service.get('ports', []))
            if name != 'postgres':
                service['image'] = image
                service['pull_policy'] = 'never'
                if name != 'controller':
                    service.pop('build', None)
        for site in ('hospital-a', 'hospital-b'):
            assert services[site]['environment']['AUTO_REPLAY'] == '1'
            assert ('CONTROLLER_URL' in services[site]['environment']) == integrated_replay
        if reliability:
            proxy_name = project + '-fault-proxy'
            proxy_port = str(smoke.free_ports(1)[0])
            settings['FAULT_PROXY_PORT'] = proxy_port
            report['ports']['FAULT_PROXY_PORT'] = int(proxy_port)
            source = (ROOT / 'infrastructure/scripts/metaguard_fault_proxy.py').read_text(encoding='utf-8')
            services['fault-proxy'] = {'image': image, 'pull_policy': 'never',
                'command': ['python', '-c', source], 'container_name': proxy_name,
                'ports': [{'target': 8000, 'published': proxy_port, 'host_ip': '127.0.0.1', 'protocol': 'tcp'}],
                'networks': {'default': None}, 'healthcheck': {'test': ['CMD', 'python', '-c',
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"],
                    'interval': '2s', 'timeout': '3s', 'retries': 15}}
            for site in ('hospital-a', 'hospital-b'):
                services[site]['environment']['CONTROLLER_URL'] = 'http://fault-proxy:8000/controller'
                services[site]['environment']['CLOUD_URL'] = 'http://fault-proxy:8000/patient/events'
        for volume in resolved['volumes'].values():
            assert not volume.get('external') and volume['name'] not in original_volumes
            assert volume['name'].startswith(project + '_')
        for network in resolved['networks'].values():
            assert not network.get('external') and network['name'].startswith(project + '_')
        compose_file.write_text(json.dumps({key: value for key, value in resolved.items()
                                            if not key.startswith('x-')}), encoding='utf-8')
        call('config', '--quiet')
        passed('Compose rendering, isolated ports/resources and selected hospital replay mode')
        owns_project = True
        call('build', 'controller')
        call('up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180')
        ids = docker('ps', '-q', '--filter', 'label=com.docker.compose.project=' + project).splitlines()
        rows = json.loads(docker('inspect', *ids))
        assert len(rows) == (9 if reliability else 8) and all(row['State']['Health']['Status'] == 'healthy' for row in rows)
        report['containers'] = {row['Config']['Labels']['com.docker.compose.service']: row['Id'] for row in rows}
        passed(('Nine real containers including test-only fault proxy' if reliability else 'All eight real containers') + ' healthy using the existing Dockerfile')

        def api(method, path, body=None, status=200, *, trusted=True):
            headers = {'X-Cloud-Token': settings['CONTROLLER_CLOUD_TOKEN']} if trusted else {}
            response = httpx.request(method, f"http://127.0.0.1:{settings['CONTROLLER_PORT']}{path}",
                                     json=body, headers=headers, timeout=10)
            assert response.status_code == status, (path, response.status_code, response.text)
            return response.json()

        db_url = (f"postgresql://{settings['POSTGRES_USER']}:{settings['POSTGRES_PASSWORD']}"
                  f"@127.0.0.1:{settings['POSTGRES_PORT']}/{settings['POSTGRES_DB']}")

        def query(sql, args=()):
            with psycopg.connect(db_url, connect_timeout=3) as conn:
                cursor = conn.execute(sql, args)
                return cursor.fetchall() if cursor.description else []

        def ticket(hospital, event):
            return api('POST', '/tickets', {'hospital_id': hospital, 'event_id': event, 'priority': 2})

        if regression_only:
            passed('Fresh PostgreSQL and core services ready for full applicable regression suite')
        elif reliability:
            if __package__:
                from .metaguard_reliability_checks import verify_reliability
            else:
                from metaguard_reliability_checks import verify_reliability
            report['reliability_measurements'] = verify_reliability(call, api, query, settings, project, passed)
        elif adaptive_admission:
            if __package__:
                from .metaguard_admission_checks import verify_admission
            else:
                from metaguard_admission_checks import verify_admission
            report['admission_measurements'] = verify_admission(call, api, query, settings, project, passed)
        elif integrated_replay:
            if __package__:
                from .metaguard_replay_checks import verify_replay
            else:
                from metaguard_replay_checks import verify_replay
            verify_replay(call, api, query, settings, project, passed)
        else:
            assert api('GET', '/health') == {'status': 'healthy'}
            assert query('SELECT current_database()') == [('cp3_verify',)]
            assert query("SELECT count(*) FROM controller_grants") == [(0,)]
            report['postgres_version'] = query('SHOW server_version')[0][0]
            passed('Controller HTTP liveness and real PostgreSQL connectivity/schema')

            assert ticket('A', 'COMPLETE')['added']
            assert not ticket('A', 'COMPLETE')['added']
            grant = api('POST', '/grants/next')
            gid = grant['grant_id']
            assert api('GET', '/grants/A/COMPLETE')['grant_id'] == gid
            assert not ticket('A', 'COMPLETE')['added']
            api('POST', f'/grants/{gid}/complete', {'hospital_id': 'A'}, status=503, trusted=False)
            api('POST', '/grants/reconcile-expired', status=503, trusted=False)
            for _ in range(2):
                assert api('POST', f'/grants/{gid}/acknowledge', {'hospital_id': 'A'}) == {'status': 'acknowledged'}
            for _ in range(2):
                assert api('POST', f'/grants/{gid}/complete', {'hospital_id': 'A'}, trusted=True) == {'status': 'completed'}
            assert api('GET', f'/grants/by-id/{gid}')['status'] == 'completed'
            assert not ticket('A', 'COMPLETE')['added']
            assert query('SELECT new_status FROM controller_grant_audit WHERE grant_id=%s ORDER BY id', (gid,)) == [('issued',), ('acknowledged',), ('completed',)]
            passed('Ticket/grant persistence, duplicate requests, ACK/completion idempotency and Cloud token gate')

            assert ticket('B', 'RESTART')['added']
            restart_grant = api('POST', '/grants/next')
            assert ticket('C', 'WAITING')['added']
            call('restart', 'controller')
            wait_for(lambda: api('GET', '/health') == {'status': 'healthy'})
            assert api('GET', '/grants/B/RESTART')['grant_id'] == restart_grant['grant_id']
            assert api('GET', '/tickets')[0]['event_id'] == 'WAITING'
            assert api('GET', f'/grants/by-id/{gid}')['status'] == 'completed'
            # Recreate as well: a restart alone retains a container writable layer.
            call('up', '-d', '--no-build', '--force-recreate', '--no-deps', '--wait', 'controller')
            assert api('GET', '/grants/B/RESTART')['grant_id'] == restart_grant['grant_id']
            assert api('GET', '/tickets')[0]['event_id'] == 'WAITING'
            assert api('POST', '/grants/next')['event_id'] == 'WAITING'
            passed('Controller container restart AND recreation preserve grants, history and waiting ticket')

            old = restart_grant['grant_id']
            query("UPDATE controller_grants SET expires_at=NOW()-INTERVAL '1 second' WHERE grant_id=%s", (old,))
            retried = api('POST', '/grants/next')
            assert retried['event_id'] == 'RESTART' and retried['grant_id'] != old
            assert api('GET', '/grants/by-id/' + old)['status'] == 'expired'
            api('POST', '/grants/' + retried['grant_id'] + '/acknowledge', {'hospital_id': 'B'})
            query("UPDATE controller_grants SET expires_at=NOW()-INTERVAL '1 second' WHERE grant_id=%s", (retried['grant_id'],))
            assert api('POST', '/grants/reconcile-expired', trusted=True)['needs_reconciliation'] == 1
            assert api('GET', '/tickets') == []
            assert api('GET', '/grants/by-id/' + retried['grant_id'])['status'] == 'reconciliation_required'
            passed('Unacknowledged expiry reissues safely; acknowledged expiry requires authoritative reconciliation')

            call('stop', 'postgres')
            api('GET', '/tickets', status=503)
            controller_id = docker('ps', '-q', '--filter', 'label=com.docker.compose.project=' + project,
                                   '--filter', 'label=com.docker.compose.service=controller')
            wait_for(lambda: json.loads(docker('inspect', controller_id))[0]['State']['Health']['Status'] == 'unhealthy')
            assert api('GET', '/health') == {'status': 'healthy'}
            call('start', 'postgres')
            wait_for(lambda: httpx.get(f"http://127.0.0.1:{settings['CONTROLLER_PORT']}/tickets", timeout=5).status_code == 200)
            assert api('GET', '/tickets') == []
            wait_for(lambda: json.loads(docker('inspect', controller_id))[0]['State']['Health']['Status'] == 'healthy')
            assert api('GET', '/grants/by-id/' + gid)['status'] == 'completed'
            passed('Database outage returns 503, container readiness fails, and same controller recovers')

            # Existing hospital replay is exercised as a compatibility regression only.
            call('stop', 'patient', 'emergency', 'admin')
            hospital_ports = [int(settings[key]) for key in ('HOSPITAL_A_PORT', 'HOSPITAL_B_PORT')]
            chains = [smoke.event_chain(project, 'metaguard', site) for site in ('Hospital_A', 'Hospital_B')]
            for port, chain in zip(hospital_ports, chains):
                smoke.wait_for_recovery(port, False, 0)
                for event in reversed(chain):
                    smoke.request(port, '/capture', event, expected=201)
                smoke.check_records(smoke.request(port, '/events'), chain, 'pending')
            smoke.verify_dashboard(int(settings['DASHBOARD_PORT']), chains, 'pending', reachable=False, assets=True)
            passed('Both hospitals capture offline; dashboard shows eight pending events during Cloud API outage')
            call('up', '-d', '--no-build', '--wait', '--wait-timeout', '120', 'patient', 'emergency', 'admin')
            for port, chain in zip(hospital_ports, chains):
                smoke.wait_for_recovery(port, True, 0, drained=True)
                smoke.check_records(smoke.request(port, '/events'), chain, 'applied')
            smoke.verify_dashboard(int(settings['DASHBOARD_PORT']), chains, 'applied', event_traffic=True)
            with psycopg.connect(db_url, row_factory=psycopg.rows.dict_row) as conn:
                events = conn.execute('SELECT * FROM events ORDER BY event_id').fetchall()
            smoke.check_records(events, [event for chain in chains for event in chain], 'applied')
            # Existing smoke helpers consume JSON timestamps; psycopg returns datetime.
            for event in events:
                event['applied_at'] = event['applied_at'].isoformat()
            smoke.check_application_order(events)
            passed('Unchanged automatic replay delivers eight events to all three Cloud APIs with correct order and dashboard history')

        pytest_env = dict(environment, CONTROLLER_TEST_DATABASE_URL=db_url, DATABASE_URL=db_url)
        for key in tuple(pytest_env):
            if key.startswith('CONTROLLER_ADMISSION_'):
                pytest_env.pop(key)
        for key in ('METAGUARD_ENABLED', 'METAGUARD_REQUIRED', 'CONTROLLER_HOSPITAL_TOKENS'):
            pytest_env.pop(key, None)
        result_file = directory / 'pytest.xml'
        paths = ['tests/unit', 'tests/integration/test_controller_postgres.py',
                 'tests/integration/test_cloud_apis.py', 'tests/integration/test_cloud_telemetry.py']
        if reliability or regression_only:
            paths = ['tests/unit', 'tests/integration']
            pytest_env['RUN_DASHBOARD_DOCKER'] = '1'
        if (extended_regressions or integrated_replay) and not (reliability or regression_only):
            paths.append('tests/integration/test_manual_replay_security.py')
        report['pytest_paths'] = paths
        output = run([sys.executable, '-m', 'pytest', *paths, '-q', '-p', 'no:cacheprovider',
                      '--basetemp=' + str(directory / 'pytest-temp'), '--junitxml=' + str(result_file)],
                     env=pytest_env, allow_failure=True)
        (directory / 'pytest.log').write_text(output, encoding='utf-8')
        suites = ET.parse(result_file).getroot().findall('testsuite')
        counts = {name: sum(int(suite.attrib.get(name, 0)) for suite in suites)
                  for name in ('tests', 'failures', 'errors', 'skipped')}
        counts['passed'] = counts['tests'] - counts['failures'] - counts['errors'] - counts['skipped']
        report['pytest'] = counts
        report['docker_result'] = 'passed'
        if counts['failures'] or counts['errors']:
            raise RuntimeError(f"Regression suite: {counts['passed']} passed, {counts['failures']} failed, {counts['errors']} errors")
        passed(f"Repository unit and controller/Cloud regression tests: {counts['passed']} passed")
        report['result'] = 'passed'
    except Exception as exc:
        report['result'] = 'failed'
        report['error'] = str(exc)
        raise
    finally:
        try:
            if owns_project:
                run([*compose, 'logs', '--no-color'])
                volumes = docker('volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project).splitlines()
                assert not (set(volumes) & original_volumes)
                # No down -v: validate ownership and remove exact newly created volumes.
                call('down', '--remove-orphans')
                for volume in ([] if keep_data else volumes):
                    metadata = json.loads(docker('volume', 'inspect', volume))[0]
                    assert metadata['Labels']['com.docker.compose.project'] == project
                    docker('volume', 'rm', volume)
                assert not docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project)
                assert not docker('network', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project)
                if keep_data:
                    report['retained_test_volumes'] = volumes
                    report['cleanup'] = 'passed; disposable containers/network removed; test volumes and image retained'
                else:
                    assert not docker('volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project)
                    report['cleanup'] = 'passed; disposable containers/network/volumes removed; test image retained'
            current = containers()
            assert all(current.get(key) == value for key, value in original_containers.items())
            assert original_volumes <= set(docker('volume', 'ls', '-q').splitlines())
            assert original_images <= set(docker('image', 'ls', '--no-trunc', '--format', '{{.Repository}}:{{.Tag}} {{.ID}}').splitlines())
            report['existing_resources_preserved'] = {'containers': len(original_containers),
                                                      'volumes': len(original_volumes), 'result': 'passed'}
            passed('Cleanup and original container states, image tags and volumes preserved')
        except Exception as exc:
            report['result'] = 'failed'
            report['cleanup_error'] = str(exc)
            raise
        finally:
            env_file.unlink(missing_ok=True)
            compose_file.unlink(missing_ok=True)
            report['finished_at'] = datetime.now(timezone.utc).isoformat()
            (directory / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            if report_path:
                with Path(report_path).open('x', encoding='utf-8') as output:
                    json.dump(report, output, indent=2)
            print('Evidence: ' + str(directory / 'report.json'), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep-data', action='store_true', help='Retain all UUID test volumes after verification')
    parser.add_argument('--report', type=Path, help='New sanitized evidence JSON; refuses overwrite')
    parser.add_argument('--extended-regressions', action='store_true',
                        help='Also run the separate dashboard/manual replay security contract tests')
    parser.add_argument('--integrated-replay', action='store_true', help='Checkpoint 4 authenticated grant-based Hospital A/B recovery')
    parser.add_argument('--adaptive-admission', action='store_true', help='Checkpoint 5 real telemetry, load and durable admission pacing')
    parser.add_argument('--reliability', action='store_true', help='Checkpoint 6 network faults, unequal backlogs and integrated reliability')
    parser.add_argument('--regression-only', action='store_true', help='Fresh Docker PostgreSQL plus full unit/integration suite including dashboard Docker regression')
    args = parser.parse_args()
    if args.report and args.report.exists():
        parser.error('--report already exists')
    verify(args.report, extended_regressions=args.extended_regressions, integrated_replay=args.integrated_replay, adaptive_admission=args.adaptive_admission, reliability=args.reliability, regression_only=args.regression_only, keep_data=args.keep_data)
