"""Opt-in real HTTP checks against disposable Cloud-only containers.

RUN_CLOUD_METRICS_DOCKER=1 and CLOUD_METRICS_TEST_IMAGE must be explicit.
Uses existing crisisguard-local PostgreSQL for read-only probes; no DB init,
Cloud event writes, volumes, builds, service restarts or controller changes.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.getenv('RUN_CLOUD_METRICS_DOCKER') != '1',
                               reason='Explicit opt-in: temporary Docker Cloud containers')


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        # Arguments can contain generated/private database credentials.
        raise RuntimeError('Docker verification command failed: ' + args[0])
    return result.stdout.strip()


@pytest.fixture(scope='module')
def running_clouds():
    image = os.environ['CLOUD_METRICS_TEST_IMAGE']
    assert image.startswith('crisisguard-metrics:cp7a-')
    original_ids = docker('ps', '-aq').splitlines()
    before = json.loads(docker('inspect', *original_ids))
    def stable(rows):
        return {r['Id']: {'image': r['Image'], 'started_at': r['State']['StartedAt'],
                'restarts': r['RestartCount'], 'mounts': r['Mounts']} for r in rows}
    baseline = stable(before)
    volumes = docker('volume', 'ls', '-q').splitlines()
    patient = [r for r in before if r['Config'].get('Labels', {}).get('com.docker.compose.project') == 'crisisguard-local'
               and r['Config']['Labels'].get('com.docker.compose.service') == 'patient']
    assert len(patient) == 1
    env = dict(item.split('=', 1) for item in patient[0]['Config']['Env'] if '=' in item)
    networks = list(patient[0]['NetworkSettings']['Networks'])
    assert networks == ['crisisguard-local_default']
    containers, observations = {}, {}
    report = {'image': image, 'checks': observations, 'original_resources_preserved': False,
              'database_scope': 'existing database read-only probes; lifespan off; failed event submission only to unreachable-database container'}
    try:
        for name in ('patient', 'emergency', 'admin', 'database-unavailable'):
            service = 'patient' if name == 'database-unavailable' else name
            url = env['DATABASE_URL'] if name != 'database-unavailable' else 'postgresql://unavailable:unavailable@127.0.0.1:1/unavailable'
            cid = docker('run', '-d', '--name', 'cg-cp7a-' + uuid4().hex[:12],
                '--network', networks[0], '-p', '127.0.0.1::8000', '-e', 'DATABASE_URL='+url,
                image, 'python', '-m', 'uvicorn', f'cloud.{service}_service.main:app',
                '--host', '0.0.0.0', '--port', '8000', '--lifespan', 'off')
            containers[name] = {'id': cid}
            row = json.loads(docker('inspect', cid))[0]
            assert row['Mounts'] == [], 'Verification containers must not create volumes'
            port = row['NetworkSettings']['Ports']['8000/tcp'][0]['HostPort']
            base = 'http://127.0.0.1:' + port
            containers[name]['url'] = base
            deadline = time.monotonic()+30
            while True:
                try:
                    if httpx.get(base+'/metrics', timeout=5, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, 'Verification service did not become ready'
                time.sleep(.2)
        yield containers, observations
    finally:
        for node in containers.values():
            docker('rm', '-f', node['id'])  # Only containers created above; no volumes.
        assert stable(json.loads(docker('inspect', *original_ids))) == baseline
        assert set(docker('volume', 'ls', '-q').splitlines()) == set(volumes)
        report['original_resources_preserved'] = True
        path = os.getenv('CLOUD_METRICS_DOCKER_REPORT')
        if path:
            with Path(path).open('x', encoding='utf-8') as output:
                json.dump(report, output, indent=2)


@pytest.mark.parametrize('service', ['patient', 'emergency', 'admin'])
def test_real_cloud_metrics_under_concurrent_http_traffic(running_clouds, service):
    containers, observations = running_clouds
    base = containers[service]['url']
    with httpx.Client(base_url=base, timeout=20, trust_env=False) as client:
        idle = client.get('/metrics').json()
        assert idle['p95_latency_ms'] is None and idle['latency_sample_count'] == 0
        assert idle['database_healthy'] is True
        root = client.get('/')
        assert root.status_code == 200
        one = client.get('/metrics').json()
        assert one['latency_sample_count'] == 1
        assert one['p95_latency_ms'] == one['average_latency_ms']
        def request(index):
            # Real 404s exercise non-5xx application errors without DB writes.
            return client.get('/' if index % 10 else '/cp7a-not-found').status_code
        with ThreadPoolExecutor(max_workers=16) as pool:
            statuses = list(pool.map(request, range(200)))
        assert statuses.count(200) == 180 and statuses.count(404) == 20
        measured = client.get('/metrics').json()
        assert measured['window_requests'] == measured['latency_sample_count'] == measured['total_requests'] == 201
        assert measured['failed_requests'] == measured['error_rate'] == 0
        assert measured['p95_latency_ms'] >= 0 and measured['average_latency_ms'] >= 0
        assert measured['throughput_rps'] > 0 and 0 < measured['observation_seconds'] <= 60
        assert abs(measured['throughput_rps'] - round(201/measured['observation_seconds'], 4)) <= .0002
        assert measured['in_flight_requests'] == 0
        assert client.get('/health').status_code == 200
        assert client.get('/metrics').json()['total_requests'] == 201
        observations[service] = {'idle': idle, 'one_request': one, 'concurrent_requests': measured,
            'response_counts': {'200': statuses.count(200), '404': statuses.count(404)}}


def test_real_database_failure_does_not_count_monitoring_as_application_error(running_clouds):
    containers, observations = running_clouds
    with httpx.Client(base_url=containers['database-unavailable']['url'], timeout=10, trust_env=False) as client:
        metrics = client.get('/metrics').json()
        assert metrics['database_healthy'] is False
        assert client.get('/health').status_code == 503
        after = client.get('/metrics').json()
        assert after['latency_sample_count'] == after['total_requests'] == after['failed_requests'] == 0
        assert after['p95_latency_ms'] is None
        response = client.post('/events', json={
            'event_id': 'CP7A_UNREACHABLE_' + uuid4().hex, 'site_id': 'Hospital_A',
            'event_type': 'patient_registration', 'base_priority': 2,
            'prerequisite_ids': [], 'payload': {'test_source': 'cp7a_no_reachable_database'}})
        assert response.status_code == 503
        failed = client.get('/metrics').json()
        assert failed['database_healthy'] is False and failed['error_rate'] == 1
        assert failed['latency_sample_count'] == failed['failed_requests'] == failed['total_requests'] == 1
        assert failed['p95_latency_ms'] == failed['average_latency_ms']
        assert failed['in_flight_requests'] == 0
        observations['database_unavailable'] = {'idle': after, 'application_503': failed}


def test_real_idle_window_expires_without_resetting_lifetime_counters(running_clouds):
    containers, observations = running_clouds
    # Exercise the real monotonic 60s expiry; no sample/clock mutation in Docker.
    deadline = time.monotonic() + 61
    while time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline-time.monotonic())))
    for service in ('patient', 'emergency', 'admin', 'database-unavailable'):
        result = httpx.get(containers[service]['url']+'/metrics', timeout=10, trust_env=False).json()
        assert result['latency_sample_count'] == result['window_requests'] == 0
        assert result['p95_latency_ms'] is None
        assert result['average_latency_ms'] == result['error_rate'] == result['throughput_rps'] == 0
        assert result['in_flight_requests'] == 0
        assert result['total_requests'] == (1 if service == 'database-unavailable' else 201)
        assert result['failed_requests'] == (1 if service == 'database-unavailable' else 0)
        observations[service+'_idle_after_61s'] = result
