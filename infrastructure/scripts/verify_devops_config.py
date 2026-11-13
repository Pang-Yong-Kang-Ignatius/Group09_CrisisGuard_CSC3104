"""Validate real Compose preflights/scaffold offline, without starting containers.

Uses temporary synthetic settings. Does not pull images or access AWS. The
controller fixture validates Compose syntax only, never controller behavior.
"""
import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

if __package__:
    from . import database_backup as backup
else:
    import database_backup as backup

ROOT = Path(__file__).resolve().parents[2]


def run(command, environment):
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                            text=True, stdin=subprocess.DEVNULL, timeout=60)
    return result.returncode, result.stdout


def verify(directory):
    cloud = {'CLOUD_PROJECT':'cg-audit-offline-cloud','CLOUD_BIND_ADDRESS':'172.31.1.2',
             'PATIENT_PORT':'8001','EMERGENCY_PORT':'8003','ADMIN_PORT':'8004',
             'POSTGRES_USER':'crisisguard','POSTGRES_DB':'crisisguard','POSTGRES_PASSWORD':'a'*48}
    hospital = {'HOSPITAL_PROJECT':'cg-audit-offline-a','HOSPITAL_BIND_ADDRESS':'127.0.0.1',
                'HOSPITAL_PORT':'8000','SITE_ID':'Hospital_A','AUTO_REPLAY':'1',
                'CLOUD_URL':'http://172.31.1.2:8001/events','EMERGENCY_URL':'http://172.31.1.2:8003/events',
                'ADMIN_URL':'http://172.31.1.2:8004/events'}
    controller_env = directory/'controller.private.env'
    controller_env.write_text('# Inert Compose fixture; real controller configuration not yet defined.\n')
    controller = {'CONTROLLER_IMAGE':'crisisguard-controller:configuration-fixture-only',
                  'CONTROLLER_ENV_FILE':str(controller_env), 'CONTROLLER_INTERNAL_PORT':'9000',
                  'CONTROLLER_HOST_PORT':'9000','CONTROLLER_STATE_PATH':'/controller-state'}
    environment = os.environ.copy()
    for key in {*cloud, *hospital, *controller, 'AUTO_REPLAY_INTERVAL_SECONDS', 'COMPOSE_PROFILES',
                'COMPOSE_PROJECT_NAME','COMPOSE_FILE','COMPOSE_ENV_FILES'}:
        environment.pop(key, None)
    for key in tuple(environment):
        if key.startswith(('CONTROLLER_', 'HOSPITAL_')):
            environment.pop(key)
    reports = []
    def audit(role, settings, success, metaguard=False, full_system=False):
        path = directory/(settings.get('CLOUD_PROJECT') or settings['HOSPITAL_PROJECT'])
        path.write_text(''.join(f'{k}={v}\n' for k,v in settings.items()))
        command = [sys.executable,str(ROOT/'infrastructure/scripts/audit_deployment.py'),
                   role,'--env-file',str(path)]
        if metaguard:
            for file in (f'infrastructure/docker/compose.{role}.yaml',
                         f'infrastructure/aws/compose.{role}.override.yaml',
                         f'infrastructure/aws/compose.metaguard.{role}.yaml'):
                command += ['--compose-file', str(ROOT/file)]
        if full_system:
            command += ['--compose-file', str(ROOT/'infrastructure/aws/compose.dashboard.cloud.yaml')]
        code, output = run(command,environment)
        if (code == 0) != success or json.loads(output)['result'] != ('passed' if success else 'failed'):
            raise RuntimeError('Unexpected real Compose configuration preflight result; output suppressed')
        reports.append({'role':role,'expected_acceptance':success,'result':'passed',
                        'metaguard':metaguard,
                        'full_system':full_system,
                        'policy':settings.get('CONTROLLER_ADMISSION_POLICY') if metaguard and role=='cloud' else None})
        return path
    cloud_file = audit('cloud',cloud,True)
    audit('hospital',hospital,True)
    audit('hospital',{**hospital,'HOSPITAL_PROJECT':'cg-audit-offline-b','SITE_ID':'Hospital_B'},True)
    audit('cloud',{**cloud,'CLOUD_PROJECT':'cg-audit-offline-unsafe','CLOUD_BIND_ADDRESS':'0.0.0.0'},False)
    audit('cloud',{**cloud,'CLOUD_PROJECT':'cg-audit-offline-default','POSTGRES_PASSWORD':'crisisguard_dev'},False)
    audit('hospital',{**hospital,'HOSPITAL_PROJECT':'cg-audit-offline-missing','ADMIN_URL':''},False)
    protected_cloud = {**cloud, 'CLOUD_PROJECT':'cg-audit-protected',
        'CONTROLLER_CLOUD_TOKEN':'c'*64, 'HOSPITAL_A_CONTROLLER_TOKEN':'d'*64,
        'HOSPITAL_B_CONTROLLER_TOKEN':'e'*64, 'CONTROLLER_ADMISSION_ADDITIVE_RPS':'0.2'}
    for policy in ('fixed','aimd'):
        audit('cloud',{**protected_cloud,'CONTROLLER_ADMISSION_POLICY':policy},True,True)
    audit('cloud', protected_cloud, True, True, True)
    audit('cloud',{**protected_cloud,'CONTROLLER_ADMISSION_POLICY':'unknown'},False,True)
    audit('cloud',{**protected_cloud,'CONTROLLER_ADMISSION_POLICY':'aimd',
                   'CONTROLLER_ADMISSION_ADDITIVE_RPS':'nan'},False,True)
    for site in ('Hospital_A','Hospital_B'):
        audit('hospital',{**hospital,'HOSPITAL_PROJECT':'cg-audit-protected-'+site[-1].lower(),
                          'SITE_ID':site, 'CONTROLLER_URL':'http://172.31.1.2:8005',
                          'HOSPITAL_CONTROLLER_TOKEN':('d' if site=='Hospital_A' else 'e')*64},True,True)
    # Render exactly the settings generated by fresh-host provisioning, rather
    # than relying exclusively on manually maintained configuration fixtures.
    settings = runpy.run_path(str(ROOT/'infrastructure/aws/configure_host.py'))['settings']
    generated = dict(role='cloud', private_ip='172.31.1.2', cloud_ip='172.31.1.2',
                     database_password='a'*64, operator_token='b'*64,
                     hospital_a_token='c'*64, hospital_b_token='d'*64,
                     live_telemetry_token='e'*64, replay_telemetry_token='f'*64)
    audit('cloud', settings(generated), True, True, True)
    for role, address, token in (('hospital-a','172.31.1.3','c'*64), ('hospital-b','172.31.1.4','d'*64)):
        audit('hospital', settings({**generated, 'role':role, 'private_ip':address,
                                   'hospital_token':token}), True, True)
    with cloud_file.open('a') as output:
        output.write(''.join(f'{k}={v}\n' for k,v in controller.items()))
    command = ['docker','compose','--env-file',str(cloud_file),'-f',str(ROOT/'infrastructure/docker/compose.cloud.yaml'),
               '-f',str(ROOT/'infrastructure/aws/compose.cloud.override.yaml'),
               '-f',str(ROOT/'infrastructure/docker/compose.controller.example.yaml'),
               '--profile','controller','config','--format','json']
    code, output = run(command,environment)
    if code: raise RuntimeError('Controller scaffold Compose validation failed; output suppressed')
    service=json.loads(output)['services']['controller']
    if (service['pull_policy']!='never' or service['ports'][0]['host_ip']!='127.0.0.1'
            or service['volumes'][0]['source']!='controller-data'
            or set(service['depends_on'])!={'patient','emergency','admin'}):
        raise RuntimeError('Controller scaffold wiring differs from the reviewed contract')
    return {'result':'passed','preflights':reports,'controller_scaffold':'configuration_only_passed',
            'containers_started':0,'aws_used':False,'scope':'Real Compose rendering; synthetic settings; no controller implementation/health claim'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,help='New sanitized report; refuses overwrite')
    args=parser.parse_args()
    output=backup.private_file(args.report) if args.report else None
    try:
        with tempfile.TemporaryDirectory(prefix='cg-devops-config-') as temporary:
            report=verify(Path(temporary))
    except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError):
        report={'result':'failed','error':'Offline configuration verification failed; private output suppressed',
                'containers_started':0,'aws_used':False}
    encoded=(json.dumps(report,indent=2)+'\n').encode()
    if output:
        with output: output.write(encoded)
    print(encoded.decode(),end='')
    return 0 if report['result']=='passed' else 1


if __name__=='__main__':
    sys.exit(main())
