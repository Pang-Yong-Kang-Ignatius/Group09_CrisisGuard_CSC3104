"""Bounded real replay comparison using the retained 7B isolation harness.

Read loads are absolute RPS, not percentages of unmeasured capacity. This
screening matrix cannot establish the proposal's calibrated live-write SLO.
"""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import random
import secrets
import subprocess
import sys
import threading
import time
import uuid

import httpx
import psycopg
from psycopg.rows import dict_row

from edge.scheduler.config import load_scheduler_settings
from evaluation.workloads.generate_workload import WorkloadConfig, generate_workload, capture_request
from infrastructure.scripts.project_verify import repository_revision

ROOT = Path(__file__).resolve().parents[2]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'docs/evidence' / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fair = load('cp8_fair', 'cp7b-sustained-fairness-experiment-20261029.py')
base = fair.base
base.BASE_FILES = [p for p in base.BASE_FILES if p.exists()]
POLICIES = {
    'rr_off': ('fifo', '0', 'priority'), 'rr_on': ('fifo', '1', 'priority'),
    'bounded_off': ('bounded', '0', 'priority'), 'bounded_on': ('bounded', '1', 'priority'),
    'priority_on': ('priority', '1', 'priority'), 'drr_on': ('drr', '1', 'priority'),
    'fifo': ('fifo', '0', 'fifo'),
}
COST_POLICIES = {'drr_under': ('drr', '1', 'priority'), 'drr_over': ('drr', '1', 'priority'),
                 'bounded_b2': ('bounded', '1', 'priority'), 'bounded_b4': ('bounded', '1', 'priority')}


def write_json(path, value):
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def create_cost_delay(conn, seconds):
    if not math.isfinite(seconds) or not 0 < seconds <= .5:
        raise ValueError('Experimental database delay must be in (0, .5] seconds')
    conn.execute(f"""CREATE OR REPLACE FUNCTION cp8_cost_delay() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.payload->>'cp8_expensive'='true' THEN PERFORM pg_sleep({seconds:.9f}); END IF; RETURN NEW; END$$""")
    conn.execute('CREATE TRIGGER cp8_cost_delay BEFORE INSERT ON events FOR EACH ROW EXECUTE FUNCTION cp8_cost_delay()')


def validate_cost_profile(profile):
    if not isinstance(profile, dict) or not isinstance(profile.get('units_by_type'), dict) or set(profile['units_by_type']) != set(base.KINDS):
        raise ValueError('Cost profile requires all four event types')
    values = list(profile['units_by_type'].values())+[profile.get('quantum'), profile.get('heavy_event_units')]
    if any(type(v) is not int or not 1 <= v <= 500 for v in values):
        raise ValueError('Cost units/quantum must be integers in 1..500')
    delay = profile.get('heavy_delay_seconds')
    if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 < delay <= .5:
        raise ValueError('Heavy delay must be finite and in (0, .5] seconds')


def missed_live_opportunities(ordinal, started, ended, rate):
    total = max(0, math.ceil(ended*rate)-ordinal)
    cap = max(0, math.ceil(ended*rate)-max(ordinal, math.ceil(started*rate)))
    return cap, total-cap


def wait_for_live_opportunity(stop, start, due, clock=time.monotonic):
    """Wait strictly past due so a tied stop clock excludes that opportunity."""
    while not stop.is_set():
        remaining = due - (clock() - start)
        if remaining < 0:
            return True
        if stop.wait(max(remaining, 1e-6)):
            return False
    return False


def close_live_window(window, lock, stop, start, output, clock=time.monotonic):
    """Capture the stop clock atomically with respect to request submission."""
    with lock:
        if start is not None and window['end'] is None:
            window['end'] = clock()-start
            output['live_generator_stop_seconds'] = window['end']
        stop.set()


def admission_settings(mode, ceiling, throughput_thresholds=None, health_policy='throughput'):
    if mode not in ('aimd', 'state-fixed', 'constant') or not math.isfinite(ceiling) or ceiling <= 0:
        raise ValueError('Invalid experimental admission mode or ceiling')
    if health_policy not in ('throughput', 'pressure-experimental'):
        raise ValueError('Unknown experimental health policy')
    if health_policy != 'throughput' and throughput_thresholds is not None:
        raise ValueError('Pressure policy does not use throughput thresholds')
    settings = fair.settings_for('aimd' if mode == 'aimd' else 'fixed')
    settings.update(SCHEDULER_ENABLE_INHERITANCE='1', SCHEDULER_AGEING_INTERVAL_SECONDS='600',
                    CONTROLLER_ADMISSION_NORMAL_RPS=str(ceiling),
                    CONTROLLER_ADMISSION_CAUTION_RPS=str(ceiling if mode == 'constant' else ceiling/4),
                    CONTROLLER_ADMISSION_RECOVERY_RPS=str(ceiling if mode == 'constant' else ceiling/10),
                    CONTROLLER_ADMISSION_ADDITIVE_RPS=str(ceiling/10))
    if health_policy != 'throughput':
        settings['CONTROLLER_ADMISSION_HEALTH_POLICY'] = health_policy
    if throughput_thresholds is not None:
        if (len(throughput_thresholds) != 3 or any(not math.isfinite(v) or v <= 0 for v in throughput_thresholds)
                or not throughput_thresholds[0] < throughput_thresholds[1] < throughput_thresholds[2]):
            raise ValueError('Throughput thresholds must be finite, positive and healthy < caution < recovery')
        settings.update({f'CONTROLLER_ADMISSION_{name}_THROUGHPUT_RPS': str(value)
                         for name, value in zip(('HEALTHY', 'CAUTION', 'RECOVERY'), throughput_thresholds)})
    return settings


def cleanup_project(runner, project, config, original_volumes, retain_databases=False):
    volumes = runner.docker('volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project='+project).splitlines()
    assert not set(volumes) & original_volumes
    for volume in volumes:
        assert json.loads(runner.docker('volume', 'inspect', volume))[0]['Labels']['com.docker.compose.project'] == project
    runner.compose_call(project, config, 'down', '--remove-orphans')
    if not retain_databases:
        for volume in volumes:
            runner.docker('volume', 'rm', volume)
    else:
        runner.out['retained_database_volumes'] = sorted(volumes)
        assert set(volumes) == set(runner.docker('volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project='+project).splitlines())
    for resource in (('network',) if retain_databases else ('volume', 'network')):
        assert not runner.docker(resource, 'ls', '-q', '--filter', 'label=com.docker.compose.project='+project)
    assert not runner.docker('ps', '-aq', '--filter', 'label=com.docker.compose.project='+project)
    config.unlink()


