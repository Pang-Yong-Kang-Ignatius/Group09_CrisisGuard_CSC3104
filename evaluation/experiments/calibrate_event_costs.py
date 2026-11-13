"""Separate bounded service-wall-time calibration through authentic replay grants.

No Hospital/SQLite or live-write claim: this is a serial, credentialed protocol
client against a fresh disposable Cloud/controller database. Existing rounded
middleware means are differenced over validated batches with explicit rounding
intervals, not reinterpreted as exact CPU cost or commit latency.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics
import time
import uuid

import httpx
import psycopg
from psycopg.rows import dict_row

from cloud.metaguard import event_digest
from evaluation.experiments import run_recovery_matrix as matrix

ROOT, base, fair = matrix.ROOT, matrix.base, matrix.fair


def batch_cost(before, after, count):
    assert count > 0
    n0, n1 = before['window_requests'], after['window_requests']
    assert n1-n0 == count and after['total_requests']-before['total_requests'] == count, 'Window expired or unexpected traffic'
    assert after['replay_latency_sample_count']-before['replay_latency_sample_count'] == count
    for key in ('live_latency_sample_count', 'unclassified_latency_sample_count'):
        assert after[key] == before[key], 'Calibration population contaminated'
    estimate = (n1*after['average_latency_ms']-n0*before['average_latency_ms'])/count
    error = .005*(n0+n1)/count
    assert estimate > error, 'Rounded metrics cannot resolve this cost'
    return {'n': count, 'mean_service_wall_ms': estimate,
            'rounding_interval_ms': [estimate-error, estimate+error]}


def replica(work, image, seed, original_volumes):
    project = 'cg-cp8-cost-'+uuid.uuid4().hex[:10]
    directory = work/project
    directory.mkdir()
    runner = base.Runner(directory, image, directory/'raw.json', 1)
    settings = fair.settings_for('fixed')
    # Serial cost calibration excludes pacing, retaining Hospital authentication,
    # content-bound grants and Cloud grant enforcement in the existing overlay.
    settings['CONTROLLER_ADMISSION_ENABLED'] = '0'
    expected = {}
    chains = [base.chain(f'CP8-COST-{seed}-{i}', base.SITES[i % 2]) for i in range(25)]
    heavy = [base.chain(f'CP8-HEAVY-{seed}-{i}', base.SITES[i % 2])[0] for i in range(5)]
    for records in chains+[[event] for event in heavy]:
        identity = records[0]['event_id']
        workflow = str(uuid.uuid5(uuid.NAMESPACE_URL, 'cp8-cost-workflow:'+identity))
        ids = {e['event_id']: str(uuid.uuid5(uuid.NAMESPACE_URL, 'cp8-cost-event:'+e['event_id'])) for e in records}
        for step, event in enumerate(records):
            event['prerequisite_ids'] = [ids[p] for p in event['prerequisite_ids']]
            event['event_id'] = ids[event['event_id']]
            # Match the generator's payload shape while calibrating each type separately.
            event['payload'] = {'synthetic': True, 'patient_id': 'SYNTHETIC_'+workflow,
                                'workflow_id': workflow, 'step': step}
    for event in heavy:
        event['payload']['cp8_expensive'] = True
    for event in [e for chain in chains for e in chain]+heavy:
        expected[event['event_id']] = event
    out = {'seed': seed, 'project': project, 'expected_events': list(expected.values()),
           'planned_workflows': len(chains)+len(heavy), 'warmup_events': 20, 'normal_measured_events': 80, 'heavy_measured_events': 5,
           'event_attempts': [], 'batches': [], 'result': 'running',
           'scope': 'Direct serial authenticated grant-client calibration; no Hospital capture or automatic replay'}
    client = httpx.Client(timeout=10, trust_env=False)
    controller = f"http://127.0.0.1:{settings['CONTROLLER_PORT']}"
    cloud = {s: f"http://127.0.0.1:{settings[s.upper()+'_PORT']}" for s in base.SERVICES}
    database = f"postgresql://{settings['POSTGRES_USER']}:{settings['POSTGRES_PASSWORD']}@127.0.0.1:{settings['POSTGRES_PORT']}/{settings['POSTGRES_DB']}"
    config = None
    def request(method, url, *, body=None, headers=None, code=200):
        response = client.request(method, url, json=body, headers=headers)
        assert response.status_code == code, (method, url, response.status_code, response.text[:200])
        return response.json()
    def send(event):
        site = event['site_id']
        token = settings['HOSPITAL_A_CONTROLLER_TOKEN' if site == base.SITES[0] else 'HOSPITAL_B_CONTROLLER_TOKEN']
        auth = {'Authorization': 'Bearer '+token}
        attempt = {'event_id': event['event_id'], 'stage': 'candidate', 'started_mono': time.monotonic()}
        out['event_attempts'].append(attempt)
        request('POST', controller+'/hospitals/'+site+'/candidate', body={
            'event_id': event['event_id'], 'priority': event['base_priority'], 'event_digest': event_digest(event)}, headers=auth)
        attempt['stage'] = 'allocate'
        grant = request('POST', controller+'/hospitals/'+site+'/grants/next', headers=auth)
        assert grant['event_id'] == event['event_id']
        attempt['stage'] = 'acknowledge'
        request('POST', controller+'/grants/'+grant['grant_id']+'/acknowledge', body={'hospital_id': site}, headers=auth)
        attempt['stage'] = 'write'
        start = time.monotonic()
        request('POST', cloud[base.ROUTE[event['event_type']]]+'/events', body=event, code=201, headers={
            **auth, 'X-MetaGuard-Grant': grant['grant_id'], 'X-CrisisGuard-Traffic': 'replay',
            'X-CrisisGuard-Telemetry-Token': settings['CLOUD_REPLAY_TELEMETRY_TOKEN']})
        attempt['client_write_ms'] = 1000*(time.monotonic()-start)
        attempt['stage'] = 'complete'
        assert request('POST', controller+'/grants/'+grant['grant_id']+'/complete', body={'hospital_id': site}, headers=auth)['status'] == 'completed'
        attempt.update(stage='completed', finished_mono=time.monotonic())
    try:
        config, resolved = runner.resolve(project, directory, settings)
        assert all(resolved['services'][service]['environment']['METAGUARD_REQUIRED'] == '1' for service in base.SERVICES)
        matrix.write_json(runner.raw_path, out)
        runner.compose_call(project, config, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180',
                            'postgres', 'controller', 'patient', 'emergency', 'admin')
        for chain in chains[:5]:
            for event in chain:
                send(event)
        for index, kind in enumerate(base.KINDS):
            before = request('GET', cloud[base.ROUTE[kind]]+'/metrics')
            start = time.monotonic()
            for chain in chains[5:]:
                send(chain[index])
            after = request('GET', cloud[base.ROUTE[kind]]+'/metrics')
            assert time.monotonic()-start < 55, 'Batch exceeded isolated window bound'
            out['batches'].append({'event_type': kind, 'before': before, 'after': after,
                                   'elapsed_seconds': time.monotonic()-start, **batch_cost(before, after, 20)})
            matrix.write_json(runner.raw_path, out)
        root_cost = out['batches'][0]['mean_service_wall_ms']
        delay = 9*root_cost/1000
        with psycopg.connect(database, connect_timeout=5, options='-c statement_timeout=10000 -c lock_timeout=5000') as conn:
            matrix.create_cost_delay(conn, delay)
        out['heavy_delay_seconds'] = delay
        before = request('GET', cloud['patient']+'/metrics')
        start = time.monotonic()
        for event in heavy:
            send(event)
        after = request('GET', cloud['patient']+'/metrics')
        assert time.monotonic()-start < 55
        out['heavy_batch'] = {'before': before, 'after': after, **batch_cost(before, after, 5)}
        with psycopg.connect(database, row_factory=dict_row, connect_timeout=5, options='-c statement_timeout=10000 -c lock_timeout=5000') as conn:
            out['postgres_version'] = conn.execute('SELECT version() AS version').fetchone()['version']
            events = conn.execute('SELECT * FROM events ORDER BY event_id').fetchall()
            grants = conn.execute('SELECT * FROM controller_grants').fetchall()
            audit = conn.execute('SELECT * FROM controller_grant_audit ORDER BY id').fetchall()
            tickets = conn.execute('SELECT * FROM controller_tickets').fetchall()
        out.update(postgres_events=events, grants=grants, audit=audit, remaining_tickets=tickets)
        matrix.verify(expected, events, grants, audit, tickets, None)
        out['result'] = 'passed'
    except Exception as exc:
        out.update(result='failed', error=f'{type(exc).__name__}: {exc}')
        try:
            with psycopg.connect(database, connect_timeout=3, row_factory=dict_row) as conn:
                out['failure_events'] = conn.execute('SELECT * FROM events').fetchall()
                out['failure_grants'] = conn.execute('SELECT * FROM controller_grants').fetchall()
        except Exception as snapshot_error:
            out['failure_snapshot_error'] = type(snapshot_error).__name__
    finally:
        client.close()
        matrix.write_json(runner.raw_path, out)
        if config:
            try:
                matrix.cleanup_project(runner, project, config, original_volumes)
                out['cleanup'] = 'passed'
            except Exception as exc:
                out.update(result='failed', cleanup_error=f'{type(exc).__name__}: {exc}')
        matrix.write_json(runner.raw_path, out)
    print(f"{out['result'].upper()} cost calibration seed={seed}", flush=True)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), 'Choose a new output'
    args.output.mkdir(parents=True)
    work = ROOT/'.venv'/('cp8-cost-'+uuid.uuid4().hex[:12])
    work.mkdir()
    runner = base.Runner(work, args.image, args.output/'summary.json', 1)
    before, volumes = runner.inventory()
    before_states = {r['Id']: r['State']['Status'] for r in json.loads(runner.docker('inspect', *before))} if before else {}
    before_networks = set(runner.docker('network', 'ls', '-q').splitlines())
    report = {'image': args.image, 'source_sha256': matrix.fingerprint(), 'raw_backup_directory': str(work),
              'image_id': runner.docker('image', 'inspect', '--format', '{{.Id}}', args.image),
              'seeds': [9191, 9192, 9193], 'replicas': [], 'result': 'running'}
    try:
        for seed in report['seeds']:
            result = replica(work, args.image, seed, volumes)
            report['replicas'].append(result)
            if result['result'] != 'passed':
                break
        assert len(report['replicas']) == 3 and all(r['result'] == 'passed' for r in report['replicas'])
        by_type = defaultdict(list)
        for row in report['replicas']:
            for batch in row['batches']:
                by_type[batch['event_type']].append(batch['mean_service_wall_ms'])
        means = {kind: statistics.fmean(values) for kind, values in by_type.items()}
        unit = statistics.median(means.values())/10
        heavy_ms = statistics.fmean(r['heavy_batch']['mean_service_wall_ms'] for r in report['replicas'])
        profile = {'schema': 'cp8-service-wall-cost-v1', 'units_by_type': {kind: max(1, round(value/unit)) for kind, value in means.items()},
            'quantum': 10, 'unit_ms': unit, 'mean_service_wall_ms_by_type': means,
            'per_replica_type_means': dict(by_type), 'heavy_mean_service_wall_ms': heavy_ms,
            'heavy_event_units': max(1, round(heavy_ms/unit)), 'heavy_delay_seconds': statistics.fmean(r['heavy_delay_seconds'] for r in report['replicas']),
            'heavy_to_root_measured_ratio': heavy_ms/means['patient_registration'],
            'scope': 'Estimated ASGI service wall-time units including DB/grant validation, not CPU demand or end-to-end latency',
            'raw_backup_directory': str(work), 'source_sha256': report['source_sha256']}
        matrix.validate_cost_profile(profile)
        matrix.write_json(args.output/'cost_profile.json', profile)
        report['result'] = 'passed'
    except Exception as exc:
        report.update(result='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        after, after_volumes = runner.inventory()
        after_states = {r['Id']: r['State']['Status'] for r in json.loads(runner.docker('inspect', *after))} if after else {}
        report['preservation'] = {'container_identity_image_mounts_equal': before == after,
            'container_states_equal': before_states == after_states,
            'exact_volume_set_equal': volumes == after_volumes,
            'exact_network_set_equal': before_networks == set(runner.docker('network', 'ls', '-q').splitlines())}
        if not all(report['preservation'].values()):
            report['result'] = 'failed'
        matrix.write_json(args.output/'summary.json', report)
    assert report['result'] == 'passed'


if __name__ == '__main__':
    main()
