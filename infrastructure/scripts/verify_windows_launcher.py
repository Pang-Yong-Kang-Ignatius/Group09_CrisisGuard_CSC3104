"""Run the actual Windows launcher from a fresh source copy with isolated data.

Requires Python 3.11, installed requirements and Docker Desktop. No AWS access.
The new venv shares the verified dependency directory of the invoking environment;
this verifies fresh-source startup, not a clean Windows installation or pip install.
Only this run's containers, volumes and image are removed. Private .env stays in
the ignored run folder; the report contains no authentication values.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import venv

from infrastructure.scripts import smoke_deployment as smoke

ROOT = Path(__file__).resolve().parents[2]


def verify():
    if os.name != 'nt' or sys.version_info[:2] != (3, 11):
        raise RuntimeError('Run this verifier on Windows with Python 3.11')
    run_id = 'cg-launcher-check-' + uuid.uuid4().hex[:12]
    directory = ROOT / '.venv' / run_id
    source = directory / 'source'
    source.mkdir(parents=True)
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith(('COMPOSE_', 'CONTROLLER_', 'HOSPITAL_', 'POSTGRES_', 'DASHBOARD_',
                           'CLOUD_', 'AUTO_REPLAY', 'METAGUARD_')) or key in (
                               'AWS_REGION', 'AWS_DEFAULT_REGION', 'AWS_EXECUTION_ENV',
                               'EC2_INSTANCE_ID', 'APP_ENV', 'ENVIRONMENT', 'DEPLOYMENT_ENV',
                               'CRISISGUARD_DEPLOYMENT_MODE'):
            environment.pop(key)
    environment.update(TEMP=str(directory), TMP=str(directory), AUTO_REPLAY='1', PYTHONUTF8='1')
    ports = smoke.free_ports(8)
    keys = ('HOSPITAL_A_PORT', 'HOSPITAL_B_PORT', 'PATIENT_PORT', 'EMERGENCY_PORT',
            'ADMIN_PORT', 'CONTROLLER_PORT', 'DASHBOARD_PORT', 'POSTGRES_PORT')
    environment.update({key: str(port) for key, port in zip(keys, ports)})
    image = 'crisisguard-launcher-test:' + run_id
    project = None
    args = None
    report = {'run': run_id, 'aws_used': False, 'result': 'failed',
              'scope': 'Fresh source on existing Windows/Docker; shared installed dependencies; no interactive browser/agent'}

    def docker(*command):
        result = subprocess.run(['docker', *command], cwd=source, env=environment,
                                text=True, encoding='utf-8', errors='replace', capture_output=True, timeout=180)
        if result.returncode:
            raise RuntimeError('Docker verification command failed; private output suppressed')
        return result.stdout.strip()

    original_ids = set(docker('ps', '-aq').splitlines())
    original_volumes = set(docker('volume', 'ls', '-q').splitlines())
    try:
        names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                                        cwd=ROOT).decode().split('\0')
        for name in names:
            if not name or name.startswith(('.git/', '.venv/', '.venv311/')):
                continue
            item = ROOT / name
            if item.is_file():
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, target)
        venv.EnvBuilder(with_pip=False).create(source / '.venv311')
        dependency_dir = Path(sys.prefix) / 'Lib' / 'site-packages'
        (source / '.venv311/Lib/site-packages/verified-dependencies.pth').write_text(str(dependency_dir)+'\n')
        (source / 'compose.override.yaml').write_text(json.dumps({'services': {
            service: {'image': image} for service in ('patient', 'emergency', 'admin', 'hospital-a',
                                                       'hospital-b', 'controller', 'dashboard')}}))
        result = subprocess.run([os.environ.get('ComSpec', 'cmd.exe'), '/d', '/c', 'start.project.bat', '--verify'],
                                cwd=source, env=environment, text=True, encoding='utf-8', errors='replace', capture_output=True, timeout=600)
        # Verification mode never starts the token-printing outage agent.
        (directory / 'launcher.log').write_text(result.stdout + result.stderr, encoding='utf-8')
        match = re.search(r'Compose project: (cg-launcher-[0-9]+)', result.stdout)
        if not match:
            raise RuntimeError('Launcher did not select an isolated verification project')
        project = match[1]
        args = ['compose', '-p', project, '-f', 'compose.yaml', '-f',
                'infrastructure/docker/compose.metaguard.yaml', '-f',
                'infrastructure/docker/compose.dashboard-test-traffic.local.yaml',
                '-f', 'compose.override.yaml', '--profile', 'metaguard']
        if result.returncode:
            raise RuntimeError('Launcher failed; see ignored launcher.log')
        raw = docker(*args, 'ps', '--format', 'json')
        rows = json.loads(raw) if raw.startswith('[') else [json.loads(row) for row in raw.splitlines()]
        assert len(rows) == 8 and all(row['Health'] == 'healthy' for row in rows)
        probes = []
        for key, port in zip(keys[:7], ports[:7]):
            endpoint = '/' if key.startswith('HOSPITAL_') else '/health'
            smoke.request(port, endpoint)
            probes.append(key)
        assert (source / '.env').is_file()
        assert smoke.request(ports[6], '/api/test-traffic')['enabled'] is True
        assert (source / 'db-snapshots/hospital-a.db').is_file()
        assert (source / 'db-snapshots/hospital-b.db').is_file()
        report.update(result='passed', python=sys.version.split()[0], services_healthy=8,
                      http_probes_passed=len(probes), private_configuration_generated=True,
                      local_test_traffic_enabled=True,
                      hospital_snapshots_created=True)
    finally:
        if args:
            owned = set(docker(*args, 'ps', '-aq').splitlines())
            volumes = set(docker('volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project='+project).splitlines())
            assert not (owned & original_ids) and not (volumes & original_volumes)
            docker(*args, 'down', '--remove-orphans')
            for volume in volumes:
                assert json.loads(docker('volume', 'inspect', volume))[0]['Labels']['com.docker.compose.project'] == project
                docker('volume', 'rm', volume)
        assert original_ids <= set(docker('ps', '-aq').splitlines())
        assert original_volumes <= set(docker('volume', 'ls', '-q').splitlines())
        report['retained_resources_preserved'] = True
        if docker('image', 'ls', '-q', image):
            docker('image', 'rm', image)
        report['cleanup'] = 'Only this run\'s containers, volumes and image removed; private run folder retained'
        (directory / 'report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(report, indent=2), flush=True)
        print('Evidence: '+str(directory / 'report.json'), flush=True)
    return report


if __name__ == '__main__':
    verify()
