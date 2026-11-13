"""Read-only preflight for the supported AWS cloud/hospital Compose deployment.

Resolved configuration (including secrets) stays in memory. No service is
started, no AWS SDK is used, and failures never print configuration values.
Local development defaults remain supported by the separate local Compose file.
"""
import argparse
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                         ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def private_ip(value):
    try:
        address = ipaddress.ip_address(value)
        return any(address in network for network in PRIVATE_NETWORKS)
    except ValueError:
        return False


def volumes(service, target):
    matches = [v for v in service.get('volumes', []) if v.get('target') == target]
    require(len(matches) == 1 and matches[0].get('type') == 'volume'
            and bool(matches[0].get('source')) and not matches[0].get('read_only'),
            'Require the writable named database volume')


def runtime(service):
    require(service.get('restart') == 'unless-stopped', 'Require the AWS restart policy')
    health = service.get('healthcheck', {})
    require(health.get('test') and health['test'][0] != 'NONE' and not health.get('disable'),
            'Require a container health check')
    logging = service.get('logging', {})
    require(logging.get('driver') == 'json-file'
            and logging.get('options', {}).get('max-size') == '10m'
            and str(logging.get('options', {}).get('max-file')) == '3',
            'Require bounded AWS container logs')


def listener(service, *, private=False):
    ports = service.get('ports', [])
    require(len(ports) == 1, 'Require exactly one application listener')
    port = ports[0]
    address = port.get('host_ip', '')
    require(private_ip(address) if private else address == '127.0.0.1',
            'Cloud listeners must use a private IP; hospital listeners must use loopback')
    require(port.get('protocol', 'tcp') == 'tcp' and str(port.get('target')) == '8000',
            'Unexpected application listener target')
    published = str(port.get('published', ''))
    require(published.isdigit() and 1024 <= int(published) <= 65535,
            'Require an explicit unprivileged application port')
    return address, published


def audit_metaguard(role, services):
    """Opt-in extension; legacy deployments retain their existing preflight."""
    def token(value):
        return isinstance(value, str) and bool(re.fullmatch(r'[a-fA-F0-9]{48,}', value))
    if role == 'cloud':
        protected = any(str(services[name].get('environment', {}).get('METAGUARD_REQUIRED', '0')) == '1'
                        for name in ('patient', 'emergency', 'admin'))
        if 'controller' not in services and not protected:
            return
        require('controller' in services, 'Protected Cloud requires the controller')
        controller = services['controller']
        runtime(controller)
        address, port = listener(controller, private=True)
        env = controller.get('environment', {})
        require(env.get('DATABASE_URL') == services['patient']['environment']['DATABASE_URL'],
                'Controller must use the same authoritative PostgreSQL database')
        identities = json.loads(env.get('CONTROLLER_HOSPITAL_TOKENS', '{}'))
        require(isinstance(identities, dict) and set(identities) == {'Hospital_A', 'Hospital_B'} and all(token(v) for v in identities.values())
                and len(set(identities.values())) == 2 and token(env.get('CONTROLLER_CLOUD_TOKEN'))
                and env['CONTROLLER_CLOUD_TOKEN'] not in identities.values(), 'Require distinct private generated tokens')
        require(str(env.get('CONTROLLER_ADMISSION_ENABLED')) == '1', 'Require adaptive admission')
        require(env.get('CONTROLLER_ADMISSION_POLICY') in ('fixed', 'aimd'),
                'Require an explicit fixed or aimd admission policy')
        additive = float(env.get('CONTROLLER_ADMISSION_ADDITIVE_RPS', 'nan'))
        require(math.isfinite(additive) and additive > 0,
                'Require a finite positive AIMD additive rate')
        require(controller.get('depends_on', {}).get('postgres', {}).get('condition') == 'service_healthy',
                'Controller must wait for healthy PostgreSQL')
        health = ' '.join(controller.get('healthcheck', {}).get('test', []))
        require('get_connection' in health and '/health' in health, 'Require HTTP and database readiness')
        for name in ('patient', 'emergency', 'admin'):
            cloud = services[name]
            require(cloud['environment'].get('METAGUARD_REQUIRED') == '1'
                    and cloud['environment'].get('CONTROLLER_HOSPITAL_TOKENS') == env['CONTROLLER_HOSPITAL_TOKENS'],
                    'All Cloud writers must enforce the same hospital identities')
            own_address, own_port = listener(cloud, private=True)
            require(own_address == address and own_port != port, 'Controller must use a distinct port on the private Cloud host')
            require(env.get('CONTROLLER_' + name.upper() + '_METRICS_URL', f'http://{name}:8000/metrics')
                    == f'http://{name}:8000/metrics', 'Use internal Cloud telemetry service addresses')
        for suffix, default in (('RPS', (2, .5, .2)), ('INFLIGHT', (4, 2, 1))):
            values = [float(env.get('CONTROLLER_ADMISSION_'+state+'_'+suffix, value))
                      for state,value in zip(('NORMAL','CAUTION','RECOVERY'), default)]
            require(all(math.isfinite(v) and v > 0 for v in values) and values == sorted(values, reverse=True),
                    'Admission limits must be finite, positive and ordered')
            require(suffix != 'INFLIGHT' or all(value.is_integer() for value in values),
                    'Admission concurrency limits must be integers')
        for suffix, defaults in (('LATENCY_MS',(150,250,1000)), ('ERROR_RATE',(.01,.05,.2)),
                                 ('THROUGHPUT_RPS',(10,20,50))):
            values=[float(env.get('CONTROLLER_ADMISSION_'+state+'_'+suffix, default))
                    for state,default in zip(('HEALTHY','CAUTION','RECOVERY'),defaults)]
            require(all(math.isfinite(value) and value>0 for value in values)
                    and values[0]<values[1]<values[2] and (suffix!='ERROR_RATE' or values[2]<=1),
                    'Require finite ordered telemetry thresholds')
        timing={name:float(env.get('CONTROLLER_ADMISSION_'+name, default))
                for name,default in (('POLL_SECONDS',5),('TIMEOUT_SECONDS',2),('MAX_AGE_SECONDS',15),('DWELL_SECONDS',15))}
        require(all(math.isfinite(value) and value>0 for value in timing.values())
                and timing['MAX_AGE_SECONDS']>timing['POLL_SECONDS']+timing['TIMEOUT_SECONDS'],
                'Require safe telemetry freshness and hysteresis intervals')
        count=str(env.get('CONTROLLER_ADMISSION_HEALTHY_SAMPLES','3'))
        require(count.isdigit() and int(count)>0, 'Require a positive integer healthy sample count')
    else:
        env = services['hospital'].get('environment', {})
        if str(env.get('METAGUARD_ENABLED', '0')) != '1':
            return
        require(token(env.get('HOSPITAL_CONTROLLER_TOKEN')), 'Require this hospital private generated token')
        require(not any(key in env for key in ('DATABASE_URL','CONTROLLER_CLOUD_TOKEN','CONTROLLER_HOSPITAL_TOKENS',
                                             'HOSPITAL_A_CONTROLLER_TOKEN','HOSPITAL_B_CONTROLLER_TOKEN')),
                'Hospital must not receive database, operator or peer credentials')
        endpoint = urlsplit(env.get('CONTROLLER_URL', ''))
        cloud = urlsplit(env['CLOUD_URL'])
        require(endpoint.scheme == 'http' and private_ip(endpoint.hostname or '') and endpoint.hostname == cloud.hostname
                and endpoint.port and 1024 <= endpoint.port <= 65535 and endpoint.path in ('','/')
                and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment,
                'Use an explicit private controller endpoint on the Cloud host without embedded credentials')
        require(endpoint.port not in {urlsplit(env[key]).port for key in ('CLOUD_URL','EMERGENCY_URL','ADMIN_URL')},
                'Controller must have its own private listener')


