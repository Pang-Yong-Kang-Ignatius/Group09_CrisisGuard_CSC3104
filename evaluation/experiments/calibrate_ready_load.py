"""Bounded readiness-first, read-only calibration. No production settings change."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import time
from urllib.parse import quote, unquote, urlsplit

import httpx


def percentile(values):
    return sorted(values)[math.ceil(.95*len(values))-1] if values else None


def base_url(value):
    u = urlsplit(value)
    if (u.scheme != 'http' or u.hostname not in ('localhost', '127.0.0.1', '::1')
            or not u.port or u.username or u.password or u.query or u.fragment or u.path not in ('', '/')):
        raise ValueError('Calibration endpoints must be explicit localhost HTTP base URLs')
    return value.rstrip('/')


def client_summary(outcomes, offered):
    issued = [r for r in outcomes if 'skip' not in r]
    success = [r for r in issued if r.get('status') == 200]
    failures = sum(r.get('status') != 200 for r in issued)
    violations = sum(r.get('status') != 200 or r['latency_ms'] > 250 for r in issued)
    cap = sum(r.get('skip') == 'cap' for r in outcomes)
    lag = sum(r.get('skip') == 'lag' for r in outcomes)
    withheld = sum(r.get('skip') == 'safety' for r in outcomes)
    valid = offered > 0 and len(outcomes) == offered and len(issued)+cap+lag == offered and not withheld and (cap+lag)/offered <= .001
    return {'scheduled': offered, 'issued': len(issued), 'cap_skips': cap, 'lag_skips': lag,
            'safety_withheld': withheld,
            'generator_valid': valid, 'issuance_ratio': len(issued)/offered if offered else None,
            'http_non_200': sum('status' in r and r['status'] != 200 for r in issued),
            'transport_failures': sum('status' not in r for r in issued),
            'successful_p95_ms': percentile([r['latency_ms'] for r in success]),
            'start_lag_p95_ms': percentile([r['lag_ms'] for r in issued]),
            'failure_fraction': failures/len(issued) if issued else None,
            'slo_violation_fraction': violations/len(issued) if issued else None}


def health_snapshot(cloud, controller, now):
    """Independent health labels; throughput never determines healthy/overloaded."""
    fresh = controller.get('telemetry_fresh') is True
    pressure = False
    try:
        age_limit = controller['settings']['max_age_seconds']
        for name in ('patient', 'emergency', 'admin'):
            m = cloud[name]
            age = (now-datetime.fromisoformat(m['sampled_at'])).total_seconds()
            fresh &= m['service'] == name and -2 <= age <= age_limit and m['database_healthy'] is True
            for key in ('error_rate', 'executor_queue_pressure'):
                if type(m[key]) not in (float, int) or not math.isfinite(m[key]) or not 0 <= m[key] <= 1:
                    raise ValueError('invalid metric')
            p95 = m.get('live_p95_latency_ms')
            if p95 is not None and (type(p95) not in (int, float) or not math.isfinite(p95) or p95 < 0):
                raise ValueError('invalid percentile')
            pressure |= m['error_rate'] > .01 or m['executor_queue_pressure'] > 0 or (p95 is not None and p95 > 250)
        return {'fresh': bool(fresh), 'pressure': bool(pressure)}
    except (KeyError, ValueError, TypeError, OverflowError):
        return {'fresh': False, 'pressure': False}


def classify(summary, samples):
    if not summary['generator_valid']:
        return 'generator_invalid'
    if not samples or not all(s['health']['fresh'] for s in samples):
        return 'telemetry_invalid'
    client_bad = summary['failure_fraction'] > .01 or summary['slo_violation_fraction'] > .01 or (summary['successful_p95_ms'] or 0) > 250
    pressure = any(s['health']['pressure'] for s in samples)
    if client_bad and pressure:
        return 'overload_candidate'  # requires repeated consecutive-bin support
    if client_bad:
        return 'client_path_degraded_without_cloud_confirmation'
    return 'cloud_pressure_without_client_slo_failure' if pressure else 'healthy_observed'


async def observe(client, clouds, controller_url, operator):
    async def get(url, headers=None):
        try:
            async with asyncio.timeout(2):
                r = await client.get(url, headers=headers)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError, TimeoutError):
            return {}
    values = await asyncio.gather(*(get(url+'/metrics') for url in clouds.values()),
                                  get(controller_url+'/admission/status', {'X-Cloud-Token': operator}))
    # Keep only documented public metric fields, never arbitrary response bodies.
    fields = ('service', 'sampled_at', 'database_healthy', 'average_latency_ms', 'p95_latency_ms',
              'live_p95_latency_ms', 'live_latency_sample_count', 'live_slo_violation_rate',
              'throughput_rps', 'error_rate', 'executor_queue_depth', 'executor_queue_pressure')
    cloud = {name: {k:v for k,v in value.items() if k in fields} if isinstance(value, dict) else {}
             for name, value in zip(clouds, values[:3])}
    source = values[3] if isinstance(values[3], dict) else {}
    controller = {k:v for k,v in source.items() if k in ('state', 'rate_rps', 'reason', 'telemetry_fresh', 'inflight_limit')}
    settings = source.get('settings')
    controller['settings'] = {'max_age_seconds': settings.get('max_age_seconds') if isinstance(settings, dict) else None}
    health = health_snapshot(cloud, controller, datetime.now(timezone.utc))
    return {'cloud': cloud, 'controller': controller, 'health': health}


async def fixed_window(client, monitor, urls, clouds, controller, operator, headers, rate, duration, workers,
                       timeout=2., sample_interval=1., safety=True):
    """Fixed [0,duration) schedule; late/unissued arrivals remain skips."""
    start = time.monotonic()
    offered = math.floor(rate*duration)
    outcomes, samples, tasks = [], [], []
    slots = asyncio.Semaphore(workers)
    abort = asyncio.Event()

    async def read(index, due):
        before = time.monotonic()-start
        try:
            if before >= duration or abort.is_set():
                outcomes.append({'index': index, 'skip': 'safety' if abort.is_set() else 'lag'})
                return
            row = {'index': index, 'started_seconds': before, 'lag_ms': max(0., 1000*(before-due))}
            try:
                async with asyncio.timeout(timeout):
                    response = await client.get(urls[index % len(urls)], headers=headers)
                row['status'] = response.status_code
            except (httpx.HTTPError, TimeoutError) as exc:
                row['error'] = type(exc).__name__  # no exception text/headers
            row['latency_ms'] = 1000*(time.monotonic()-start-before)
            outcomes.append(row)
        finally:
            slots.release()

    async def sample():
        bad_bins = 0
        last_bin = 0
        while time.monotonic()-start < duration and not abort.is_set():
            s = await observe(monitor, clouds, controller, operator)
            patient = s['cloud']['patient']
            if not patient.get('live_latency_sample_count') or patient.get('live_p95_latency_ms') is None:
                s['health']['fresh'] = False  # do not call unclassified traffic a live calibration
            s['elapsed_seconds'] = time.monotonic()-start
            samples.append(s)
            current_bin = int(s['elapsed_seconds']//10)
            if current_bin > last_bin:
                bin_rows = [r for r in outcomes if 'skip' not in r and last_bin*10 <= r['started_seconds'] < (last_bin+1)*10]
                b = client_summary(bin_rows, len(bin_rows))
                bad = bool(bin_rows) and (b['failure_fraction'] > .01 or b['slo_violation_fraction'] > .01 or (b['successful_p95_ms'] or 0) > 250)
                bad_bins = bad_bins+1 if bad else 0
                last_bin = current_bin
            # Freshness/database protection is independent of performance labels.
            if safety and (not s['health']['fresh'] or bad_bins >= 3):
                abort.set()
                break
            await asyncio.sleep(min(sample_interval, max(0., duration-(time.monotonic()-start))))

    sampler = asyncio.create_task(sample())
    try:
        for index in range(offered):
            due = index/rate
            while time.monotonic()-start < due and not abort.is_set():
                await asyncio.sleep(max(0., start+due-time.monotonic()))
            if abort.is_set() or time.monotonic()-start >= duration:
                outcomes.extend({'index': i, 'skip': 'safety' if abort.is_set() else 'lag'} for i in range(index, offered))
                break
            if slots.locked():
                outcomes.append({'index': index, 'skip': 'cap'})
                continue
            await slots.acquire()
            tasks.append(asyncio.create_task(read(index, due)))
        await asyncio.gather(*tasks)
        await sampler
    finally:
        if not sampler.done():
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
    outcomes.sort(key=lambda r:r['index'])
    summary = client_summary(outcomes, offered)
    summary['unique_schedule_indices'] = sorted(r['index'] for r in outcomes) == list(range(offered))
    summary['issued_within_window'] = all(0 <= r['started_seconds'] < duration for r in outcomes if 'skip' not in r)
    summary['no_early_issue'] = all(r['started_seconds']+1e-9 >= r['index']/rate for r in outcomes if 'skip' not in r)
    summary['generator_valid'] &= all(summary[k] for k in ('unique_schedule_indices', 'issued_within_window', 'no_early_issue'))
    return {'rate_rps': rate, 'planned_seconds': duration, 'elapsed_seconds': time.monotonic()-start,
            'safety_stopped': abort.is_set(), 'summary': summary,
            'classification': 'safety_stopped_incomplete_window' if abort.is_set() else classify(summary, samples),
            'outcomes': outcomes, 'samples': samples}


async def execute(args, report, save):
    live = os.getenv('CLOUD_LIVE_TELEMETRY_TOKEN', '')
    operator = os.getenv('CONTROLLER_CLOUD_TOKEN', '')
    if len(live) < 32 or len(operator) < 32 or live == operator:
        raise ValueError('Distinct existing live/operator credentials are required in environment variables')
    headers = {'X-CrisisGuard-Traffic': 'live', 'X-CrisisGuard-Telemetry-Token': live}
    clouds = dict(zip(('patient', 'emergency', 'admin'), (args.patient, args.emergency, args.admin)))
    urls = [args.patient+'/events/'+quote(eid, safe='') for eid in args.event_ids.split(',')]
    random.Random(args.seed).shuffle(urls)
    async with httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=False,
                                 limits=httpx.Limits(max_connections=args.workers, max_keepalive_connections=args.workers)) as client, \
            httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=False) as monitor:
        deadline = time.monotonic()+30
        while True:
            status = await observe(monitor, clouds, args.controller, operator)
            if status['health']['fresh']:
                break
            if time.monotonic() >= deadline:
                raise ValueError('Readiness/freshness not established within 30 seconds')
            await asyncio.sleep(1)
        for url in urls:
            r = await client.get(url, headers=headers)
            body = r.json() if r.status_code == 200 else None
            if (not isinstance(body, dict) or body.get('status') != 'applied'
                    or body.get('event_id') != unquote(url.rsplit('/', 1)[1])
                    or body.get('event_type') not in ('patient_registration', 'encounter_creation')):
                raise ValueError('Each target must be an existing applied Patient record')
        for rate in args.rates:
            step = {'rate_rps': rate}
            report['steps'].append(step)
            for phase in ('warmup', 'measurement'):
                step[phase] = await fixed_window(client, monitor, urls, clouds, args.controller, operator,
                    headers, rate, getattr(args, phase), args.workers)
                save()
                result = step[phase]
                if result['safety_stopped'] or not result['summary']['generator_valid']:
                    report['decision'] = 'stopped: unsafe or generator-invalid; no profile justified'
                    save()
                    return
                # A client SLO failure is not automatically a Cloud overload label.
                if result['classification'] != 'healthy_observed':
                    report['decision'] = 'degradation observed: review phase traces; no automatic profile'
                    save()
                    return
    report['decision'] = 'healthy classes only; further calibration required; no profile justified'
    save()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name, port in (('patient', 8001), ('emergency', 8003), ('admin', 8004), ('controller', 8005)):
        p.add_argument('--'+name, type=base_url, default=f'http://127.0.0.1:{port}')
    p.add_argument('--event-ids', required=True, help='Comma-separated existing applied Patient IDs; never create records')
    p.add_argument('--rates', type=float, nargs='+', default=[120, 140])
    p.add_argument('--warmup', type=int, default=60)
    p.add_argument('--measurement', type=int, default=60)
    p.add_argument('--workers', type=int, default=128)
    p.add_argument('--seed', type=int, default=70101)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--plan-only', action='store_true')
    args = p.parse_args()
    if (args.output.exists() or not 1 <= len(args.rates) <= 2 or any(not math.isfinite(r) or not 0 < r <= 160 for r in args.rates)
            or args.rates != sorted(set(args.rates)) or args.warmup != 60 or args.measurement != 60
            or not 1 <= args.workers <= 128 or len(args.event_ids.split(',')) > 20
            or any(not eid or len(eid)>256 for eid in args.event_ids.split(',')) or args.seed < 0
            or args.seed in range(3104,3109) or args.seed in range(60101,60106)):
        p.error('Use new output, 1-2 increasing rates <=160, fixed 60/60-second windows, 1..128 workers and 1..20 existing IDs')
    public = {k:v for k,v in vars(args).items() if k not in ('event_ids', 'output')}
    report = {'schema': 'readiness-fixed-window-v1', 'configuration': public, 'steps': [],
              'target_ids_sha256': hashlib.sha256(args.event_ids.encode()).hexdigest(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'notes': ['Read-only calibration; no production/AWS configuration changes.',
                        'Seed shuffles record rotation; one session is not multiple independent replicates.',
                        'Client limits are calibration labels, not changed D3 acceptance targets.',
                        'No throughput thresholds are inferred automatically.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as f:
        json.dump(report, f, indent=2)
    def save():
        # Only update this newly-created run's checkpoint, never prior evidence.
        temp = args.output.with_suffix(args.output.suffix+'.tmp')
        temp.write_text(json.dumps(report, indent=2), encoding='utf-8')
        temp.replace(args.output)
    if args.plan_only:
        print('Plan only: up to 30s readiness + two 60s warmup/60s measurement steps + bounded request drain; no traffic sent')
        return
    try:
        asyncio.run(execute(args, report, save))
    except (ValueError, httpx.HTTPError):
        report['decision'] = 'readiness, credential or target validation failed; no profile justified'
        save()
        raise SystemExit(report['decision'])
    print(report['decision'])


if __name__ == '__main__':
    main()
