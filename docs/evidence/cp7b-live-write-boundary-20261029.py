"""One disposable grant-enforced Cloud write/provenance characterization.

Usage: python docs/evidence/cp7b-live-write-boundary-20261029.py --output PATH
Only a uniquely named Compose project, image and volumes are created. Secrets
remain in a Git-ignored temporary directory and never enter the evidence.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid

import httpx
import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from cloud.metaguard import event_digest


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, HERE / path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


fair = module('cp7b_fairness_reuse', 'cp7b-sustained-fairness-experiment-20261029.py')
base = fair.base
ROUTES = {'patient': 'patient_registration', 'emergency': 'emergency_record',
          'admin': 'patient_discharge'}


def percentile(values):
    values = sorted(values)
    return round(values[(95 * len(values) + 99) // 100 - 1], 3) if values else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists')
    project = 'cg-livewrite-' + uuid.uuid4().hex[:8]
    image = 'crisisguard-livewrite:' + project
    work = Path(tempfile.mkdtemp(prefix=project + '-', dir=ROOT / 'backups'))
    runner = base.Runner(work, image, work / 'private-runner.json', 1)
    output = {'schema': 'cp7b-live-write-boundary-v1', 'started_at': base.utcnow(),
              'project': project, 'source_revision': runner.run(['git', 'rev-parse', 'HEAD'], capture=True),
              'isolated': True, 'observations': [], 'metrics': {}, 'result': 'running'}
    settings = fair.settings_for('aimd')
    settings.update(POSTGRES_USER='livewrite_eval', POSTGRES_DB='livewrite_eval',
                    AUTO_REPLAY='0', ENABLE_MANUAL_REPLAY='0')
    config = None
    original_containers = original_volumes = None

    def request(method, url, *, headers=None, body=None, label):
        start = time.perf_counter()
        response = httpx.request(method, url, headers=headers, json=body, timeout=8)
        elapsed = (time.perf_counter() - start) * 1000
        record = {'label': label, 'method': method, 'status_code': response.status_code,
                  'client_latency_ms': round(elapsed, 3)}
        output['observations'].append(record)
        return response, record

    def api(method, url, *, headers=None, body=None, label, expected=200):
        response, row = request(method, url, headers=headers, body=body, label=label)
        if response.status_code != expected:
            raise AssertionError(f'{label}: HTTP {response.status_code}, expected {expected}')
        return response.json(), row

    def controller(path, *, body=None, label, expected=200):
        return api('POST' if body is not None else 'GET',
                   control_url + path, headers={'X-Cloud-Token': settings['CONTROLLER_CLOUD_TOKEN']},
                   body=body, label=label, expected=expected)[0]

    def grant_for(event):
        controller('/tickets', body={'hospital_id': event['site_id'],
                   'event_id': event['event_id'], 'priority': event['base_priority'],
                   'event_digest': event_digest(event)}, label='register_ticket')
        until = time.monotonic() + 25
        while time.monotonic() < until:
            grant = controller('/grants/next', body={}, label='issue_grant')
            if grant.get('grant_id'):
                assert grant['event_id'] == event['event_id']
                break
            time.sleep(.55)
        else:
            raise AssertionError('Grant issuance timed out')
        while time.monotonic() < until:
            response, _ = request('POST', control_url + '/grants/' + grant['grant_id'] + '/acknowledge',
                                  headers={'X-Cloud-Token': settings['CONTROLLER_CLOUD_TOKEN']},
                                  body={'hospital_id': event['site_id']}, label='ack_grant')
            if response.status_code == 200:
                assert response.json()['status'] == 'acknowledged'
                return grant['grant_id']
            assert response.status_code == 429
            time.sleep(.55)
        raise AssertionError('Grant acknowledgement timed out')

    try:
        original_containers, original_volumes = runner.inventory()
        output['original_resource_counts'] = {'containers': len(original_containers),
                                              'volumes': len(original_volumes)}
        runner.run(['docker', 'build', '-q', '-t', image, '-f',
                    str(ROOT / 'infrastructure/docker/Dockerfile'), str(ROOT)], timeout=900)
        config, resolved = runner.resolve(project, work, settings)
        assert resolved['services']['patient']['environment']['METAGUARD_REQUIRED'] == '1'
        assert resolved['services']['controller']['environment']['CONTROLLER_ADMISSION_POLICY'] == 'aimd'
        runner.compose_call(project, config, 'up', '-d', '--no-build', '--pull', 'never', '--wait',
                            '--wait-timeout', '180', 'postgres', 'controller', 'patient', 'emergency', 'admin')
        urls = {service: f"http://127.0.0.1:{settings[service.upper() + '_PORT']}" for service in ROUTES}
        control_url = f"http://127.0.0.1:{settings['CONTROLLER_PORT']}"
        operator = {'X-Cloud-Token': settings['CONTROLLER_CLOUD_TOKEN']}
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            status = httpx.get(control_url + '/admission/status', headers=operator, timeout=5).json()
            if status['state'] == 'Normal' and status['telemetry_fresh']:
                break
            time.sleep(1)
        else:
            raise AssertionError('Controller did not reach fresh Normal state')
        output['controller'] = {'policy': status['policy'], 'state': status['state'],
                                'telemetry_fresh': status['telemetry_fresh']}

        prefix = 'SYN-LIVEWRITE-' + uuid.uuid4().hex[:6]
        events = {service: {'event_id': f'{prefix}-{service}',
                            'site_id': 'Hospital_A' if service != 'emergency' else 'Hospital_B',
                            'event_type': kind, 'base_priority': 2,
                            'prerequisite_ids': [], 'payload': {'synthetic': True, 'service': service}}
                  for service, kind in ROUTES.items()}
        live = {'X-CrisisGuard-Traffic': 'live',
                'X-CrisisGuard-Telemetry-Token': settings['CLOUD_LIVE_TELEMETRY_TOKEN']}
        replay = {'X-CrisisGuard-Traffic': 'replay',
                  'X-CrisisGuard-Telemetry-Token': settings['CLOUD_REPLAY_TELEMETRY_TOKEN']}
        denied_event = dict(events['patient'], event_id=prefix + '-denied')
        response, _ = api('POST', urls['patient'] + '/events', headers=live,
                          body=denied_event, label='live_write_no_hospital_token', expected=401)
        assert response.get('detail') == 'Hospital credentials required'
        a_token = settings['HOSPITAL_A_CONTROLLER_TOKEN']
        response, _ = api('POST', urls['patient'] + '/events',
                          headers={**live, 'Authorization': 'Bearer ' + a_token},
                          body=denied_event, label='live_write_no_grant', expected=403)
        assert response.get('detail') == 'Valid MetaGuard grant required'

        for service, event in events.items():
            grant_id = grant_for(event)
            hospital_token = settings['HOSPITAL_A_CONTROLLER_TOKEN'] if event['site_id'] == 'Hospital_A' else settings['HOSPITAL_B_CONTROLLER_TOKEN']
            # The live-labelled grant-bearing request is deliberately contradictory.
            kind_headers = live if service == 'emergency' else replay
            headers = {**kind_headers, 'Authorization': 'Bearer ' + hospital_token,
                       'X-MetaGuard-Grant': grant_id}
            body, _ = api('POST', urls[service] + '/events', headers=headers, body=event,
                          label=service + '_authorized_write', expected=201)
            assert body['result'] == 'applied'
            if service == 'patient':
                body, _ = api('POST', urls[service] + '/events', headers=headers, body=event,
                              label='patient_identical_retry')
                assert body['result'] == 'already_applied'
            completed = controller('/grants/' + grant_id + '/complete',
                                   body={'hospital_id': event['site_id']}, label='complete_grant')
            assert completed['status'] == 'completed'

        # Small alternating mixture; no offered-rate or capacity claim.
        for service, event in events.items():
            for index in range(4):
                for kind, headers in (('live', live), ('replay', replay)):
                    body, _ = api('GET', urls[service] + '/events/' + event['event_id'],
                                  headers=headers, label=f'{service}_{kind}_read_{index}')
                    assert body['event_id'] == event['event_id'] and body['status'] == 'applied'

        for service, url in urls.items():
            metric = httpx.get(url + '/metrics', timeout=8).json()
            output['metrics'][service] = {key: metric.get(key) for key in (
                'sampled_at', 'live_p95_latency_ms', 'live_latency_sample_count',
                'replay_latency_sample_count', 'unclassified_latency_sample_count',
                'p95_latency_ms', 'latency_sample_count', 'average_latency_ms',
                'error_rate', 'failed_requests', 'total_requests', 'window_requests',
                'executor_queue_depth', 'executor_active_workers', 'executor_worker_limit',
                'executor_queue_pressure', 'database_healthy')}
            assert metric['database_healthy'] and metric['failed_requests'] == 0
            assert metric['executor_queue_depth'] >= 0 and metric['executor_worker_limit'] > 0
        assert output['metrics']['patient']['live_latency_sample_count'] == 6
        assert output['metrics']['patient']['replay_latency_sample_count'] == 6
        assert output['metrics']['patient']['unclassified_latency_sample_count'] == 0
        assert output['metrics']['emergency']['live_latency_sample_count'] == 4
        assert output['metrics']['emergency']['replay_latency_sample_count'] == 4
        assert output['metrics']['emergency']['unclassified_latency_sample_count'] == 1
        assert output['metrics']['admin']['live_latency_sample_count'] == 4
        assert output['metrics']['admin']['replay_latency_sample_count'] == 5
        assert output['metrics']['admin']['unclassified_latency_sample_count'] == 0

        db_url = (f"postgresql://{settings['POSTGRES_USER']}:{settings['POSTGRES_PASSWORD']}"
                  f"@127.0.0.1:{settings['POSTGRES_PORT']}/{settings['POSTGRES_DB']}")
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            rows = conn.execute('SELECT event_id,site_id,event_type,status FROM events ORDER BY event_id').fetchall()
            grants = conn.execute('SELECT event_id,status FROM controller_grants ORDER BY event_id').fetchall()
            tickets = conn.execute('SELECT count(*) AS n FROM controller_tickets').fetchone()['n']
        assert {row['event_id'] for row in rows} == {event['event_id'] for event in events.values()}
        assert len(rows) == 3 and all(row['status'] == 'applied' for row in rows)
        assert len(grants) == 3 and all(row['status'] == 'completed' for row in grants)
        assert tickets == 0 and denied_event['event_id'] not in {row['event_id'] for row in rows}
        output['database'] = {'events': rows, 'grants': grants, 'tickets': tickets,
                              'unauthorized_event_absent': True, 'duplicate_rows': False}
        output['client_p95_ms'] = {label: percentile([r['client_latency_ms'] for r in output['observations']
                                                    if r['label'].startswith(label)])
                                   for label in ('patient', 'emergency', 'admin', 'live_write')}
        output['finding'] = 'No successful grant-enforced write can enter live-only p95: a grant-bearing live-labelled write is unclassified; grantless live writes are rejected.'
        output['result'] = 'passed_boundary_characterization'
    except Exception as exc:
        output['result'] = 'failed'
        output['error'] = f'{type(exc).__name__}: {str(exc)[:350]}'
        raise
    finally:
        if config:
            try:
                runner.compose_call(project, config, 'down', '-v', '--remove-orphans')
                output['compose_cleanup'] = 'passed'
            except Exception as exc:
                output['compose_cleanup'] = f'failed: {type(exc).__name__}'
        try:
            runner.docker('image', 'rm', image)
            output['image_cleanup'] = 'passed'
        except Exception:
            output['image_cleanup'] = 'failed'
        if original_containers is not None:
            current_containers, current_volumes = runner.inventory()
            output['original_resources_preserved'] = (
                current_containers == original_containers and current_volumes == original_volumes)
            output['project_resources_absent'] = not runner.docker('ps', '-aq', '--filter',
                'label=com.docker.compose.project=' + project) and not runner.docker(
                'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project)
        if work.resolve().parent == (ROOT / 'backups').resolve() and work.name.startswith(project + '-'):
            try:
                shutil.rmtree(work)
                output['private_work_cleanup'] = 'passed'
            except Exception:
                output['private_work_cleanup'] = 'failed'
        if output['result'] == 'passed_boundary_characterization' and (
            output.get('compose_cleanup') != 'passed' or output.get('image_cleanup') != 'passed'
            or not output.get('original_resources_preserved') or not output.get('project_resources_absent')
            or output.get('private_work_cleanup') != 'passed'
        ):
            output['result'] = 'failed_cleanup'
        output['finished_at'] = base.utcnow()
        args.output.write_text(json.dumps(output, indent=2, default=str), encoding='utf-8')
        print(f"{output['result']}: {args.output}", flush=True)


if __name__ == '__main__':
    main()