def audit(role, config):
    require(role in ('cloud', 'hospital'), 'Unknown deployment role')
    require(bool(re.fullmatch(r'[a-z0-9][a-z0-9_-]*', config.get('name', ''))),
            'Require an explicit Compose project name')
    # Do not expose any value from the rendered configuration in errors/reports.
    require('REPLACE_' not in json.dumps(config), 'Replace deployment placeholders first')
    services = config['services']
    selected = ['patient', 'emergency', 'admin', 'postgres'] if role == 'cloud' else ['hospital']
    require(all(name in services for name in selected), 'Required deployment services are missing')
    for name in selected:
        runtime(services[name])
    if role == 'cloud':
        postgres = services['postgres']
        require(not postgres.get('ports'), 'PostgreSQL must not publish host ports')
        volumes(postgres, '/var/lib/postgresql/data')
        settings = postgres['environment']
        user, database = settings.get('POSTGRES_USER', ''), settings.get('POSTGRES_DB', '')
        password = settings.get('POSTGRES_PASSWORD', '')
        require(all(isinstance(v, str) and re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', v)
                    for v in (user, database)), 'Require explicit database user/name identifiers')
        require(isinstance(password, str) and bool(re.fullmatch(r'[a-fA-F0-9]{48,}', password)),
                'AWS requires a generated hex database password of at least 48 characters')
        expected_url = f'postgresql://{user}:{password}@postgres:5432/{database}'
        listeners = []
        for name in ('patient', 'emergency', 'admin'):
            require(services[name].get('environment', {}).get('DATABASE_URL') == expected_url,
                    'Cloud database settings must match the selected PostgreSQL service')
            listeners.append(listener(services[name], private=True))
        require(len(set(listeners)) == 3 and len({address for address, _ in listeners}) == 1,
                'Cloud APIs require distinct ports on the same private IP')
    else:
        hospital = services['hospital']
        volumes(hospital, '/data')
        listener(hospital)
        settings = hospital['environment']
        require(settings.get('HOSPITAL_DB_PATH') == '/data/hospital.db',
                'Hospital database path must use its persistent volume')
        require(settings.get('SITE_ID') in ('Hospital_A', 'Hospital_B'), 'Require an explicit hospital site')
        require(str(settings.get('AUTO_REPLAY')) == '1', 'AWS requires automatic recovery enabled')
        interval = float(settings.get('AUTO_REPLAY_INTERVAL_SECONDS', '5'))
        require(math.isfinite(interval) and interval > 0, 'Recovery interval must be finite and positive')
        endpoints = []
        for name in ('CLOUD_URL', 'EMERGENCY_URL', 'ADMIN_URL'):
            url = urlsplit(settings.get(name, ''))
            require(url.scheme == 'http' and private_ip(url.hostname or '') and url.port
                    and url.path == '/events' and not url.username and not url.password
                    and not url.query and not url.fragment,
                    'Require all three explicit private cloud event URLs without credentials')
            require(1024 <= url.port <= 65535, 'Unexpected cloud API port')
            endpoints.append((url.hostname, url.port))
        require(len(set(endpoints)) == 3 and len({host for host, _ in endpoints}) == 1,
                'Hospital routing requires three distinct ports on one private cloud host')
    audit_metaguard(role, services)
    if role == 'cloud' and 'dashboard' in services:
        dashboard = services['dashboard']
        runtime(dashboard)
        ports = dashboard.get('ports', [])
        require(len(ports) == 1 and ports[0].get('host_ip') == '127.0.0.1'
                and str(ports[0].get('target')) == '8080', 'Dashboard must remain on Cloud loopback')
        env = dashboard.get('environment', {})
        controller = services.get('controller', {}).get('environment', {})
        require(env.get('METAGUARD_REQUIRED') == '1'
                and env.get('DASHBOARD_CONTROLLER_TOKEN') == controller.get('CONTROLLER_CLOUD_TOKEN')
                and bool(env.get('DASHBOARD_CONTROLLER_TOKEN'))
                and env.get('DASHBOARD_CONTROLLER_URL') == 'http://controller:8000'
                and env.get('DATABASE_URL') == services['patient']['environment']['DATABASE_URL'],
                'Dashboard requires the protected controller and authoritative database')
        host = services['patient']['ports'][0]['host_ip']
        for site, port in (('A', 18000), ('B', 18002)):
            require(env.get(f'DASHBOARD_HOSPITAL_{site}_URL') == f'http://{host}:{port}',
                    'Dashboard must use distinct private hospital reverse tunnels')
    return {'result': 'passed', 'role': role, 'services_checked': len(selected) + int(role == 'cloud' and 'controller' in services) + int('dashboard' in services),
            'scope': 'Static AWS deployment configuration; no live health, security-group or record verification',
            'changes_made': False}


