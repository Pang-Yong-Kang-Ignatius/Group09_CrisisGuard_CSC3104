"""Run one bounded AIMD transition check on a disposable, empty Docker host.

Usage: python3 docs/evidence/cp7b-aws-adaptive-transitions-20261029.py \
    --revision <git-sha> --output /home/ubuntu/cp7b-aws-transitions.json
The source tree must be the archive of that revision. No production URLs or
credentials are used; all services, data, and telemetry manipulation are local.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
PROXY = r'''from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import urllib.request

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        name = self.path.removeprefix('/metrics/')
        if name not in ('patient', 'emergency', 'admin'):
            self.send_error(404)
            return
        try:
            with urllib.request.urlopen('http://' + name + ':8000/metrics', timeout=2) as response:
                sample = json.load(response)
            mode = Path('/tmp/metrics_mode').read_text().strip()
            if name == 'patient' and mode == 'caution':
                sample['average_latency_ms'] = 300.0
            if name == 'patient' and mode == 'stale':
                sample['sampled_at'] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
            body = json.dumps(sample).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_error(503)

ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
'''


def run(*args, capture=False, timeout=600, env=None):
    result = subprocess.run(args, cwd=ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args[:3])} failed ({result.returncode}): {result.stderr[-800:]}")
    return result.stdout.strip() if capture else None


def api(method, port, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data,
                                     method=method, headers={'X-Cloud-Token': token,
                                                             'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists')
    project = 'cgaws-' + secrets.token_hex(4)
    work = Path(tempfile.mkdtemp(prefix=project + '-', dir='/home/ubuntu'))
    config_path = work / 'compose.json'
    env_path = work / 'test.env'
    mode_path = work / 'metrics_mode'
    proxy_path = work / 'metrics_proxy.py'
    output = {'schema': 'cp7b-aws-aimd-transitions-v1', 'revision': args.revision,
              'project': project, 'started_at': datetime.now(timezone.utc).isoformat(),
              'isolated': True, 'synthetic_data_only': True, 'snapshots': [], 'grants': []}
    port = 18005
    token = secrets.token_hex(32)
    image = 'crisisguard-cp7b-aws:' + project

    def compose(*parts, timeout=600):
        return run('docker', 'compose', '-p', project, '-f', str(config_path), *parts,
                   timeout=timeout)

    def snapshot(label):
        status = api('GET', port, '/admission/status', token)
        record = {'label': label, 'at': datetime.now(timezone.utc).isoformat()}
        for key in ('policy', 'state', 'reason', 'rate_rps', 'telemetry_fresh',
                    'observed_at', 'server_time', 'allowed', 'denied', 'last_decision'):
            record[key] = status.get(key)
        record['metrics'] = {name: {'sampled_at': sample.get('sampled_at'),
                                    'average_latency_ms': sample.get('average_latency_ms')}
                             for name, sample in status.get('metrics', {}).items()
                             if isinstance(sample, dict)}
        output['snapshots'].append(record)
        return record

    def wait_for(label, predicate, timeout=90):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                row = snapshot(label)
                if predicate(row):
                    return row
            except Exception:
                pass
            time.sleep(1)
        raise AssertionError('Timed out waiting for ' + label)

    try:
        settings = {
            'POSTGRES_USER': 'cp7b', 'POSTGRES_DB': 'cp7b',
            'POSTGRES_PASSWORD': secrets.token_hex(24),
            'CONTROLLER_CLOUD_TOKEN': token,
            'HOSPITAL_A_CONTROLLER_TOKEN': secrets.token_hex(32),
            'HOSPITAL_B_CONTROLLER_TOKEN': secrets.token_hex(32),
            'CLOUD_LIVE_TELEMETRY_TOKEN': secrets.token_hex(32),
            'CLOUD_REPLAY_TELEMETRY_TOKEN': secrets.token_hex(32),
            'POSTGRES_PORT': '15433', 'CONTROLLER_PORT': str(port),
            'PATIENT_PORT': '18001', 'EMERGENCY_PORT': '18003',
            'ADMIN_PORT': '18004', 'HOSPITAL_A_PORT': '18006',
            'HOSPITAL_B_PORT': '18007', 'DASHBOARD_PORT': '18008',
            'CONTROLLER_ADMISSION_POLICY': 'aimd',
            'CONTROLLER_ADMISSION_ENABLED': '1',
            'CONTROLLER_ADMISSION_POLL_SECONDS': '1',
            'CONTROLLER_ADMISSION_MAX_AGE_SECONDS': '5',
            'CONTROLLER_ADMISSION_TIMEOUT_SECONDS': '1',
            'CONTROLLER_ADMISSION_DWELL_SECONDS': '3',
            'CONTROLLER_ADMISSION_HEALTHY_SAMPLES': '2',
            'CONTROLLER_ADMISSION_ADDITIVE_RPS': '0.2',
        }
        output['admission_config'] = {key: value for key, value in settings.items()
                                      if key.startswith('CONTROLLER_ADMISSION_')}
        output['telemetry_faults'] = {
            'caution': 'Patient average_latency_ms set to 300 in fresh real /metrics response',
            'stale': 'Patient sampled_at set 30 seconds behind in real /metrics response',
        }
        env_path.write_text(''.join(f'{key}={value}\n' for key, value in settings.items()))
        env_path.chmod(0o600)
        mode_path.write_text('normal')
        proxy_path.write_text(PROXY)
        run('docker', 'build', '-q', '-t', image, '-f',
            str(ROOT / 'infrastructure/docker/Dockerfile'), str(ROOT), timeout=900)
        command = ['docker', 'compose', '--env-file', str(env_path), '-p', project]
        for path in ('compose.yaml', 'compose.override.yaml',
                     'infrastructure/docker/compose.metaguard.yaml'):
            if (ROOT / path).exists():
                command += ['-f', str(ROOT / path)]
        command += ['--profile', 'metaguard', 'config', '--format', 'json']
        clean_env = {key: value for key, value in os.environ.items()
                     if not key.startswith('COMPOSE_') and key not in settings}
        resolved = json.loads(run(*command, capture=True, env=clean_env))
        chosen = ('postgres', 'controller', 'patient', 'emergency', 'admin')
        resolved['services'] = {name: resolved['services'][name] for name in chosen}
        for name, service in resolved['services'].items():
            service.pop('profiles', None)
            if name != 'postgres':
                service['image'] = image
                service['pull_policy'] = 'never'
                service.pop('build', None)
        controller = resolved['services']['controller']
        assert controller['environment']['CONTROLLER_ADMISSION_POLICY'] == 'aimd'
        for name in ('patient', 'emergency', 'admin'):
            controller['environment']['CONTROLLER_' + name.upper() + '_METRICS_URL'] = (
                'http://telemetry-proxy:8000/metrics/' + name)
        resolved['services']['telemetry-proxy'] = {
            'image': image, 'pull_policy': 'never',
            'command': ['python', '-u', '/tmp/metrics_proxy.py'],
            'volumes': [str(proxy_path) + ':/tmp/metrics_proxy.py:ro',
                        str(mode_path) + ':/tmp/metrics_mode:ro'],
            'networks': controller['networks'],
        }
        config_path.write_text(json.dumps({key: value for key, value in resolved.items()
                                           if not key.startswith('x-')}))
        config_path.chmod(0o600)
        env_path.unlink()
        compose('config', '--quiet')
        compose('up', '-d', '--pull', 'missing', '--wait', '--wait-timeout', '180',
                'postgres', 'patient', 'emergency', 'admin', 'telemetry-proxy', 'controller',
                timeout=360)
        normal = wait_for('initial_normal', lambda row: row['policy'] == 'aimd' and
                          row['state'] == 'Normal' and row['telemetry_fresh'] and
                          row['rate_rps'] >= 1.99)
        mode_path.write_text('caution')
        caution = wait_for('caution', lambda row: row['state'] == 'Caution' and
                           row['telemetry_fresh'] and row['rate_rps'] < normal['rate_rps'], 20)
        mode_path.write_text('stale')
        stale = wait_for('stale_recovery', lambda row: row['state'] == 'Recovery' and
                         row['telemetry_fresh'] is False, 20)
        api('POST', port, '/tickets', token,
            {'hospital_id': 'Hospital_A', 'event_id': 'AWS-SYNTHETIC-A', 'priority': 1})
        denied = api('POST', port, '/grants/next', token)
        stale_denial = snapshot('stale_denial')
        assert denied.get('message') == 'No eligible hospitals'
        assert stale_denial['denied'] > stale['denied']
        assert stale_denial['last_decision']['reason'] == 'telemetry_unavailable'
        mode_path.write_text('normal')
        wait_for('recovery_to_caution', lambda row: row['state'] == 'Caution' and
                 row['telemetry_fresh'], 30)
        recovered = wait_for('caution_to_normal', lambda row: row['state'] == 'Normal' and
                             row['telemetry_fresh'] and row['rate_rps'] >= 1.99, 90)
        api('POST', port, '/tickets', token,
            {'hospital_id': 'Hospital_B', 'event_id': 'AWS-SYNTHETIC-B', 'priority': 1})
        first = api('POST', port, '/grants/next', token)
        assert first.get('event_id') == 'AWS-SYNTHETIC-A'
        first_at = time.monotonic()
        paced = api('POST', port, '/grants/next', token)
        pacing = snapshot('rate_limit')
        assert paced.get('message') == 'No eligible hospitals'
        assert pacing['last_decision']['reason'] == 'rate_limit'
        second = wait_for('second_grant_ready', lambda row: time.monotonic() - first_at >= .55, 5)
        second_grant = api('POST', port, '/grants/next', token)
        assert second_grant.get('event_id') == 'AWS-SYNTHETIC-B'
        output['grants'] = [{'event_id': first['event_id'], 'outcome': 'issued'},
                            {'event_id': 'AWS-SYNTHETIC-B', 'outcome': 'paced_then_issued'}]
        output['checks'] = {'normal': normal['rate_rps'], 'caution': caution['rate_rps'],
                            'stale_recovery': stale['rate_rps'],
                            'recovered_normal': recovered['rate_rps'],
                            'stale_denial': True, 'pacing_denial': True,
                            'allowed_before_second_grant': second['allowed'],
                            'denied_before_second_grant': pacing['denied']}
        output['result'] = 'passed'
    except Exception as exc:
        output['result'] = 'failed'
        output['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if config_path.exists():
            try:
                compose('down', '-v', '--remove-orphans', timeout=120)
                output['compose_cleanup'] = 'passed'
            except Exception as exc:
                output['compose_cleanup'] = f'failed: {type(exc).__name__}'
        output['finished_at'] = datetime.now(timezone.utc).isoformat()
        args.output.write_text(json.dumps(output, indent=2, default=str))
        print(f"Result: {output['result']}; evidence: {args.output}", flush=True)


if __name__ == '__main__':
    main()