def fingerprint():
    paths = [p for folder in ('edge', 'cloud', 'evaluation/experiments', 'evaluation/workloads')
             for p in (ROOT / folder).rglob('*.py')]
    paths += [ROOT / 'compose.yaml', ROOT / 'infrastructure/docker/compose.metaguard.yaml']
    paths += [ROOT / 'docs/evidence' / name for name in (
        'cp7b-fixed-vs-aimd-experiment-20261029.py', 'cp7b-sustained-fairness-experiment-20261029.py')]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def plan(mode, total=44, pilot_policy='bounded_on', cost_profile=None, pilot_seeds=None):
    seeds = (9191,) if pilot_seeds is None else tuple(pilot_seeds)
    cases = [(1, 1, 3, 'balanced', seed) for seed in seeds] if mode == 'pilot' else [
        (*ratio, depth, urgency, seed) for ratio, depth, urgency, seed in
        itertools.product(((1, 1), (10, 1)), (0, 3), ('balanced', 'a_only'), range(3104, 3109))]
    if mode == 'cost-stress':
        cases = [(10, 1, 3, 'balanced', seed) for seed in range(3104, 3109)]
        assert cost_profile is not None, 'Cost stress requires an independent calibration profile'
    result = []
    for a, b, depth, urgency, seed in cases:
        condition = f'ratio{a}-{b}_depth{depth}_{urgency}'
        config = WorkloadConfig(run_id='cp8-cost-stress-v1' if mode == 'cost-stress' else 'cp8-screen-v2', seed=seed, total_events=total,
                                backlog_ratio=(a, b), dependency_depth=depth, urgent_distribution=urgency,
                                urgent_fraction=.5)
        policies = [pilot_policy] if mode == 'pilot' else list(POLICIES)
        workload = generate_workload(config)
        if mode == 'cost-stress':
            policies = ['rr_on', 'bounded_on', 'bounded_b2', 'bounded_b4', 'drr_on', 'drr_under', 'drr_over']
            roots = [e for e in workload['events'] if not e['prerequisite_ids']]
            expensive = random.Random(f'cp8-heavy:{seed}').sample(roots, max(1, math.ceil(.1*total)))
            assert all(e['event_type'] == 'patient_registration' for e in expensive)
            for event in expensive:
                event['payload']['cp8_expensive'] = True
        # Fixed randomization independent of timings, outcomes and Python hash salt.
        random.Random(f'cp8-order-v1:{condition}:{seed}').shuffle(policies)
        case = {'condition': condition, 'seed': seed, 'workload': workload, 'policies': policies}
        if cost_profile is not None:
            case['quantum'] = cost_profile['quantum']
            if mode == 'cost-stress':
                case['bypass_limits'] = {'bounded_b2': 2, 'bounded_b4': 4}
                case['heavy_delay_seconds'] = cost_profile['heavy_delay_seconds']
            case['reference_event_cost_units'] = {e['event_id']: cost_profile['heavy_event_units'] if e['payload'].get('cp8_expensive') else cost_profile['units_by_type'][e['event_type']]
                                                   for e in workload['events']}
            case['event_cost_units'] = {policy: {e['event_id']: max(1, round(case['reference_event_cost_units'][e['event_id']]*(
                .5 if policy == 'drr_under' else 2))) if e['payload'].get('cp8_expensive') and policy in ('drr_under', 'drr_over') else case['reference_event_cost_units'][e['event_id']]
                for e in workload['events']} for policy in policies}
            assert all(type(v) is int and 1 <= v <= 1000 for costs in case['event_cost_units'].values() for v in costs.values())
        result.append(case)
    return result


def acceptance_plan(capacity, total=88, cost_profile=None):
    """D2 shapes, two loads and paired admission arms; read-only scope."""
    if not math.isfinite(capacity) or capacity <= 0 or total < 88 or total % 88:
        raise ValueError('Finite positive measured capacity and backlog divisible by 88 required')
    result = []
    for case in plan('matrix', total, cost_profile=cost_profile):
        for fraction in (.3, .7):
            item = copy.deepcopy(case)
            item['condition'] += f'_load{int(fraction*100)}'
            item['live_read_rps'] = capacity*fraction
            item['policies'] = [p for p in item['policies'] if p != 'fifo']
            item['arms'] = [(p, 'aimd') for p in item['policies']]
            if item['condition'].startswith('ratio10-1_depth3_a_only_'):
                item['arms'].append(('bounded_on', 'constant'))
            random.Random(f"admission:{item['condition']}:{item['seed']}").shuffle(item['arms'])
            result.append(item)
    return result


# Reviewed corrected acquisition revision: monotonic recheck already present.
REVIEWED_GENERATOR_SHA256 = '41dddab16a39b6380523347213148c9390cbf28550e509ee76b620db76fca39c'


def admission_plan(capacity, total=88, cost_profile=None):
    cases = [c for c in acceptance_plan(capacity, total, cost_profile)
             if c['condition'].startswith('ratio1-1_depth3_balanced_')]
    for case in cases:
        case['policies'] = ['bounded_on']
        case['arms'] = [('bounded_on', mode) for mode in ('constant', 'aimd')]
        random.Random(f"admission-study:{case['condition']}:{case['seed']}").shuffle(case['arms'])
    return cases


def holdout_plan():
    """Frozen H5 comparisons; no calibration or diagnosis seeds are reused."""
    cases = []
    for comparison, ratio, urgency in (('admission', (1, 1), 'balanced'),
                                       ('scheduler', (10, 1), 'a_only')):
        for load_index, rate in enumerate((36, 84)):
            for index, seed in enumerate(range(60101, 60106)):
                workload = generate_workload(WorkloadConfig(run_id='h5-holdout-v1', seed=seed,
                    total_events=88, backlog_ratio=ratio, dependency_depth=3,
                    urgent_distribution=urgency, urgent_fraction=.5))
                arms = [('bounded_on', 'aimd'),
                        ('bounded_on', 'constant') if comparison == 'admission' else ('rr_on', 'aimd')]
                if (index + load_index) % 2:
                    arms.reverse()
                cases.append(dict(comparison=comparison,
                    condition=f'ratio{ratio[0]}-{ratio[1]}_depth3_{urgency}_load{30 if rate == 36 else 70}',
                    seed=seed, workload=workload, live_read_rps=rate,
                    policies=sorted({arm[0] for arm in arms}), arms=arms))
    random.Random('h5-block-order-20261102-v1').shuffle(cases)
    return cases


