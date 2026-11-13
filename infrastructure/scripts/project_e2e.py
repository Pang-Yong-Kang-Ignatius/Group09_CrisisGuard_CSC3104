"""Real HTTP/SQLite/PostgreSQL verification in an owned disposable Docker stack.

No root .env, private overrides, existing volumes, remote Docker or AWS. Faults
and deletion require a confirmation bound to the runtime isolation manifest.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import uuid

from infrastructure.scripts import project_verify as verification
from infrastructure.scripts.smoke_deployment import free_ports
from infrastructure.scripts.project_docker_guard import (
    DockerGuardError, LiveDocker, capture_inventory, preservation_changes,
    stable_baseline, validate_baseline,
)

ROOT = Path(__file__).resolve().parents[2]
LABEL = 'org.crisisguard.e2e.run'
SERVICES = ('postgres', 'controller', 'patient', 'emergency', 'admin',
            'hospital-a', 'hospital-b', 'dashboard', 'fault-proxy')
PORT_KEYS = ('POSTGRES_PORT', 'CONTROLLER_PORT', 'PATIENT_PORT', 'EMERGENCY_PORT',
             'ADMIN_PORT', 'HOSPITAL_A_PORT', 'HOSPITAL_B_PORT', 'DASHBOARD_PORT', 'FAULT_PROXY_PORT')
CONFIRMATION_PLACEHOLDERS = (
    'Cloud contract/security faults', 'Scheduler policy/retry/ageing scenarios',
    'Network outage and restart/crash scenarios', 'Fixed/AIMD/experimental admission scenarios',
    'Dashboard traffic/error/concurrency scenarios')


class IsolationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise IsolationError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sanitized(value, secrets_to_remove=()):
    """Only documented synthetic identifiers/counters enter reports, never bodies."""
    text = str(value)
    for secret in sorted(secrets_to_remove, key=len, reverse=True):
        if secret:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'postgres(?:ql)?://\S+', '[REDACTED_DATABASE_URL]', text)
    text = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [REDACTED]', text)
    return text


def audit_config(config, project, nonce, original_volumes=(), original_networks=()):
    """Reject dangerous Compose features BEFORE creating even a test volume."""
    require(config.get('name') == project and re.fullmatch(r'cg-e2e-[a-f0-9]{24}', project), 'Invalid project identity')
    require(set(config.get('services', {})) == set(SERVICES), 'Unexpected service set')
    volumes, networks = config.get('volumes', {}), config.get('networks', {})
    require(set(volumes) == {'postgres-data', 'hospital-a-data', 'hospital-b-data'}, 'Unexpected volumes')
    require(set(networks) == {'default', 'control'}, 'Unexpected networks')
    for key, definitions, baseline in (('volume', volumes, original_volumes), ('network', networks, original_networks)):
        for item in definitions.values():
            require(not item.get('external') and item['name'].startswith(project + '_')
                    and item['name'] not in baseline and item.get('labels', {}).get(LABEL) == nonce,
                    f'Unowned or reused {key}')
            require(not item.get('driver_opts'), f'External {key} driver options')
            if key == 'network':
                require(item.get('internal') is True, 'Runtime network must have no external route')
    ports = []
    for name, service in config['services'].items():
        require(service.get('labels', {}).get(LABEL) == nonce, 'Missing service ownership label')
        require(not any(service.get(k) for k in ('privileged', 'devices', 'network_mode', 'pid', 'ipc',
                    'extra_hosts', 'cap_add', 'volumes_from', 'extends', 'external_links', 'secrets', 'configs', 'env_file')),
                'Unsafe service capability or external configuration')
        require(not service.get('container_name') and not service.get('build'), 'Runtime must use pinned owned image')
        for mount in service.get('volumes', []):
            require(mount.get('type') == 'volume' and mount.get('source') in volumes, 'Bind/foreign mount refused')
        require(set(service.get('networks', {})) <= set(networks), 'Foreign network')
        for port in service.get('ports', []):
            require(port.get('host_ip') == '127.0.0.1' and int(port['published']) > 1024, 'Non-loopback port')
            ports.append(int(port['published']))
        environment = service.get('environment', {})
        require(not any(k.startswith('AWS_') for k in environment), 'AWS environment refused')
        for key in ('DATABASE_URL', 'CONTROLLER_URL', 'CLOUD_URL', 'EMERGENCY_URL', 'ADMIN_URL',
                    'DASHBOARD_PATIENT_URL', 'DASHBOARD_EMERGENCY_URL', 'DASHBOARD_ADMIN_URL',
                    'DASHBOARD_HOSPITAL_A_URL', 'DASHBOARD_HOSPITAL_B_URL', 'DASHBOARD_CONTROLLER_URL'):
            if key in environment:
                from urllib.parse import urlsplit
                require(urlsplit(environment[key]).hostname in SERVICES, 'External service target refused')
    require(len(ports) == len(set(ports)) == len(SERVICES), 'Ports must be unique for every service')
    require(config['services']['hospital-a']['volumes'][0]['source'] !=
            config['services']['hospital-b']['volumes'][0]['source'], 'Hospital SQLite volumes overlap')
    for name in ('patient', 'emergency', 'admin'):
        require(config['services'][name]['environment'].get('METAGUARD_REQUIRED') == '1', 'Cloud protection disabled')
    for name in ('hospital-a', 'hospital-b'):
        require(config['services'][name]['environment'].get('METAGUARD_ENABLED') == '1', 'Protected replay disabled')


class Stack:
    def __init__(self, directory, state, report):
        self.directory, self.state, self.report = directory, state, report
        self.project, self.nonce = state['project'], state['nonce']
        self.settings = state['settings']
        self.config_file = directory / 'compose.private.json'
        self.config = json.loads(self.config_file.read_text(encoding='utf-8')) if self.config_file.exists() else None
        self.environment = verification.test_environment(directory)
        for key in list(self.environment):
            if key.startswith(('COMPOSE_', 'CONTROLLER_', 'CLOUD_', 'HOSPITAL_', 'DASHBOARD_',
                               'METAGUARD_', 'POSTGRES_', 'SCHEDULER_', 'AWS_')):
                self.environment.pop(key)
        self.environment['AWS_EC2_METADATA_DISABLED'] = 'true'
        self.environment['COMPOSE_DISABLE_ENV_FILE'] = '1'
        self.confirmed = False
        self.active_faults = list(state.get('active_faults', []))
        self._live_reader = None
        self._provisioning = False
        self._runtime_config = None

    @property
    def secrets(self):
        return [v for k, v in self.settings.items() if 'TOKEN' in k or 'PASSWORD' in k]

    def command(self, args, *, timeout=60, allow_failure=False):
        # Always capture before publishing. Docker inspect/config contain secrets.
        result = subprocess.run(args, cwd=ROOT, env=self.environment, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=timeout)
        if result.returncode and not allow_failure:
            with (self.directory / 'operations.log').open('a', encoding='utf-8') as log:
                log.write(sanitized(f'{args[0]} {args[1]}: exit {result.returncode}\n' + result.stderr, self.secrets) + '\n')
            raise RuntimeError(f'{args[0]} {args[1]} failed with exit {result.returncode}')
        return result

    def docker(self, *args, **kwargs):
        require(args and args[0] in ('info', 'ps', 'inspect', 'image', 'network', 'volume',
                                     'context', 'exec', 'logs', 'build', 'compose', 'start', 'stop', 'restart', 'kill', 'rm'),
                'Unsupported Docker operation; no mutation allowed')
        if args[0] in ('network', 'volume', 'image', 'context'):
            allowed = {'network': ('ls', 'inspect', 'rm', 'connect', 'disconnect'),
                       'volume': ('ls', 'inspect', 'rm'), 'image': ('ls', 'inspect'), 'context': ('inspect',)}
            require(len(args) > 1 and args[1] in allowed[args[0]], 'Unsupported Docker resource operation')
        if args and args[0] in ('start', 'stop', 'restart', 'kill', 'rm', 'network', 'volume'):
            destructive = args[0] not in ('network', 'volume') or (len(args) > 1 and args[1] in ('rm', 'connect', 'disconnect'))
            if destructive:
                proof = self.fault_gate(allow_missing=True)
                if args[0] in ('network', 'volume'):
                    rest = list(args[2:])
                    if args[1] == 'connect' and rest[:1] == ['--alias']:
                        require(len(rest) == 4 and rest[1] in ('hospital-a', 'hospital-b'), 'Invalid network alias/target')
                        rest = rest[2:]
                    allowed = (set(proof['volumes']) if args[0] == 'volume' else set(proof['networks'].values()))
                    require(bool(rest) and rest[0] in allowed, 'Destructive operation targets an unowned resource')
                    if args[1] in ('connect', 'disconnect'):
                        require(len(rest) == 2 and rest[1] in proof['containers'].values(), 'Unowned network endpoint')
                    else:
                        require(all(target in allowed for target in rest), 'Unowned deletion target')
                else:
                    rest = list(args[1:])
                    if rest[:1] in (['--time'], ['--signal']):
                        require(len(rest) >= 3 and (rest[0] == '--time' and rest[1].isdecimal()
                                or rest[0] == '--signal' and rest[1] == 'KILL'), 'Unsupported destructive flags')
                        rest = rest[2:]
                    require(rest and all(cid in proof['containers'].values() for cid in rest), 'Unowned container target')
        elif args and args[0] == 'build':
            require(self._provisioning, 'Image build is restricted to new-project provisioning')
            self.require_preserved()
        elif args and args[0] == 'exec':
            require(len(args) > 3 and args[1] in self.state.get('owned', {}).get('containers', {}).values()
                    and args[2] == 'psql' and args[-1] == 'SELECT system_identifier::text FROM pg_control_system()',
                    'Raw exec is restricted to the owned database identity probe')
            self.require_preserved()
            self.prove_ownership()
        elif args and args[0] == 'compose':
            # Raw Compose is used only to render configuration. Service-changing
            # operations must enter through compose(), which enforces both gates.
            require('config' in args and not any(a in args for a in
                    ('up', 'down', 'start', 'stop', 'restart', 'kill', 'rm', 'run', 'create', 'pause', 'unpause')),
                    'Raw mutating Compose calls are forbidden')
        return self.command(['docker', *args], **kwargs).stdout.strip()

    def compose(self, *args, timeout=180):
        require(bool(args), 'Missing Compose operation')
        if args[0] != 'config':
            require(args[0] in ('up', 'start', 'stop', 'restart', 'kill'), 'Unsupported Compose mutation')
            if self._provisioning and args[0] == 'up':
                self.require_preserved()
                audit_config(self.config, self.project, self.nonce, self.state['baseline']['volumes'],
                             [n['name'] for n in self.state['baseline']['networks'].values()])
            else:
                self.fault_gate()
                # An explicit service list is required after provisioning.
                values = iter(args[1:])
                targets = []
                for value in values:
                    if args[0] == 'up' and value in ('--pull', '--wait-timeout'):
                        option = next(values, None)
                        require(option == 'never' if value == '--pull' else option is not None and option.isdecimal(), 'Invalid Compose option')
                    elif args[0] == 'up' and value in ('-d', '--no-build', '--no-deps', '--wait', '--force-recreate'):
                        continue
                    else:
                        require(value in SERVICES, 'Compose mutation targets an unowned service')
                        targets.append(value)
                require(bool(targets), 'A service mutation must name owned services explicitly')
        return self.command(['docker', 'compose', '--project-name', self.project, '--env-file',
                           str(self.directory / 'settings.private.env'), '-f', str(self.config_file),
                           '--profile', 'metaguard', *args], timeout=timeout).stdout.strip()

    def save(self):
        self.state['active_faults'] = list(self.active_faults)
        self.report['active_faults'] = list(self.active_faults)
        verification.write_json(self.directory / 'session.private.json', self.state)
        verification.write_json(self.directory / 'report.json', self.report)
        rows = ['# CrisisGuard real-system E2E verification', '',
                f"Revision: `{self.report['revision']}`; project: `{self.project}`; run: `{self.nonce}`.",
                f"Created UTC: {self.report.get('created_utc', 'test fixture')}. Updated UTC: {datetime.now(timezone.utc).isoformat()}.",
                f"Phase: **{self.report['phase']}**. Only synthetic records and generated credentials used.", '',
                '| Scenario / component | Status | Evidence / limitation |', '|---|---|---|']
        rows += [f"| {item['name']} | {item['status']} | {item.get('detail', '')} |" for item in self.report['checks']]
        if self.report.get('confirmation_placeholder_history'):
            rows += ['', 'Earlier pending-confirmation placeholders are retained in report.json under '
                     'confirmation_placeholder_history. They are not executed scenario results.']
        rows += ['', 'HTTP results, event lifecycles, PostgreSQL/grant evidence, container/volume/network identities, ownership checks, sanitized scheduler logs and unresolved records are in report.json and logs/.',
                 'A health response alone does not establish functional correctness. Test-only SQL fixture changes are labeled separately from real outages and replay.',
                'Repeated lifecycle snapshots of the same IDs are retained observations, not additional events or distinct test cases.',
                 'No browser interaction or performance acceptance is inferred from these tests.', '']
        if self.report.get('confirmation_required'):
            rows += ['Fault injection and cleanup require explicit confirmation of this isolation manifest.',
                     'Review isolation.json, then use the continuation command printed by the runner. No timeout grants consent.', '']
        (self.directory / 'SUMMARY.md').write_text('\n'.join(rows), encoding='utf-8')

    def record(self, name, status, detail='', **evidence):
        self.report['checks'].append({'name': name, 'status': status, 'detail': detail, **evidence})
        print(f'{name:58} {status}', flush=True)
        self.save()

    def scenario(self, name, action):
        try:
            self.report['active_scenario'] = name
            self.save()
            evidence = action() or {}
            self.record(name, 'PASS', **evidence)
            return True
        except Exception as exc:
            self.record(name, 'FAIL', sanitized(str(exc), self.secrets)[:500], error_type=type(exc).__name__)
            return False

    def inspect(self, kind, identifiers):
        return json.loads(self.read_docker(*(['inspect'] if kind == 'container' else [kind, 'inspect']), *identifiers)) if identifiers else []

    @property
    def live_reader(self):
        if self._live_reader is None:
            self._live_reader = LiveDocker(self.environment)
            baseline = self.state.get('baseline')
            if baseline:
                validate_baseline(baseline)
                self._live_reader.identity = baseline['engine']
        return self._live_reader

    def read_docker(self, *args):
        return self.live_reader.output(*args)

    def inventory(self):
        return capture_inventory(self.live_reader)

    def capture_baseline(self):
        require('baseline' not in self.state, 'Replacing an existing baseline is forbidden; investigation required')
        verification.require_local_docker()
        baseline = stable_baseline(self.live_reader)
        # The CLI is used only to pin the mutation endpoint to the direct reader.
        # No CLI inventory is accepted as baseline or preservation evidence.
        selected = self.command(['docker', 'info', '--format', '{{.ID}}']).stdout.strip()
        require(selected == baseline['engine']['daemon_id'], 'Selected Docker endpoint differs from verified live engine')
        self.state['baseline'] = baseline
        self.save()
        return baseline

    def require_preserved(self):
        validate_baseline(self.state.get('baseline'))
        require(not self.state.get('preservation_block') and not any(
            c.get('status') == 'FAIL' and c.get('name') == 'Existing-resource preservation inventory'
            for c in self.report.get('checks', [])), 'Preservation failure is latched; investigation required')
        require(self.verify_preservation() is True, 'Preservation check did not pass; investigation required')
        verification.require_local_docker()
        selected = self.command(['docker', 'info', '--format', '{{.ID}}']).stdout.strip()
        require(selected == self.state['baseline']['engine']['daemon_id'], 'Docker mutation endpoint changed; investigation required')

    def prove_ownership(self, *, allow_missing=False):
        verification.require_local_docker()
        require(self.config is not None and digest(self.config_file) == self.state['config_sha256'], 'Compose manifest changed unexpectedly')
        baseline = self.state['baseline']
        validate_baseline(baseline)
        baseline_network_names = [n['name'] for n in baseline['networks'].values()]
        audit_config(self.config, self.project, self.nonce, baseline['volumes'], baseline_network_names)
        ids = self.read_docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + self.project).splitlines()
        rows = self.inspect('container', ids)
        by_service = {}
        volume_names = {v['name'] for v in self.config['volumes'].values()}
        network_names = {n['name'] for n in self.config['networks'].values()}
        for row in rows:
            labels = row['Config'].get('Labels') or {}
            service = labels.get('com.docker.compose.service')
            require(labels.get(LABEL) == self.nonce and labels.get('com.docker.compose.project') == self.project
                    and service in SERVICES and service not in by_service
                    and row['Id'] not in baseline['containers'], 'Foreign, duplicate or pre-existing container')
            require(set(row['NetworkSettings']['Networks']) <= network_names, 'Container attached to foreign network')
            require(not row['HostConfig'].get('Privileged'), 'Unsafe runtime capabilities')
            expected = (self._runtime_config or self.config)['services'][service]
            actual_environment = dict(value.split('=', 1) for value in row['Config'].get('Env', []) if '=' in value)
            require(all(actual_environment.get(key) == str(value) for key, value in expected.get('environment', {}).items()),
                    'Runtime environment differs from isolated manifest')
            expected_mounts = {self.config['volumes'][m['source']]['name']: m['target'] for m in expected.get('volumes', [])}
            require({m.get('Name'): m['Destination'] for m in row['Mounts']} == expected_mounts
                    and all(m['Type'] == 'volume' and m.get('Name') in volume_names for m in row['Mounts']), 'Runtime mount mismatch')
            require(row['Image'] == (self.state.get('postgres_image_id') if service == 'postgres' else self.state.get('app_image_id')),
                    'Runtime image differs from verified build')
            for entries in row['NetworkSettings'].get('Ports', {}).values():
                for port in entries or []:
                    require(port['HostIp'] == '127.0.0.1' and int(port['HostPort']) in self.state['ports'].values(), 'Runtime port mismatch')
            by_service[service] = row['Id']
        require(allow_missing or set(by_service) == set(SERVICES), 'Incomplete disposable stack')
        present_volumes = set(self.read_docker('volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + self.project).splitlines())
        present_networks = set(self.read_docker('network', 'ls', '--format', '{{.Name}}', '--filter', 'label=com.docker.compose.project=' + self.project).splitlines())
        require(present_volumes <= volume_names and present_networks <= network_names, 'Unexpected project resource')
        require(allow_missing or (present_volumes == volume_names and present_networks == network_names), 'Incomplete volume/network set')
        actual_volumes = self.inspect('volume', sorted(present_volumes))
        actual_networks = self.inspect('network', sorted(present_networks))
        for row in actual_volumes + actual_networks:
            require((row.get('Labels') or {}).get(LABEL) == self.nonce
                    and row['Labels'].get('com.docker.compose.project') == self.project, 'Unowned runtime resource')
            if 'Containers' in row:
                require(row.get('Internal') is True and set(row['Containers']) <= set(by_service.values()), 'Network has foreign peers or external route')
                require(row['Id'] not in baseline['networks'], 'Pre-existing network reused')
        proof = {'project': self.project, 'nonce': self.nonce, 'config_sha256': self.state['config_sha256'],
                 'containers': by_service, 'volumes': sorted(present_volumes),
                 'networks': {r['Name']: r['Id'] for r in actual_networks}, 'external_networks': False,
                 'existing_container_ids_disjoint': True, 'existing_volume_names_disjoint': True,
                 'loopback_ports': self.state['ports']}
        self.state['owned'] = proof
        self.report['environment'] = proof
        verification.write_json(self.directory / 'isolation.json', proof)
        self.save()
        return proof

    def confirm(self, token, *, allow_missing=False):
        self.require_preserved()
        proof = self.prove_ownership(allow_missing=True) if allow_missing else self.prove_ownership()
        require(token == self.project + ':' + self.nonce, 'Isolation confirmation must match project and run UUID')
        self.confirmed = True
        self.report['confirmation_required'] = False
        self.report['confirmation'] = {'scope': 'isolated fault scenarios, restoration and owned-resource cleanup',
                                       'project': proof['project'], 'at_utc': datetime.now(timezone.utc).isoformat()}
        self.save()

    def fault_gate(self, *, allow_missing=False):
        require(self.confirmed, 'Explicit confirmation required before faults or cleanup')
        self.require_preserved()
        return self.prove_ownership(allow_missing=True) if allow_missing else self.prove_ownership()

    def supersede_confirmation_placeholders(self, *, cleanup_only=False):
        """Retain pending-gate history without presenting it as current test output."""
        require(self.confirmed, 'Explicit confirmation required before updating pending gates')
        require(self.report['phase'] != 'complete', 'Completed evidence must remain unchanged')
        pending = [item for item in self.report['checks']
                   if item['name'] in CONFIRMATION_PLACEHOLDERS
                   and item['status'] == 'NOT VERIFIED'
                   and item.get('detail') == 'Awaiting isolation confirmation']
        if not pending:
            return
        self.report.setdefault('confirmation_placeholder_history', []).append({
            'at_utc': datetime.now(timezone.utc).isoformat(),
            'continuation': 'cleanup only' if cleanup_only else 'confirmed fault scenarios',
            'checks': deepcopy(pending)})
        if cleanup_only:
            for item in pending:
                item['detail'] = 'Not executed: cleanup-only continuation'
        else:
            self.report['checks'] = [item for item in self.report['checks'] if item not in pending]
        self.save()

    def http(self, service, method, path, body=None, *, headers=None, expected=200, timeout=20):
        return self.http_batch(service, [{'method': method, 'path': path, 'body': body,
                                         'headers': headers, 'expected': expected}], timeout=timeout)[0]

    def http_batch(self, service, requests, *, timeout=20):
        require(service in self.state['ports'] and service != 'postgres' and requests, 'HTTP target is outside test stack')
        base = 'http://127.0.0.1:8080' if service == 'dashboard' else 'http://' + service + ':8000'
        for item in requests:
            path = item['path']
            require(path.startswith('/') and not path.startswith('//') and '://' not in path, 'HTTP target is outside test stack')
        proof = self.state.get('owned')
        require(proof and 'dashboard' in proof['containers'], 'HTTP requires a proven owned client container')
        try:
            # Docker Desktop does not forward published ports on internal-only
            # networks reliably. Run the real HTTP client in the owned dashboard
            # container; it reaches hospital control links during partitions.
            program = '''import json,sys,httpx,time
x=json.load(sys.stdin)
result=[]
with httpx.Client(timeout=x['timeout'],trust_env=False,follow_redirects=False) as client:
    for item in x['requests']:
        start=time.monotonic()
        r=client.request(item['method'],x['base']+item['path'],json=item.get('body'),headers=item.get('headers'))
        result.append({'status':r.status_code,'text':r.text,'elapsed_ms':round((time.monotonic()-start)*1000,2)})
print(json.dumps(result))
'''
            result = subprocess.run(['docker', 'exec', '-i', proof['containers']['dashboard'], 'python', '-c', program],
                input=json.dumps({'base': base, 'requests': requests, 'timeout': timeout}),
                cwd=ROOT, env=self.environment, capture_output=True, text=True, timeout=timeout * len(requests) + 10)
            require(result.returncode == 0, f'{service} HTTP transport failed inside isolated client')
            responses = json.loads(result.stdout)
            require(len(responses) == len(requests), 'HTTP batch response count mismatch')
        except Exception as exc:
            self.report['http'].append({'service': service, 'paths': [r['path'] for r in requests],
                                        'actual': 'transport failure', 'expected': [r.get('expected', 200) for r in requests], 'error_type': type(exc).__name__})
            raise
        bodies = []
        for item, response in zip(requests, responses):
            method, path, expected = item['method'], item['path'], item.get('expected', 200)
            self.report['http'].append({'service': service, 'method': method, 'path': path,
                                    'actual': response['status'], 'expected': expected,
                                    'transport': 'real HTTP from owned dashboard container on internal Docker networks',
                                    'elapsed_ms': response['elapsed_ms']})
            self.save()
            require(not any(secret in response['text'] for secret in self.secrets), 'Credential exposed in HTTP response')
            if expected is not None:
                require(response['status'] in (expected if isinstance(expected, tuple) else (expected,)),
                    f'{service} {method} {path}: HTTP {response["status"]}, expected {expected}')
            bodies.append(json.loads(response['text']))
        return bodies

    def operator(self):
        return {'X-Cloud-Token': self.settings['CONTROLLER_CLOUD_TOKEN']}

    def hospital_auth(self, site):
        return {'Authorization': 'Bearer ' + self.settings['HOSPITAL_' + site[-1] + '_CONTROLLER_TOKEN']}

    def api(self, method, path, body=None, expected=200):
        return self.http('controller', method, path, body, headers=self.operator(), expected=expected)

    def query(self, sql, args=(), *, fixture=False):
        if fixture:
            self.fault_gate()
        else:
            require(sql.lstrip().upper().startswith(('SELECT', 'SHOW')), 'Use explicit fixture=True for test SQL mutations')
        proof = self.state['owned']
        program = '''import os,json,sys,psycopg
from datetime import datetime
from decimal import Decimal
x=json.load(sys.stdin)
diagnostic={'phase':'connect'}
try:
    with psycopg.connect(os.environ['DATABASE_URL'],connect_timeout=3,options='' if x['fixture'] else '-c default_transaction_read_only=on') as c:
        try:
            diagnostic['phase']='identity'
            identity=c.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone()[0]
            if identity != x['identity']: raise RuntimeError('PostgreSQL server identity mismatch')
            diagnostic['phase']='query'
            r=c.execute(x['sql'],x['args'] or None)
            rows=r.fetchall() if r.description else []
            diagnostic['phase']='commit'
        finally:
            diagnostic['transaction_status']=c.info.transaction_status.name
except Exception as exc:
    diagnostic.update(error_type=type(exc).__name__,sqlstate=getattr(exc,'sqlstate',None))
    print(json.dumps(diagnostic),file=sys.stderr)
    raise SystemExit(1)
def encode(v):
    if isinstance(v,datetime): return {'__e2e_datetime__':v.isoformat()}
    if isinstance(v,Decimal): return {'__e2e_decimal__':str(v)}
    return str(v)
print(json.dumps(rows,default=encode))
'''
        result = subprocess.run(['docker', 'exec', '-i', proof['containers']['dashboard'], 'python', '-c', program],
            input=json.dumps({'sql': sql, 'args': args, 'fixture': fixture, 'identity': self.state['postgres_system_identifier']}),
            cwd=ROOT, env=self.environment, capture_output=True, text=True, timeout=20)
        if result.returncode:
            # Raw stderr may contain SQL, payloads or connection credentials.
            # Retain only the structured fields emitted by our owned client.
            failure = {'query_sha256': hashlib.sha256(sql.encode()).hexdigest(), 'fixture': fixture}
            try:
                detail = json.loads(getattr(result, 'stderr', ''))
                for key, pattern in (('error_type', r'[A-Za-z][A-Za-z0-9_]{0,79}'),
                                     ('sqlstate', r'[0-9A-Z]{5}'),
                                     ('phase', r'connect|identity|query|commit'),
                                     ('transaction_status', r'IDLE|ACTIVE|INTRANS|INERROR|UNKNOWN')):
                    value = detail.get(key)
                    if isinstance(value, str) and re.fullmatch(pattern, value):
                        failure[key] = value
            except (ValueError, TypeError, AttributeError):
                pass
            self.report.setdefault('sql_failures', []).append(failure)
            self.save()
            raise IsolationError('Isolated PostgreSQL query failed; no external fallback attempted'
                                 + ('; ' + failure['error_type'] if 'error_type' in failure else ''))
        from decimal import Decimal
        def decode(value):
            if set(value) == {'__e2e_datetime__'}:
                return datetime.fromisoformat(value['__e2e_datetime__'])
            if set(value) == {'__e2e_decimal__'}:
                return Decimal(value['__e2e_decimal__'])
            return value
        return [tuple(row) for row in json.loads(result.stdout, object_hook=decode)]

    def sqlite(self, hospital, sql, args=(), *, fixture=False):
        require(hospital in ('hospital-a', 'hospital-b'), 'Unknown hospital')
        proof = self.fault_gate() if fixture else self.state.get('owned')
        require(proof is not None, 'SQLite evidence requires a proven owned container')
        require(fixture or sql.lstrip().upper().startswith('SELECT'), 'SQLite mutation requires explicit fixture mode')
        # Read-only connection URI is enforced for every evidence query.
        program = ('import sqlite3,os,json; p=os.environ["HOSPITAL_DB_PATH"]; '
                   + ('c=sqlite3.connect(p); ' if fixture else 'c=sqlite3.connect("file:"+p+"?mode=ro",uri=True); ')
                   + 'q,a=json.loads(input()); r=c.execute(q,a); '
                   + 'print(json.dumps(r.fetchall() if r.description else [])); c.commit(); c.close()')
        result = subprocess.run(['docker', 'exec', '-i', proof['containers'][hospital], 'python', '-c', program],
                                input=json.dumps([sql, args]), cwd=ROOT, env=self.environment,
                                capture_output=True, text=True, timeout=20)
        require(result.returncode == 0, 'Isolated SQLite query failed')
        return json.loads(result.stdout)

    def until(self, predicate, timeout=90, description='condition'):
        import httpx
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except (httpx.HTTPError, ConnectionError):
                pass
            time.sleep(.35)
        raise RuntimeError('Timed out waiting for ' + description)

    def configure(self, changes):
        self.fault_gate()
        require(set(changes) <= set(SERVICES), 'Unknown configuration target')
        previous = deepcopy(self.config)
        for service, settings in changes.items():
            self.config['services'][service]['environment'].update(settings)
        audit_config(self.config, self.project, self.nonce, self.state['baseline']['volumes'])
        verification.write_json(self.config_file, self.config)
        self.state['config_sha256'] = digest(self.config_file)
        self.save()
        # Recheck the existing runtime against the pre-change configuration;
        # the new environment becomes authoritative only after recreation.
        self._runtime_config = previous
        try:
            self.compose('up', '-d', '--no-build', '--pull', 'never', '--no-deps', '--wait', '--wait-timeout', '120', *changes)
        finally:
            self._runtime_config = None
        self.prove_ownership()

    @contextmanager
    def stopped(self, services):
        self.fault_gate()
        require(set(services) <= set(SERVICES), 'Unknown outage target')
        self.active_faults.append({'kind': 'stopped', 'services': list(services)})
        self.save()
        try:
            self.compose('stop', *services)
            yield
        finally:
            self.fault_gate()
            self.compose('start', *services)
            self.wait_ready(services)
            self.active_faults.pop()
            self.save()

    @contextmanager
    def disconnected(self, hospitals):
        proof = self.fault_gate()
        network = proof['networks'][self.config['networks']['default']['name']]
        disconnected = []
        self.active_faults.append({'kind': 'network partition', 'hospitals': list(hospitals)})
        self.save()
        try:
            for hospital in hospitals:
                require(hospital in ('hospital-a', 'hospital-b'), 'Only test hospitals can be partitioned')
                cid = proof['containers'][hospital]
                self.docker('network', 'disconnect', network, cid)
                disconnected.append((hospital, cid))
            yield
        finally:
            for hospital, cid in disconnected:
                self.fault_gate()
                self.docker('network', 'connect', '--alias', hospital, network, cid)
            if self.active_faults:
                self.active_faults.pop()
            if disconnected:
                self.wait_ready(hospitals)
            self.save()

    def restore_faults(self):
        proof = self.fault_gate(allow_missing=True)
        while self.active_faults:
            fault = self.active_faults[-1]
            if fault['kind'] == 'stopped':
                require(set(fault['services']) <= set(proof['containers']), 'Cannot restore unknown service')
                self.compose('start', *fault['services'])
                self.wait_ready(fault['services'])
            elif fault['kind'] == 'network partition':
                name = self.config['networks']['default']['name']
                network = proof['networks'][name]
                for hospital in fault['hospitals']:
                    require(hospital in ('hospital-a', 'hospital-b'), 'Invalid saved partition target')
                    cid = proof['containers'][hospital]
                    row = self.inspect('container', [cid])[0]
                    if name not in row['NetworkSettings']['Networks']:
                        self.docker('network', 'connect', '--alias', hospital, network, cid)
                self.wait_ready(fault['hospitals'])
            elif fault['kind'] == 'test proxy':
                require(set(fault['controls']) <= {'drop_ack_for', 'drop_cloud_for', 'hold_cloud_for'}, 'Unknown saved proxy control')
                self.http('fault-proxy', 'POST', '/__faults', {key: None for key in fault['controls']})
            elif fault['kind'] == 'hospital SIGKILL':
                require(fault['container_id'] == proof['containers'].get('hospital-a'), 'Unknown saved crash target')
                self.docker('start', fault['container_id'])
                self.wait_ready(['hospital-a'])
            else:
                raise IsolationError('Unknown saved fault: restoration requires review')
            self.active_faults.pop()
            self.save()
        self.record('Saved fault restoration', 'PASS', 'Every persisted injected fault restored before further scenarios or deletion')

    def wait_ready(self, services=SERVICES, timeout=120):
        def ready():
            ids = self.read_docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + self.project).splitlines()
            rows = self.inspect('container', ids)
            status = {r['Config']['Labels']['com.docker.compose.service']:
                      r['State'].get('Running') and r['State'].get('Health', {}).get('Status') == 'healthy' for r in rows}
            return all(status.get(service) for service in services)
        self.until(ready, timeout=timeout, description='owned service readiness')

    def collect(self):
        proof = self.prove_ownership()
        logs = self.directory / 'logs'
        logs.mkdir(exist_ok=True)
        for service, cid in proof['containers'].items():
            result = self.command(['docker', 'logs', '--tail', '1000', cid], allow_failure=True)
            lines = []
            for line in (result.stdout + result.stderr).splitlines():
                # Existing scheduler contract emits only IDs/priorities/policy,
                # and Uvicorn access logs emit method/path/status, never bodies.
                if any(marker in line for marker in ('scheduler.selection ', 'scheduler.outcome ',
                    'scheduler.admission_deferred ', 'Cloud unreachable; capture continues locally',
                    'Cloud reachable again; starting recovery', 'Backlog drained')) or re.search(r'"(?:GET|POST) /[^ ]* HTTP/', line):
                    lines.append(sanitized(line, self.secrets))
            (logs / (service + '.log')).write_text('\n'.join(lines), encoding='utf-8')
            if result.returncode:
                self.record('Log capture: ' + service, 'FAIL', 'Docker logs command failed')
        self.report['active_faults'] = list(self.active_faults)
        self.report['unresolved_grants'] = [list(r) for r in self.query("SELECT grant_id::text,hospital_id,event_id,status FROM controller_grants WHERE status IN ('issued','acknowledged','reconciliation_required') ORDER BY event_id")]
        self.report['unresolved_events'] = {}
        for hospital in ('hospital-a', 'hospital-b'):
            self.report['unresolved_events'][hospital] = self.sqlite(hospital,
                "SELECT e.event_id,e.status,COALESCE(r.quarantined,0),COALESCE(r.retry_count,0) FROM events e LEFT JOIN recovery_state r ON e.event_id=r.event_id WHERE e.status<>'applied' ORDER BY e.event_id")
        self.verify_preservation()
        self.save()

    def verify_preservation(self):
        # Incomplete historical baselines are never rewritten by a resume probe.
        validate_baseline(self.state.get('baseline'))
        require(not self.state.get('preservation_block'), 'Preservation failure is latched; investigation required')
        try:
            changes = preservation_changes(self.state['baseline'], self.inventory())
            if any(changes.values()):
                raise DockerGuardError('Docker engine or original baseline resources changed; investigation required')
        except (DockerGuardError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            reason = sanitized(str(exc), self.secrets)
            self.state['preservation_block'] = {'reason': reason, 'at_utc': datetime.now(timezone.utc).isoformat()}
            self.report['preservation'] = {'status': 'FAIL', 'investigation_required': True,
                                           'changes': locals().get('changes'), 'reason': reason}
            self.record('Existing-resource preservation inventory', 'FAIL', reason)
            raise IsolationError(reason) from exc
        self.report['preservation'] = {'status': 'PASS', 'all_baseline_resources_unchanged': True,
            'engine': self.state['baseline']['engine'],
            'scope': 'Live daemon identity and original containers/mounts/network attachments, volumes and networks; not database-content equality'}
        return True

    def cleanup(self):
        proof = self.fault_gate(allow_missing=True)
        require(not self.active_faults, 'Restore all injected faults before cleanup')
        # Preserve failed evidence and unresolved records BEFORE deleting anything.
        if set(proof['containers']) == set(SERVICES) and self.state.get('postgres_system_identifier'):
            self.collect()
        else:
            self.report['partial_cleanup'] = 'Incomplete startup: preserved existing logs/report; SQL evidence unavailable'
        self.report['phase'] = 'cleanup'
        self.save()
        for cid in proof['containers'].values():
            row = self.inspect('container', [cid])[0]
            require(row['Config'].get('Labels', {}).get(LABEL) == self.nonce and cid not in self.state['baseline']['containers'],
                    'Container ownership changed before deletion')
            if row['State']['Running']:
                self.docker('stop', '--time', '15', cid)
            self.docker('rm', cid)
        for network_id in proof['networks'].values():
            row = self.inspect('network', [network_id])[0]
            require(row.get('Labels', {}).get(LABEL) == self.nonce and not row.get('Containers')
                    and network_id not in self.state['baseline']['networks'], 'Network ownership changed before deletion')
            self.docker('network', 'rm', network_id)
        for name in proof['volumes']:
            row = self.inspect('volume', [name])[0]
            require(row.get('Labels', {}).get(LABEL) == self.nonce and name not in self.state['baseline']['volumes'],
                    'Volume ownership changed before deletion')
            self.docker('volume', 'rm', name)
        remaining = self.docker('ps', '-aq', '--filter', 'label=' + LABEL + '=' + self.nonce)
        require(not remaining, 'Owned containers remain after cleanup')
        self.verify_preservation()
        self.report['phase'] = 'complete'
        self.report['cleanup'] = {'status': 'PASS', 'containers_removed': list(proof['containers'].values()),
                                  'volumes_removed': proof['volumes'], 'image_retained': self.state['image']}
        self.record('Owned-resource cleanup', 'PASS', 'Evidence saved first; unique image retained; no unrelated resource deleted')


def create_stack():
    nonce = uuid.uuid4().hex
    project = 'cg-e2e-' + nonce[:24]
    directory = ROOT / '.project-test-results' / ('e2e-' + nonce)
    directory.mkdir(parents=True, exist_ok=False)
    settings = dict(zip(PORT_KEYS, map(str, free_ports(len(PORT_KEYS)))))
    settings.update(POSTGRES_USER='e2e_' + nonce[:12], POSTGRES_DB='e2e_' + nonce[:12],
        POSTGRES_PASSWORD=secrets.token_hex(32), CONTROLLER_CLOUD_TOKEN=secrets.token_hex(32),
        HOSPITAL_A_CONTROLLER_TOKEN=secrets.token_hex(32), HOSPITAL_B_CONTROLLER_TOKEN=secrets.token_hex(32),
        CLOUD_LIVE_TELEMETRY_TOKEN=secrets.token_hex(32), CLOUD_REPLAY_TELEMETRY_TOKEN=secrets.token_hex(32),
        MANUAL_REPLAY_TOKEN=secrets.token_hex(32), DASHBOARD_REPLAY_OPERATOR_TOKEN=secrets.token_hex(32),
        AUTO_REPLAY='0', ENABLE_MANUAL_REPLAY='1', CONTROLLER_ADMISSION_ENABLED='0',
        CLOUD_LIVE_SLO_MS='250')
    ports = dict(zip(SERVICES, map(int, (settings[k] for k in PORT_KEYS))))
    revision = verification.repository_revision()
    state = {'version': 1, 'nonce': nonce, 'project': project, 'settings': settings, 'ports': ports,
             'image': 'crisisguard-e2e:' + nonce, 'directory': str(directory)}
    report = {'revision': revision, 'created_utc': datetime.now(timezone.utc).isoformat(),
              'phase': 'preflight', 'project': project, 'run': nonce, 'checks': [], 'http': [], 'events': [],
              'aws_used': False, 'confirmation_required': True,
              'source_hashes': {p.relative_to(ROOT).as_posix(): digest(p) for base in ('cloud', 'edge', 'dashboard')
                                for p in (ROOT / base).rglob('*') if p.suffix in ('.py', '.js', '.html', '.css')}}
    stack = Stack(directory, state, report)
    stack.save()
    return stack


def provision(stack):
    baseline = stack.capture_baseline()
    require(not stack.read_docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + stack.project), 'Project already exists')
    require(not stack.read_docker('image', 'ls', '-q', stack.state['image']), 'Image tag already exists')
    env_file = stack.directory / 'settings.private.env'
    env_file.write_text(''.join(f'{k}={v}\n' for k, v in stack.settings.items()), encoding='utf-8')
    os.chmod(env_file, 0o600)
    resolved = stack.docker('compose', '--env-file', str(env_file), '-p', stack.project,
            '-f', str(ROOT / 'compose.yaml'), '-f', str(ROOT / 'infrastructure/docker/compose.metaguard.yaml'),
            '--profile', 'metaguard', 'config', '--format', 'json')
    config = json.loads(resolved)
    for key in list(config):
        if key.startswith('x-'):
            del config[key]
    for values in config['volumes'].values():
        values['labels'] = {LABEL: stack.nonce}
    config['networks']['default']['internal'] = True
    config['networks']['control'] = {'name': stack.project + '_control', 'internal': True}
    for values in config['networks'].values():
        values['labels'] = {LABEL: stack.nonce}
    for name, service in config['services'].items():
        service.pop('build', None)
        service['labels'] = {LABEL: stack.nonce}
        service['restart'] = 'no'
        if name != 'postgres':
            service['image'] = stack.state['image']
            service['pull_policy'] = 'never'
        if name in ('hospital-a', 'hospital-b', 'dashboard'):
            service['networks'] = {'default': {}, 'control': {'priority': 1000, 'gw_priority': 1}}
        if name in ('hospital-a', 'hospital-b'):
            service['environment'].update(CLOUD_URL='http://fault-proxy:8000/patient/events',
                CONTROLLER_URL='http://fault-proxy:8000/controller', AUTO_REPLAY_INTERVAL_SECONDS='.5')
        if name == 'dashboard':
            service['environment'].update(ENABLE_MANUAL_REPLAY='1', MANUAL_REPLAY_TOKEN=stack.settings['MANUAL_REPLAY_TOKEN'],
                DASHBOARD_REPLAY_OPERATOR_TOKEN=stack.settings['DASHBOARD_REPLAY_OPERATOR_TOKEN'],
                CLOUD_REPLAY_TELEMETRY_TOKEN=stack.settings['CLOUD_REPLAY_TELEMETRY_TOKEN'],
                DASHBOARD_TEST_TRAFFIC_ENABLED='1', CRISISGUARD_DEPLOYMENT_MODE='local-development')
    proxy_source = (ROOT / 'infrastructure/scripts/metaguard_fault_proxy.py').read_text(encoding='utf-8')
    config['services']['fault-proxy'] = {'image': stack.state['image'], 'pull_policy': 'never',
        'labels': {LABEL: stack.nonce}, 'command': ['python', '-c', proxy_source],
        'networks': {'default': {}}, 'ports': [{'host_ip': '127.0.0.1', 'target': 8000,
            'published': stack.settings['FAULT_PROXY_PORT'], 'protocol': 'tcp'}],
        'healthcheck': {'test': ['CMD', 'python', '-c', "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"],
            'interval': '2s', 'timeout': '3s', 'retries': 30}}
    baseline_network_names = [n['name'] for n in baseline['networks'].values()]
    audit_config(config, stack.project, stack.nonce, baseline['volumes'], baseline_network_names)
    stack.config = config
    verification.write_json(stack.config_file, config)
    stack.state['config_sha256'] = digest(stack.config_file)
    stack.save()
    # Build the existing Dockerfile, never install host dependencies or reuse an
    # existing deployment image tag. The image remains uniquely tagged after cleanup.
    stack._provisioning = True
    try:
        stack.docker('build', '-f', str(ROOT / 'infrastructure/docker/Dockerfile'), '-t', stack.state['image'], str(ROOT), timeout=600)
        stack.state['app_image_id'] = stack.read_docker('image', 'inspect', '--format', '{{.Id}}', stack.state['image'])
        stack.state['postgres_image_id'] = stack.read_docker('image', 'inspect', '--format', '{{.Id}}', 'postgres:16')
        stack.save()
        stack.compose('up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180', timeout=220)
    finally:
        stack._provisioning = False
    return establish_database(stack)


def establish_database(stack):
    proof = stack.prove_ownership()
    identity = stack.docker('exec', proof['containers']['postgres'], 'psql', '-X', '-U',
                stack.settings['POSTGRES_USER'], '-d', stack.settings['POSTGRES_DB'], '-Atc',
                'SELECT system_identifier::text FROM pg_control_system()')
    stack.state['postgres_system_identifier'] = identity
    stack.save()
    require(stack.query('SELECT current_database()') == [(stack.settings['POSTGRES_DB'],)], 'Unexpected test database')
    require(stack.query('SELECT count(*) FROM events') == [(0,)], 'New test ledger is not empty')
    return {'detail': 'Fresh eight-service system plus test-only proxy; internal networks, unique volumes/ports/credentials',
            'postgres_system_identifier': identity}


def load_stack(path):
    directory = Path(path).resolve()
    results = (ROOT / '.project-test-results').resolve()
    require(directory.parent == results and re.fullmatch(r'e2e-[a-f0-9]{32}', directory.name), 'Resume path must identify an E2E results directory')
    state = json.loads((directory / 'session.private.json').read_text(encoding='utf-8'))
    require(state['version'] == 1 and state['nonce'] == directory.name[4:]
            and state['project'] == 'cg-e2e-' + state['nonce'][:24], 'Resume manifest identity mismatch')
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    validate_baseline(state.get('baseline'))
    require(not state.get('preservation_block') and not any(c.get('status') == 'FAIL'
            and c.get('name') == 'Existing-resource preservation inventory' for c in report.get('checks', [])),
            'Retained preservation failure requires investigation; resume and cleanup blocked')
    require(report['phase'] != 'complete', 'This disposable run has already completed')
    return Stack(directory, state, report)


def prompt_confirmation(stack):
    if not sys.stdin.isatty():
        return None
    print('Review: ' + str(stack.directory / 'isolation.json'), flush=True)
    try:
        return input('Type ' + stack.project + ':' + stack.nonce +
                     ' to confirm isolated faults, restoration and cleanup: ').strip() or None
    except EOFError:
        # Some Windows task hosts report a TTY but immediately close stdin.
        # Missing input is a pending gate, never consent and never a test failure.
        return None


def run_e2e(*, resume=None, confirmation=None, cleanup_only=False):
    try:
        stack = load_stack(resume) if resume else create_stack()
    except (DockerGuardError, IsolationError) as exc:
        print('E2E BLOCKED: ' + str(exc), flush=True)
        # Do not append to or rebase a rejected historical run's evidence.
        return 1, Path(resume).resolve() if resume else ROOT / '.project-test-results'
    stack.report.setdefault('runner_revisions', []).append({
        'at_utc': datetime.now(timezone.utc).isoformat(),
        'hashes': {name: digest(ROOT / name) for name in ('test.project.ps1',
            'infrastructure/scripts/project_e2e.py', 'infrastructure/scripts/project_e2e_scenarios.py',
            'infrastructure/scripts/project_docker_guard.py', '.dockerignore')}})
    print('CRISISGUARD REAL SYSTEM E2E: ' + stack.project, flush=True)
    from infrastructure.scripts.project_e2e_scenarios import smoke, comprehensive
    try:
        if not resume:
            if not stack.scenario('Isolation and real service provisioning', lambda: provision(stack)):
                stack.report['phase'] = 'blocked before smoke'
                stack.save()
                return 1, stack.directory
            if not stack.scenario('All event types: both hospitals to Cloud/SQL/Dashboard', lambda: smoke(stack)):
                stack.report['phase'] = 'smoke failed; faults blocked'
                stack.collect()
                stack.save()
                return 1, stack.directory
            stack.collect()
            stack.report['phase'] = 'awaiting isolation confirmation'
            stack.record('Browser interaction/accessibility/loading visuals', 'NOT VERIFIED', 'No browser automation performed')
            stack.record('Per-event Cloud request/grant log correlation', 'NOT VERIFIED', 'SQL/audit/request evidence exists; Cloud access logs do not carry event IDs')
        else:
            stack.prove_ownership(allow_missing=cleanup_only)
            smoke_passed = any(c['name'] == 'All event types: both hospitals to Cloud/SQL/Dashboard' and c['status'] == 'PASS'
                               for c in stack.report['checks'])
            if not smoke_passed and not cleanup_only:
                stack.wait_ready()
                if not stack.state.get('postgres_system_identifier'):
                    stack.record('Resumed isolation proof and empty database', 'PASS', **establish_database(stack))
                stack.report['phase'] = 'resumed safe smoke'
                if not stack.scenario('All event types: both hospitals to Cloud/SQL/Dashboard', lambda: smoke(stack)):
                    stack.report['phase'] = 'smoke failed; faults blocked'
                    stack.save()
                    return 1, stack.directory
                stack.collect()
                stack.report['phase'] = 'awaiting isolation confirmation'
        token = stack.project + ':' + stack.nonce
        if not confirmation:
            confirmation = prompt_confirmation(stack)
        if not confirmation:
            stack.report['confirmation_required'] = True
            stack.report['phase'] = 'awaiting isolation confirmation'
            stack.report['cleanup'] = {'status': 'NOT VERIFIED', 'detail': 'Resources retained pending explicit isolation confirmation'}
            existing_names = {c['name'] for c in stack.report['checks']}
            for name in (*CONFIRMATION_PLACEHOLDERS, 'Browser interaction/accessibility/loading visuals'):
                if name not in existing_names:
                    stack.record(name, 'NOT VERIFIED', 'Awaiting isolation confirmation' if 'Browser' not in name else 'No browser automation performed')
            stack.save()
            print('Isolation manifest: ' + str(stack.directory / 'isolation.json'), flush=True)
            print('Explicit confirmation required before fault injection or cleanup.', flush=True)
            print('.\\test.project.ps1 -EndToEnd -E2EResume "' + str(stack.directory) + '" -ConfirmIsolation "' + token + '"', flush=True)
            return 2, stack.directory
        stack.confirm(confirmation, allow_missing=cleanup_only)
        stack.supersede_confirmation_placeholders(cleanup_only=cleanup_only)
        if stack.active_faults:
            stack.restore_faults()
        if not cleanup_only:
            require(stack.report['phase'] != 'smoke failed; faults blocked', 'A failed smoke must be repaired before fault scenarios')
            stack.report['phase'] = 'confirmed comprehensive scenarios'
            comprehensive(stack)
        stack.cleanup()
    except Exception as exc:
        stack.record('E2E orchestration', 'FAIL', sanitized(str(exc), stack.secrets)[:500], error_type=type(exc).__name__)
        stack.report['phase'] = 'failed; resources retained'
        stack.report['active_faults'] = list(stack.active_faults)
        stack.save()
        return 1, stack.directory
    finally:
        stack.save()
        print('E2E report: ' + str(stack.directory / 'SUMMARY.md'), flush=True)
    return (1 if any(c['status'] == 'FAIL' for c in stack.report['checks']) else 0), stack.directory