def audit_repository(names):
    """Check tracked artifact paths; this is not a content-based secret scanner."""
    for name in names:
        path = PurePosixPath(name)
        private = (any(part in {'.aws', '.venv', 'backups', 'release-checkpoints'} for part in path.parts)
            or path.name == 'credentials' or name.endswith('.private.json')
            or name.endswith(('.pem', '.ppk', '.key', '.db', '.sqlite', '.sqlite3', '.dump'))
            or bool(re.search(r'\.(db|sqlite|sqlite3)-', path.name))
            or (('.env' == path.name or path.name.startswith('.env.') or name.endswith('.env'))
                and not name.endswith('.env.example')))
        require(not private, 'Tracked private deployment artifact; remove it from tracking and review history privately')
    return {'result': 'passed', 'role': 'repository', 'tracked_paths_checked': len(names),
            'scope': 'Artifact names only; not a content-based secret/history scan', 'changes_made': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=['cloud', 'hospital', 'repository'])
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--compose-file', action='append', type=Path)
    parser.add_argument('--project-name')
    args = parser.parse_args(argv)
    try:
        if args.role == 'repository':
            result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=30)
            require(result.returncode == 0, 'Cannot inspect tracked artifact names')
            print(json.dumps(audit_repository([name for name in result.stdout.split('\0') if name])))
            return 0
        require(args.env_file is not None, 'Select the private environment file explicitly')
        require(args.env_file.is_file(), 'Selected private environment file is missing')
        command = ['docker', 'compose', '--env-file', str(args.env_file.resolve())]
        if args.project_name:
            command += ['--project-name', args.project_name]
        for path in args.compose_file or [ROOT / f'infrastructure/docker/compose.{args.role}.yaml',
                                         ROOT / f'infrastructure/aws/compose.{args.role}.override.yaml']:
            command += ['-f', str(path.resolve())]
        result = subprocess.run(command + ['config', '--format', 'json'], cwd=ROOT,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        require(result.returncode == 0, 'Compose could not resolve the selected deployment')
        report = audit(args.role, json.loads(result.stdout))
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        report = {'result': 'failed', 'role': args.role, 'changes_made': False,
                  'error': 'AWS configuration preflight failed; inspect private settings against CONFIGURATION.md. Values suppressed.'}
    print(json.dumps(report))
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