def reuse_admission_trials(cases, reports, args, sources):
    """Remove only validated, source-compatible exact arms; leave originals intact."""
    from evaluation.experiments.assess_acceptance import assess
    reused = []
    for raw in reports:
        old_sources = raw['design'].get('source_sha256', {})
        required = {k:v for k,v in sources.items() if k.startswith(('edge/', 'cloud/', 'evaluation/workloads/'))
                    or k.endswith('.yaml') or k.startswith('docs/evidence/')}
        if any(old_sources.get(k) != v for k,v in required.items()):
            raise ValueError('Reuse source/configuration baseline differs')
        if not (raw['design'].get('generator_protocol') in ('monotonic-recheck-v1', 'serialized-stop-v2')
                or old_sources.get('evaluation/experiments/run_recovery_matrix.py') == REVIEWED_GENERATOR_SHA256):
            raise ValueError('Reuse requires a reviewed corrected generator revision')
        valid = {(t['condition'], t['seed'], t['policy'], t['admission']): t['valid']
                 for t in assess([raw], policies=['bounded_on'])['trial_validity']}
        old_cases = {(c['condition'],c['seed']):c for c in raw['design']['cases']}
        for trial in raw['trials']:
            config = trial.get('configuration', {})
            arm = (trial['policy'], config.get('admission_mode'))
            for case in cases:
                if (case['condition'],case['seed']) != (trial['condition'],trial['seed']) or arm[0] != 'bounded_on':
                    continue
                if json.dumps(old_cases[trial['condition'],trial['seed']]['workload'], sort_keys=True) != json.dumps(case['workload'], sort_keys=True):
                    raise ValueError('Reuse workload differs')
                expected = dict(live_read_rps=case['live_read_rps'], live_read_target=args.live_read_target,
                    live_slo_ms=args.live_slo_ms, live_concurrency_cap=args.live_workers,
                    poll_seconds=args.poll_seconds, drain_timeout_seconds=args.timeout,
                    replay_activation=args.activation, scheduling='bounded', bypass_limit=1,
                    inheritance='1', local_selection='priority')
                if any(config.get(k) != v for k,v in expected.items()):
                    raise ValueError('Reuse measurement/scheduling configuration differs')
                rates = {k:v for k,v in admission_settings(arm[1], args.admission_ceiling).items()
                         if k.startswith('CONTROLLER_ADMISSION_')}
                def numeric_settings(settings):
                    result = {}
                    for k, v in settings.items():
                        try: result[k] = float(v)
                        except (ValueError, TypeError): result[k] = v
                    return result
                if numeric_settings(config.get('controller', {})) != numeric_settings(rates):
                    raise ValueError('Reuse admission configuration differs')
                costs = case.get('reference_event_cost_units', {e['event_id']:1 for e in case['workload']['events']})
                if config.get('reference_event_cost_units') != costs:
                    raise ValueError('Reuse reference costs differ')
                if not valid.get((trial['condition'],trial['seed'],*arm)):
                    raise ValueError('Reuse trial is invalid')
                if arm not in case['arms']:
                    raise ValueError('Duplicate reused trial')
                case['arms'].remove(arm)
                reused.append(dict(condition=case['condition'],seed=case['seed'],policy=arm[0],admission=arm[1]))
    return reused


def state_changes(before, after):
    return {key: {'before': before.get(key), 'after': after.get(key)}
            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)}


def verify(expected, events, grants, audit, tickets, local):
    actual = {e['event_id']: e for e in events}
    assert len(events) == len(actual) == len(expected) and set(actual) == set(expected), 'Event-set mismatch'
    assert not tickets, 'Remaining tickets'
    completed = [g for g in grants if g['status'] == 'completed']
    assert len(completed) == len(expected) and {g['event_id'] for g in completed} == set(expected), 'Grant mismatch'
    assert all(g['status'] not in ('issued', 'acknowledged', 'reconciliation_required') for g in grants)
    allocations = {g['event_id']: next(a['id'] for a in audit if a['grant_id'] == g['grant_id'] and a['action'] == 'allocate')
                   for g in completed}
    for eid, event in expected.items():
        assert actual[eid]['status'] == 'applied'
        for key in ('site_id', 'event_type', 'base_priority', 'prerequisite_ids', 'payload'):
            assert actual[eid][key] == event[key], (eid, key)
        for parent in event['prerequisite_ids']:
            assert actual[parent]['applied_at'] <= actual[eid]['applied_at']
            assert allocations[parent] < allocations[eid], 'Dependency grant order'
    if local is not None:
        assert set(e['event_id'] for rows in local.values() for e in rows) == set(expected)
        assert all(e['status'] == 'applied' for rows in local.values() for e in rows)


class Trial(base.Runner):
    def __init__(self, work, image, case, policy, volumes, args):
        self.case, self.policy, self.volumes, self.args = case, policy, volumes, args
        self.project = 'cg-cp8-' + uuid.uuid4().hex[:12]
        directory = work / self.project
        directory.mkdir()
        super().__init__(directory, image, directory / 'raw.json', 1)
        self.out = {'condition': case['condition'], 'seed': case['seed'], 'policy': policy,
                    'project': self.project, 'workload_sha256': hashlib.sha256(json.dumps(case['workload'], sort_keys=True).encode()).hexdigest(),
                    'capture_attempts': [], 'completion_intervals': {}, 'samples': [], 'live_reads': [],
                    'replay_errors': [], 'resource_samples': [], 'result': 'running', 'raw_path': str(self.raw_path)}

    def save(self):
        write_json(self.raw_path, self.out)

    def execute_trial(self):
        settings = admission_settings(self.args.admission_mode, self.args.admission_ceiling,
                                      getattr(self.args, 'admission_throughput_thresholds', None),
                                      getattr(self.args, 'admission_health_policy', 'throughput'))
        if self.args.live_slo_ms is not None:
            settings['CLOUD_LIVE_SLO_MS'] = str(self.args.live_slo_ms)
        settings.update(ENABLE_MANUAL_REPLAY='0' if self.args.activation == 'automatic' else '1', MANUAL_REPLAY_TOKEN=secrets.token_hex(32))
        site_urls = dict(zip(base.SITES, [f"http://127.0.0.1:{settings[k]}" for k in ('HOSPITAL_A_PORT', 'HOSPITAL_B_PORT')]))
        cloud = {s: f"http://127.0.0.1:{settings[s.upper()+'_PORT']}" for s in base.SERVICES}
        controller = f"http://127.0.0.1:{settings['CONTROLLER_PORT']}"
        operator = {'X-Cloud-Token': settings['CONTROLLER_CLOUD_TOKEN']}
        database = f"postgresql://{settings['POSTGRES_USER']}:{settings['POSTGRES_PASSWORD']}@127.0.0.1:{settings['POSTGRES_PORT']}/{settings['POSTGRES_DB']}"
        expected = {e['event_id']: capture_request(e) for e in self.case['workload']['events']}
        self.out['expected_events'] = list(expected.values())
        config = None
        stop = threading.Event()
        live_window = {'end': None}
        live_window_lock = threading.Lock()
        pool = ThreadPoolExecutor(max_workers=4)
        client = httpx.Client(timeout=10, trust_env=False)
        futures = []
        def request(method, url, **kwargs):
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        try:
            startup = time.monotonic()
            config, resolved = self.resolve(self.project, self.work, settings)
            scheduling, inheritance, local_policy = (POLICIES | COST_POLICIES)[self.policy]
            costs = self.case.get('event_cost_units', {}).get(self.policy, {eid: 1 for eid in expected})
            quantum = self.case.get('quantum', 1)
            bypass_limit = 0 if scheduling == 'fifo' else self.case.get('bypass_limits', {}).get(self.policy, 1)
            # shortcut: the main screen uses unit costs; use the separate calibrated stress for cost estimates.
            for service in ('hospital-a', 'hospital-b'):
                resolved['services'][service]['environment'].update(CRISISGUARD_EXPERIMENTAL_POLICIES='1',
                    REPLAY_INHERITANCE=inheritance, REPLAY_SELECTION_POLICY=local_policy)
                if self.args.metaguard_retry_policy != 'failure-backoff':
                    resolved['services'][service]['environment']['METAGUARD_RETRY_POLICY'] = self.args.metaguard_retry_policy
            resolved['services']['controller']['environment'].update(CRISISGUARD_EXPERIMENTAL_POLICIES='1',
                CONTROLLER_SCHEDULING_POLICY=scheduling, CONTROLLER_BYPASS_LIMIT=str(bypass_limit),
                CONTROLLER_DRR_EVENT_COSTS=json.dumps(costs), CONTROLLER_DRR_QUANTUM=str(quantum))
            health_policy = getattr(self.args, 'admission_health_policy', 'throughput')
            if health_policy != 'throughput':
                resolved['services']['controller']['environment']['CONTROLLER_ADMISSION_HEALTH_POLICY'] = health_policy
            config.write_text(json.dumps(resolved), encoding='utf-8')
            self.out['configuration'] = {'controller': {k: v for k, v in settings.items() if k.startswith('CONTROLLER_ADMISSION_')},
                'admission_mode': self.args.admission_mode,
                'metaguard_retry_policy': self.args.metaguard_retry_policy,
                'scheduler': {site: load_scheduler_settings(resolved['services'][site]['environment']).as_dict()
                              for site in ('hospital-a', 'hospital-b')},
                'scheduling': scheduling, 'bypass_limit': bypass_limit, 'inheritance': inheritance, 'local_selection': local_policy,
                'drr_quantum': quantum, 'estimated_event_cost_units': costs,
                'reference_event_cost_units': self.case.get('reference_event_cost_units', costs),
                'drr_cost_units': 'calibrated service-wall-time proxy, not CPU cost' if 'event_cost_units' in self.case else 'one per event, not calibrated CPU cost',
                'poll_seconds': self.args.poll_seconds, 'drain_timeout_seconds': self.args.timeout,
                'live_read_rps': self.args.live_read_rps, 'live_concurrency_cap': self.args.live_workers,
                'live_read_target': self.args.live_read_target, 'live_slo_ms': self.args.live_slo_ms,
                'replay_activation': self.args.activation,
                'loopback_ports': {key: value for key, value in settings.items() if key.endswith('_PORT')},
                'AUTO_REPLAY': '1' if self.args.activation == 'automatic' else '0',
                'ENABLE_MANUAL_REPLAY': settings['ENABLE_MANUAL_REPLAY'],
                'automatic_health_poll_seconds': .5 if self.args.activation == 'automatic' else None}
            self.save()
            self.compose_call(self.project, config, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180',
                              'postgres', 'hospital-a', 'hospital-b')
            self.out['edge_startup_seconds'] = time.monotonic()-startup
            capture_start = time.monotonic()
            for event in expected.values():
                attempt = {'event_id': event['event_id'], 'started_mono': time.monotonic()}
                self.out['capture_attempts'].append(attempt)
                try:
                    response = client.post(site_urls[event['site_id']]+'/capture', json=event)
                    attempt.update(status_code=response.status_code, finished_mono=time.monotonic())
                    assert response.status_code == 201, response.text[:100]
                except Exception as exc:
                    attempt['error'] = type(exc).__name__
                    raise
            self.out['capture_seconds'] = time.monotonic()-capture_start
            self.save()
            restore = time.monotonic()
            self.compose_call(self.project, config, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180',
                              'controller', 'patient', 'emergency', 'admin')
            deadline = time.monotonic()+60
            while True:
                status = request('GET', controller+'/admission/status', headers=operator)
                if status['state'] == 'Normal' and status['telemetry_fresh'] and status['rate_rps'] >= self.args.admission_ceiling-.001:
                    break
                assert time.monotonic() < deadline, 'Controller readiness timeout'
                time.sleep(.5)
            self.out['startup_seconds'] = time.monotonic()-startup
            self.out['cloud_startup_and_controller_ready_seconds'] = time.monotonic()-restore
            self.out['initial_controller'] = status
            self.save()
            live_fixture_id = None
            if self.args.live_read_rps and self.args.live_read_target == 'preseed':
                # Fixture is exclusive to this trial's disposable PostgreSQL database.
                # It is excluded from the original event/grant correctness denominators.
                live_fixture_id = 'CP8_LIVE_FIXTURE_' + uuid.uuid4().hex
                with psycopg.connect(database) as conn:
                    conn.execute('''INSERT INTO events
                        (event_id,site_id,event_type,base_priority,prerequisite_ids,payload)
                        VALUES (%s,'Hospital_A','patient_registration',1,'[]'::jsonb,'{}'::jsonb)''',
                        (live_fixture_id,))
                with httpx.Client(timeout=2, trust_env=False) as check_client:
                    preflight = check_client.get(cloud['patient']+'/events/'+live_fixture_id,
                        headers={'X-CrisisGuard-Traffic': 'live',
                                 'X-CrisisGuard-Telemetry-Token': settings['CLOUD_LIVE_TELEMETRY_TOKEN']})
                assert preflight.status_code == 200, 'Preseeded live-read fixture must be readable'
                self.out['live_fixture_id'] = live_fixture_id
            if 'heavy_delay_seconds' in self.case:
                with psycopg.connect(database) as conn:
                    create_cost_delay(conn, self.case['heavy_delay_seconds'])
                self.out['heavy_delay_seconds'] = self.case['heavy_delay_seconds']
            if self.args.activation == 'automatic':
                preparation = time.monotonic()
                self.compose_call(self.project, config, 'stop', 'patient', 'emergency', 'admin')
                self.set_replay(config, True)
                hospital_start = time.monotonic()
                self.compose_call(self.project, config, 'up', '-d', '--no-build', '--pull', 'never', '--wait',
                                  '--wait-timeout', '180', 'hospital-a', 'hospital-b')
                self.out['hospital_auto_startup_seconds'] = time.monotonic()-hospital_start
                deadline = time.monotonic()+30
                while True:
                    disconnected = {site: request('GET', url+'/recovery/status') for site, url in site_urls.items()}
                    if all(s['enabled'] and s['cloud_reachable'] is False and not any(s['service_health'].values()) for s in disconnected.values()):
                        break
                    assert time.monotonic() < deadline, 'Hospitals did not observe the isolated Cloud outage'
                    time.sleep(.25)
                self.out['disconnected_status'] = disconnected
                self.out['automatic_outage_preparation_seconds'] = time.monotonic()-preparation
                self.out['controller_before_restore'] = request('GET', controller+'/admission/status', headers=operator)
                with psycopg.connect(database, connect_timeout=5) as conn:
                    assert conn.execute('SELECT count(*) FROM events WHERE event_id = ANY(%s)',
                                        (list(expected),)).fetchone()[0] == 0
                self.save()
            start = time.monotonic()
            self.out['replay_start_mono'] = start
            self.out['replay_start_utc'] = base.utcnow()
            def replay(site):
                with httpx.Client(timeout=self.args.timeout+15, trust_env=False) as worker:
                    while not stop.is_set():
                        try:
                            response = worker.post(site_urls[site]+'/replay', headers={'Authorization': 'Bearer '+settings['MANUAL_REPLAY_TOKEN']})
                            response.raise_for_status()
                        except Exception as exc:
                            self.out['replay_errors'].append({'site': site, 'elapsed': time.monotonic()-start, 'error': type(exc).__name__})
                        stop.wait(.1)
            def live_reads():
                if not self.args.live_read_rps:
                    return
                limits = httpx.Limits(max_connections=self.args.live_workers,
                                      max_keepalive_connections=self.args.live_workers)
                with httpx.Client(timeout=2, trust_env=False, limits=limits) as reader, \
                        ThreadPoolExecutor(max_workers=self.args.live_workers) as read_pool:
                    ordinal = 0
                    pending = set()
                    target_events = list(expected.values())

                    def read_one(event, due):
                        before = time.monotonic()
                        record = {'offered_index': due[0], 'scheduled_elapsed': due[1],
                                  'started_elapsed': before-start,
                                  'start_lag_ms': max(0., 1000*(before-start-due[1])),
                                  'event_id': event['event_id'],
                                  'service': base.ROUTE[event['event_type']]}
                        try:
                            response = reader.get(cloud[base.ROUTE[event['event_type']]]+'/events/'+event['event_id'], headers={
                                'X-CrisisGuard-Traffic': 'live', 'X-CrisisGuard-Telemetry-Token': settings['CLOUD_LIVE_TELEMETRY_TOKEN']})
                            record['status_code'] = response.status_code
                        except Exception as exc:
                            record['error'] = type(exc).__name__
                        record['latency_ms'] = 1000*(time.monotonic()-before)
                        record['finished_elapsed'] = time.monotonic()-start
                        return record

                    while not stop.is_set():
                        due = ordinal/self.args.live_read_rps
                        if not wait_for_live_opportunity(stop, start, due):
                            break
                        finished = {future for future in pending if future.done()}
                        for future in finished:
                            self.out['live_reads'].append(future.result())
                        pending.difference_update(finished)
                        with live_window_lock:
                            if stop.is_set() or live_window['end'] is not None:
                                break
                            ordinal += 1
                            if len(pending) == self.args.live_workers:
                                self.out['live_generator_cap_skips'] = self.out.get('live_generator_cap_skips', 0)+1
                                continue
                            event = (dict(event_id=live_fixture_id, event_type='patient_registration')
                                     if live_fixture_id else target_events[(ordinal-1) % len(target_events)])
                            pending.add(read_pool.submit(read_one, event, (ordinal, due)))
                    for future in pending:
                        self.out['live_reads'].append(future.result())
                if live_window['end'] is not None:
                    target = math.ceil(live_window['end']*self.args.live_read_rps)
                    recorded = len(self.out['live_reads'])+self.out.get('live_generator_cap_skips', 0)+self.out.get('live_generator_lag_skips', 0)
                    # Opportunities are due in [0, end). Cancellation does not
                    # erase an arrival that was due before the stop clock.
                    _, unrecorded = missed_live_opportunities(recorded, live_window['end'], live_window['end'], self.args.live_read_rps)
                    self.out['live_generator_lag_skips'] = self.out.get('live_generator_lag_skips', 0)+unrecorded
                    self.out['live_target_opportunities'] = target
            def resource_samples():
                while not stop.is_set():
                    before = time.monotonic()-start
                    try:
                        ids = self.docker('ps', '-q', '--filter', 'label=com.docker.compose.project='+self.project).splitlines()
                        assert ids, 'No owned containers available for resource sampling'
                        self.out['resource_samples'].append({'started_seconds': before, 'finished_seconds': time.monotonic()-start,
                            'containers': [json.loads(line) for line in self.docker('stats', '--no-stream', '--format', '{{json .}}', *ids).splitlines()]})
                        self.out['resource_samples'][-1]['finished_seconds'] = time.monotonic()-start
                    except Exception as exc:
                        self.out['resource_samples'].append({'started_seconds': before, 'error': type(exc).__name__})
                    stop.wait(10)
            def activate_automatic():
                self.compose_call(self.project, config, 'up', '-d', '--no-build', '--pull', 'never', '--wait',
                                  '--wait-timeout', '180', 'patient', 'emergency', 'admin')
                self.out['cloud_restoration_readiness_seconds'] = time.monotonic()-start
            futures = ([pool.submit(activate_automatic)] if self.args.activation == 'automatic' else
                       [pool.submit(replay, site) for site in base.SITES]) + [pool.submit(live_reads), pool.submit(resource_samples)]
            previous = 0.0
            previous_reconnect = dict.fromkeys(base.SITES, 0.0)
            self.out['reconnection_observations'] = {}
            with psycopg.connect(database, autocommit=True, row_factory=dict_row, connect_timeout=5,
                                 options='-c statement_timeout=10000 -c lock_timeout=5000') as conn:
                while True:
                    before = time.monotonic()-start
                    events = conn.execute('SELECT * FROM events WHERE event_id = ANY(%s) ORDER BY event_id',
                                          (list(expected),)).fetchall()
                    after = time.monotonic()-start
                    for event in events:
                        self.out['completion_intervals'].setdefault(event['event_id'], [previous, after])
                    sample = {'query_started_seconds': before, 'elapsed_seconds': after, 'cloud_completed': len(events),
                              'controller': request('GET', controller+'/admission/status', headers=operator)}
                    sample['tickets'] = conn.execute('''SELECT *, (SELECT coalesce(max(id),0) FROM controller_grant_audit) AS observed_audit_id
                        FROM controller_tickets ORDER BY queue_position''').fetchall()
                    if self.args.activation == 'automatic':
                        for site, url in site_urls.items():
                            if site in self.out['reconnection_observations']:
                                continue
                            requested = time.monotonic()-start
                            try:
                                recovery = request('GET', url+'/recovery/status')
                                received = time.monotonic()-start
                                if recovery['enabled'] and recovery['last_reconnected_at'] and recovery['cloud_reachable']:
                                    self.out['reconnection_observations'][site] = {
                                        'interval_seconds': [previous_reconnect[site], received], 'status': recovery}
                                else:
                                    previous_reconnect[site] = requested
                            except Exception as exc:
                                previous_reconnect[site] = requested
                                sample.setdefault('reconnect_poll_errors', []).append({'site': site, 'error': type(exc).__name__})
                    self.out['samples'].append(sample)
                    if len(events) == len(expected):
                        self.out['cloud_drain_interval_seconds'] = [previous, after]
                        break
                    if after >= self.args.timeout:
                        raise TimeoutError('Original backlog did not drain')
                    previous = before
                    stop.wait(self.args.poll_seconds)
                local_deadline = time.monotonic()+15
                while True:
                    local = {site: request('GET', url+'/events') for site, url in site_urls.items()}
                    if all(all(e['status'] == 'applied' for e in rows) for rows in local.values()):
                        break
                    assert time.monotonic() < local_deadline, 'Local ACK timeout'
                    time.sleep(.2)
                close_live_window(live_window, live_window_lock, stop, start, self.out)
                for future in futures:
                    future.result(timeout=20)
                self.out['local_ack_drain_seconds'] = time.monotonic()-start
                grants = conn.execute('SELECT * FROM controller_grants ORDER BY original_queue_position').fetchall()
                audit = conn.execute('SELECT * FROM controller_grant_audit ORDER BY id').fetchall()
                tickets = conn.execute('SELECT * FROM controller_tickets').fetchall()
                self.out.update(postgres_events=events, grants=grants, audit=audit, local=local, remaining_tickets=tickets)
                verify(expected, events, grants, audit, tickets, local)
                # A completed grant must fence an identical repeat without another effect.
                event = next(iter(expected.values()))
                grant = next(g for g in grants if g['event_id'] == event['event_id'] and g['status'] == 'completed')
                site_token = settings['HOSPITAL_A_CONTROLLER_TOKEN' if event['site_id'] == base.SITES[0] else 'HOSPITAL_B_CONTROLLER_TOKEN']
                duplicate = client.post(cloud[base.ROUTE[event['event_type']]]+'/events', json=event,
                    headers={'Authorization': 'Bearer '+site_token, 'X-MetaGuard-Grant': str(grant['grant_id'])})
                assert duplicate.status_code == 409 and conn.execute(
                    'SELECT count(*) AS n FROM events WHERE event_id = ANY(%s)',
                    (list(expected),)).fetchone()['n'] == len(expected)
                self.out['duplicate_probe_status'] = duplicate.status_code
            self.out['cloud_metrics'] = {s: request('GET', url+'/metrics') for s, url in cloud.items()}
            self.out['resources'] = self.docker('stats', '--no-stream', '--format', '{{json .}}', *self.docker('ps', '-q', '--filter', 'label=com.docker.compose.project='+self.project).splitlines()).splitlines()
            self.out['recovery_status'] = {site: request('GET', url+'/recovery/status') for site, url in site_urls.items()}
            if self.args.activation == 'automatic':
                assert set(self.out['reconnection_observations']) == set(base.SITES), 'Reconnection observer missed a site'
                self.out['disabled_manual_probe_status'] = client.post(next(iter(site_urls.values()))+'/replay').status_code
                assert self.out['disabled_manual_probe_status'] == 404, 'Disabled manual replay route must remain unavailable'
            probe = ("import sqlite3,json; c=sqlite3.connect('/data/hospital.db'); c.row_factory=sqlite3.Row; "
                     "print(json.dumps({'integrity':c.execute('PRAGMA integrity_check').fetchone()[0],"
                     "'recovery':[dict(r) for r in c.execute('SELECT * FROM recovery_state')]}))")
            self.out['sqlite_recovery'] = {site: json.loads(self.compose_capture(self.project, config, 'exec', '-T', service, 'python', '-c', probe))
                for service, site in zip(('hospital-a', 'hospital-b'), base.SITES)}
            if self.args.metaguard_retry_policy != 'failure-backoff':
                journal_probe = ("import json,sqlite3;from edge.database.db import DB_PATH;"
                    "c=sqlite3.connect(DB_PATH);c.row_factory=sqlite3.Row;"
                    "exists=c.execute(\"SELECT 1 FROM sqlite_master WHERE name='metaguard_deferrals'\").fetchone();"
                    "print(json.dumps([dict(r) for r in c.execute('SELECT * FROM metaguard_deferrals ORDER BY id')] if exists else []))")
                self.out['admission_deferrals'] = {site: json.loads(self.compose_capture(self.project, config,
                    'exec', '-T', service, 'python', '-c', journal_probe))
                    for service, site in zip(('hospital-a', 'hospital-b'), base.SITES)}
            assert all(s['integrity'] == 'ok' and all(not r['quarantined'] and not r['in_flight'] for r in s['recovery'])
                       for s in self.out['sqlite_recovery'].values())
            self.out['integrity'] = 'passed'
            self.out['result'] = 'passed'
            # PostgreSQL integration tests run once, each in its own schema.
            if self.args.mode == 'pilot' or getattr(self.args, 'verify_postgres', False):
                env = os.environ.copy()
                env['CONTROLLER_TEST_DATABASE_URL'] = database
                result = subprocess.run([sys.executable, '-m', 'pytest', 'tests/integration/test_controller_postgres.py',
                    'tests/integration/test_cp8_postgres.py',
                    'tests/integration/test_admission_deferral_postgres.py', '-q', '-p', 'no:cacheprovider',
                    '--basetemp='+str(self.work/'pytest'), '--junitxml='+str(self.work/'postgres-tests.xml')],
                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=180)
                (self.work/'pytest.log').write_text(result.stdout+result.stderr, encoding='utf-8')
                self.out['postgres_test_output'] = result.stdout+result.stderr
                assert result.returncode == 0, 'PostgreSQL regression failed'
        except Exception as exc:
            self.out.update(result='failed', error=f'{type(exc).__name__}: {exc}')
            close_live_window(live_window, live_window_lock, stop, locals().get('start'), self.out)
            # Preserve unfinished/failed outcomes before disposable storage is cleaned.
            if config:
                try:
                    with psycopg.connect(database, connect_timeout=3, row_factory=dict_row) as conn:
                        self.out['failure_postgres_events'] = conn.execute('SELECT * FROM events').fetchall()
                        self.out['failure_grants'] = conn.execute('SELECT * FROM controller_grants').fetchall()
                        self.out['failure_tickets'] = conn.execute('SELECT * FROM controller_tickets').fetchall()
                    self.out['failure_local'] = {site: request('GET', url+'/events') for site, url in site_urls.items()}
                except Exception as snapshot_error:
                    self.out['failure_snapshot_error'] = type(snapshot_error).__name__
        finally:
            close_live_window(live_window, live_window_lock, stop, locals().get('start'), self.out)
            pool.shutdown(wait=True)
            client.close()
            self.save()
            if config:
                try:
                    # Reuse 7B's owned-project cleanup, with label and baseline fencing.
                    cleanup_project(self, self.project, config, self.volumes,
                                    getattr(self.args, 'retain_test_databases', False))
                    self.out['cleanup'] = 'passed'
                except Exception as exc:
                    self.out.update(result='failed', cleanup_error=f'{type(exc).__name__}: {exc}')
            self.save()
        print(f"{self.out['result'].upper()} {self.case['condition']} seed={self.case['seed']} {self.policy}: {self.out.get('cloud_drain_interval_seconds', self.out.get('error'))}", flush=True)
        return self.out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('pilot', 'matrix', 'cost-stress', 'acceptance', 'admission', 'holdout'), default='pilot')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--retain-test-databases', action='store_true',
                        help='Retain all trial database volumes; remove only owned containers/networks')
    parser.add_argument('--workers', type=int, choices=(1, 2, 4), default=1)
    parser.add_argument('--total-events', type=int, help='Default: 88 for acceptance, 44 otherwise')
    parser.add_argument('--pilot-policy', choices=tuple(POLICIES), default='bounded_on')
    parser.add_argument('--pilot-seeds', default='9191',
                        help='Comma-separated distinct integer seeds for pilot mode (1..20)')
    parser.add_argument('--activation', choices=('manual', 'automatic'), default='manual')
    parser.add_argument('--cost-profile', type=Path)
    parser.add_argument('--admission-ceiling', type=float, default=20)
    parser.add_argument('--admission-mode', choices=('aimd', 'state-fixed', 'constant'), default='aimd')
    parser.add_argument('--admission-health-policy', choices=('throughput', 'pressure-experimental'), default='throughput',
                        help='Opt-in experimental health signal; production/Compose defaults are unchanged')
    parser.add_argument('--metaguard-retry-policy', choices=('failure-backoff', 'bounded-deferral'), default='failure-backoff',
                        help='Pilot-only opt-in retry handling for definite fresh admission deferrals')
    parser.add_argument('--admission-throughput-thresholds', type=float, nargs=3, metavar=('HEALTHY', 'CAUTION', 'RECOVERY'),
                        help='Experiment-only thresholds, frozen from separate calibration; omit to preserve original settings')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--poll-seconds', type=float, default=.25)
    parser.add_argument('--live-read-rps', type=float, default=2)
    parser.add_argument('--live-workers', type=int, default=1,
                        help='Maximum concurrent authenticated live-read requests (1..128)')
    parser.add_argument('--live-read-target', choices=('original', 'preseed'), default='original',
                        help='preseed uses an applied test-only record in the disposable DB; original retains historical 404 semantics')
    parser.add_argument('--live-slo-ms', type=float,
                        help='Positive live latency SLO for Cloud telemetry and analysis; unset retains legacy behavior')
    parser.add_argument('--capacity-rps', type=float, help='Measured sustainable read capacity; acceptance mode only')
    parser.add_argument('--acceptance-condition', action='append', help='Exact condition ID; repeat to select whole paired case blocks')
    parser.add_argument('--acceptance-seed', type=int, action='append', help='Seed 3104..3108; repeat to batch without rerunning completed seeds')
    parser.add_argument('--reuse-summary', type=Path, action='append', default=[], help='Admission study only: reuse exact compatible corrected trials')
    parser.add_argument('--admission-arm', choices=('constant', 'aimd'), help='Admission mode only: run one selected arm for an exact replacement')
    args = parser.parse_args()
    try:
        admission_settings(args.admission_mode, args.admission_ceiling, args.admission_throughput_thresholds,
                           args.admission_health_policy)
    except ValueError as exc:
        parser.error(str(exc))
    if args.admission_throughput_thresholds and args.reuse_summary:
        parser.error('Do not reuse original trials when changing admission thresholds')
    if args.admission_health_policy != 'throughput' and (args.reuse_summary or args.live_slo_ms is None or args.mode not in ('pilot', 'holdout')):
        parser.error('Experimental health requires a new pilot with explicit live SLO and no reused trials')
    if args.metaguard_retry_policy != 'failure-backoff' and (args.mode not in ('pilot', 'holdout') or args.reuse_summary):
        parser.error('Experimental retries require a new pilot without reused trials')
    if args.admission_arm and args.mode != 'admission':
        parser.error('--admission-arm requires admission mode')
    if args.reuse_summary and args.mode != 'admission':
        parser.error('--reuse-summary requires admission mode')
    if args.total_events is None:
        args.total_events = 88 if args.mode in ('acceptance', 'admission', 'holdout') else 44
    if args.mode not in ('acceptance', 'admission') and (args.acceptance_condition or args.acceptance_seed):
        parser.error('Acceptance filters require acceptance mode')
    if args.output.exists() or args.total_events < 44 or args.total_events % 44 or any(not math.isfinite(v) for v in (args.timeout, args.poll_seconds, args.live_read_rps, args.admission_ceiling)) or args.timeout <= 0 or args.poll_seconds <= 0 or args.live_read_rps < 0 or args.admission_ceiling <= 0 or not 1 <= args.live_workers <= 128 or (args.live_slo_ms is not None and (not math.isfinite(args.live_slo_ms) or args.live_slo_ms <= 0)):
        parser.error('Choose a new output, positive timeout/poll, nonnegative live rate and a multiple of 44 events')
    try:
        pilot_seeds = tuple(int(item.strip()) for item in args.pilot_seeds.split(','))
    except ValueError:
        parser.error('--pilot-seeds must contain comma-separated integers')
    if (not 1 <= len(pilot_seeds) <= 20 or len(set(pilot_seeds)) != len(pilot_seeds)
            or any(seed < 0 for seed in pilot_seeds)
            or (args.mode != 'pilot' and pilot_seeds != (9191,))):
        parser.error('Use 1..20 distinct nonnegative pilot seeds, in pilot mode only')
    profile = json.loads(args.cost_profile.read_text(encoding='utf-8')) if args.cost_profile else None
    if args.mode == 'cost-stress' and profile is None:
        parser.error('--cost-profile is required for the separately calibrated cost stress')
    if profile is not None:
        validate_cost_profile(profile)
    if args.mode == 'holdout':
        if (args.admission_health_policy != 'pressure-experimental' or args.metaguard_retry_policy != 'bounded-deferral'
                or args.total_events != 88 or args.capacity_rps != 120 or args.live_slo_ms != 250
                or args.activation != 'automatic' or args.live_read_target != 'preseed'
                or args.live_workers != 128 or args.admission_ceiling != 20 or args.timeout != 300
                or args.poll_seconds != .25 or args.workers != 1 or args.cost_profile
                or args.admission_throughput_thresholds or args.reuse_summary):
            parser.error('Holdout requires the frozen H5 configuration; see H5_EXPERIMENT_SPEC_20261102.md')
        cases = holdout_plan()
    elif args.mode in ('acceptance', 'admission'):
        if args.capacity_rps is None or not math.isfinite(args.capacity_rps) or args.capacity_rps <= 0 or args.total_events % 88 or args.live_read_target != 'preseed' or args.live_slo_ms is None or args.activation != 'automatic':
            parser.error('Acceptance requires measured --capacity-rps, total divisible by 88, preseed, SLO and automatic activation')
        cases = (admission_plan if args.mode == 'admission' else acceptance_plan)(args.capacity_rps, args.total_events, profile)
        if args.acceptance_condition and not set(args.acceptance_condition) <= {c['condition'] for c in cases}:
            parser.error('Unknown acceptance condition')
        if args.acceptance_seed and not set(args.acceptance_seed) <= set(range(3104, 3109)):
            parser.error('Acceptance seeds must be 3104..3108')
        cases = [c for c in cases if (not args.acceptance_condition or c['condition'] in args.acceptance_condition)
                 and (not args.acceptance_seed or c['seed'] in args.acceptance_seed)]
    else:
        cases = plan(args.mode, args.total_events, args.pilot_policy, profile, pilot_seeds)
    if args.admission_arm:
        for case in cases:
            case['arms'] = [arm for arm in case['arms'] if arm[1] == args.admission_arm]
    reused = reuse_admission_trials(cases, [json.loads(p.read_text()) for p in args.reuse_summary], args, fingerprint()) if args.reuse_summary else []
    args.output.mkdir(parents=True)
    git_head = repository_revision()
    design = {'generator_protocol': 'serialized-stop-v2', 'reused_trials': reused,
              'reuse_summary_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in args.reuse_summary},
              'evaluation_scope': 'H5 independent final-policy holdout; full D2 intentionally unexecuted' if args.mode == 'holdout' else 'Reduced admission study; full 16-condition matrix intentionally unexecuted' if args.mode == 'admission' else 'Original screen or proposed acceptance matrix',
              'schema': 'cp8-recovery-screen-v1', 'configuration': vars(args), 'cases': cases,
              'source_sha256': fingerprint(), 'git_head': git_head,
              'git_head_status': 'PASS' if git_head else 'NOT VERIFIED',
              'limitations': ['Read capacity reference is operator-supplied; validate calibration evidence separately.' if args.mode in ('acceptance', 'admission') else 'Absolute read RPS is uncalibrated; no 30%/70% capacity claim.',
                'Successful live-classified writes remain unsupported.',
                'DRR uses a calibrated service-wall-time proxy, not CPU costs.' if profile is not None else 'DRR unit event costs are uncalibrated.',
                'Automatic activation restores disposable Cloud services after both enabled Hospitals observe an outage; independent two-link partitions are not covered.' if args.activation == 'automatic' else 'Manual replay activation excludes automatic reconnect detection; startup is separate.',
                'Completion elapsed intervals use one host monotonic observer, not cross-clock subtraction.']}
    if args.mode in ('acceptance', 'admission', 'holdout'):
        design['acceptance_protocol'] = {
            'generator_validity': {'max_skip_fraction': .001, 'minimum_issuance_ratio': .999,
                                   'sensitivity_skip_fractions': [0, .0001, .001],
                                   'require_exact_ledger': True, 'scope': 'trial aggregate accuracy; report start-lag diagnostics'},
            'capacity_scope': 'operator-reviewed existing-record reads',
            'urgent_target_conditions': ['ratio10-1_depth3_a_only_load30', 'ratio10-1_depth3_a_only_load70'],
            'urgent_target_site': 'Hospital_A', 'main_policy': 'bounded_on', 'baseline': 'rr_on',
            'scheduler_admission': 'aimd', 'minimum_seed_pairs': 5,
            'targets': {'urgent_relative_pct_max': -10, 'routine_relative_pct_max': 20,
                        'drain_relative_pct_max': 20, 'live_end_to_end_percentage_points_max': 1},
            'slo_definition': 'non-200 OR transport failure OR latency > live_slo_ms; denominator all attempted requests',
            'arrival_model': 'same deterministic constant-rate schedule and existing-record read fixture semantics; common prefix until each trial drains',
            'backlog_note': '88 is the minimum exact-ratio full-chain design, not a proposal-prescribed sample size; verify sustained recovery before expansion'}
    if args.mode == 'holdout':
        design['acceptance_protocol'].update(
            capacity_scope='36/84 RPS against provisional 120 RPS existing-record read-only reference; not maximum sustainable mixed capacity',
            fixed_readiness_window_seconds=10,
            selection='Five pre-reserved seeds; balanced admission and required skewed scheduler comparisons; no replacements or adaptive sample expansion',
            experimental_review='Historical approvals do not approve newer pressure-health/bounded-deferral policies; owner review remains required')
    if profile is not None:
        design['cost_calibration_profile'] = profile
        design['cost_profile_sha256'] = hashlib.sha256(args.cost_profile.read_bytes()).hexdigest()
    write_json(args.output/'plan.json', design)
    if args.plan_only:
        print(f"Planned {sum(len(c.get('arms', c['policies'])) for c in cases)} trials; no experiments executed")
        return
    image = 'crisisguard-cp8:'+uuid.uuid4().hex[:12]
    work = ROOT/'.venv'/('cp8-'+uuid.uuid4().hex[:12])
    work.mkdir()
    runner = base.Runner(work, image, args.output/'summary.json', 1)
    before, volumes = runner.inventory()
    before_states = {r['Id']: r['State']['Status'] for r in json.loads(runner.docker('inspect', *before))} if before else {}
    before_networks = set(runner.docker('network', 'ls', '-q').splitlines())
    capacity = json.loads(runner.docker('info', '--format', '{{json .}}'))
    if args.workers == 2:
        assert capacity['MemTotal'] >= 6*1024**3 and capacity['NCPU'] >= 4, 'Insufficient capacity for two projects'
    if args.workers == 4:
        assert capacity['MemTotal'] >= 7*1024**3 and capacity['NCPU'] >= 8, 'Insufficient capacity for four projects'
    report = {'design': design, 'raw_backup_directory': str(work), 'image': image,
              'original_container_ids': sorted(before), 'original_volume_names': sorted(volumes),
              'original_container_states': before_states, 'original_network_ids': sorted(before_networks),
              'capacity': {k: capacity[k] for k in ('NCPU', 'MemTotal', 'ServerVersion')}, 'trials': []}
    try:
        runner.run(['docker', 'build', '-q', '-t', image, '-f', str(ROOT/'infrastructure/docker/Dockerfile'), str(ROOT)], timeout=1200)
        abort = threading.Event()
        def run_case(case):
            results = []
            for policy, admission in case.get('arms', [(p, args.admission_mode) for p in case['policies']]):
                if abort.is_set() or (args.output/'STOP').exists():
                    break
                try:
                    trial_args = copy.copy(args)
                    trial_args.admission_mode = admission
                    trial_args.live_read_rps = case.get('live_read_rps', args.live_read_rps)
                    trial_args.verify_postgres = args.mode == 'holdout' and case is cases[0] and (policy, admission) == tuple(case['arms'][0])
                    result = Trial(work, image, case, policy, volumes, trial_args).execute_trial()
                except Exception:
                    abort.set()
                    raise
                results.append(result)
                if result['result'] != 'passed':
                    abort.set()
                    break
            return results
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            pending = [pool.submit(run_case, case) for case in cases]
            try:
                for future in as_completed(pending):
                    report['trials'].extend(future.result())
                    write_json(args.output/'summary.json', report)
            except BaseException:
                abort.set()
                for future in pending:
                    future.cancel()
                raise
    finally:
        after, after_volumes = runner.inventory()
        after_states = {r['Id']: r['State']['Status'] for r in json.loads(runner.docker('inspect', *after))} if after else {}
        report['final_container_states'] = after_states
        report['container_state_changes'] = state_changes(before_states, after_states)
        report['preservation'] = {'container_identity_image_mounts_equal': before == after,
                                  'container_states_equal': before_states == after_states,
                                  'exact_network_set_equal': before_networks == set(runner.docker('network', 'ls', '-q').splitlines()),
                                  'exact_volume_set_equal': volumes == after_volumes}
        report['source_unchanged_during_run'] = design['source_sha256'] == fingerprint()
        if args.retain_test_databases:
            retained = {v for t in report['trials'] for v in t.get('retained_database_volumes', [])}
            report['volume_retention'] = {'original': sorted(volumes), 'retained': sorted(retained),
                                          'final': sorted(after_volumes)}
            report['preservation'].pop('exact_volume_set_equal')
            report['preservation']['original_volumes_preserved_only_owned_added'] = after_volumes == volumes | retained
        report['planned_trials'] = sum(len(c.get('arms', c['policies'])) for c in cases)
        report['result'] = 'passed' if len(report['trials']) == report['planned_trials'] and all(t['result'] == 'passed' for t in report['trials']) and all(report['preservation'].values()) and report['source_unchanged_during_run'] else 'failed'
        write_json(args.output/'summary.json', report)
    if report['result'] != 'passed':
        raise SystemExit('Experiment failed; failed attempts retained in summary.json')


if __name__ == '__main__':
    main()
