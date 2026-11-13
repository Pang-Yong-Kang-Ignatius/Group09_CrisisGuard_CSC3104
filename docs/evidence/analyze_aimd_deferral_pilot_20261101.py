"""Verify the independent seed-60202 retry pilot without running services."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path

from evaluation.experiments.analyze_recovery_matrix import generator_accuracy, p95, trial_metrics


def client(reads):
    failures = sum(r.get('status_code') != 200 for r in reads)
    late = sum(r.get('status_code') == 200 and r['latency_ms'] > 250 for r in reads)
    return dict(attempts=len(reads), http_non_200=sum('status_code' in r and r['status_code'] != 200 for r in reads),
        transport_failures=sum('status_code' not in r for r in reads), late_successes=late,
        violations=failures+late, fraction=(failures+late)/len(reads) if reads else None,
        successful_p95_ms=p95([r['latency_ms'] for r in reads if r.get('status_code') == 200]))


def review(root):
    paths = sorted(root.glob('run-*/summary.json'))
    assert len(paths) == 2
    results, hashes, manifests, graphs, configs = {}, {}, [], [], []
    for path in paths:
        d = json.loads(path.read_text())
        assert d['result'] == 'passed' and d['planned_trials'] == len(d['trials']) == 1
        assert d['source_unchanged_during_run'] and all(d['preservation'].values())
        assert not d['container_state_changes']
        t = d['trials'][0]
        assert t['result'] == t['integrity'] == t['cleanup'] == 'passed' and t['seed'] == 60202
        raw = Path(t['raw_path'])
        assert json.loads(raw.read_text()) == t
        for source in (path, raw):
            hashes[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
        manifests.append(d['design']['source_sha256'])
        workload = d['design']['cases'][0]['workload']
        graph = hashlib.sha256(json.dumps(workload, sort_keys=True).encode()).hexdigest()
        assert graph == t['workload_sha256']
        graphs.append(graph)
        ids = {e['event_id'] for e in workload['events']}
        assert len(ids) == 88 and set(t['completion_intervals']) == ids
        assert {e['event_id'] for e in t['postgres_events']} == ids
        assert sum(g['status'] == 'completed' for g in t['grants']) == 88
        assert all(s['integrity'] == 'ok' and all(not r['quarantined'] and not r['in_flight'] for r in s['recovery'])
                   for s in t['sqlite_recovery'].values())
        config = t['configuration']
        assert config['live_slo_ms'] == 250 and config['live_read_rps'] == 84 and config['admission_mode'] == 'aimd'
        assert config['controller']['CONTROLLER_ADMISSION_HEALTH_POLICY'] == 'pressure-experimental'
        policy = config['metaguard_retry_policy']
        configs.append({k:v for k,v in config.items() if k not in ('metaguard_retry_policy', 'loopback_ports')})
        accuracy = generator_accuracy(t)
        assert accuracy['ledger_reconciled']
        ready, stop = t['cloud_restoration_readiness_seconds'], t['live_generator_stop_seconds']
        fixed = [r for r in t['live_reads'] if ready <= r['started_elapsed'] < ready+10]
        samples = t['samples']
        fresh = [s for s in samples if s['controller']['telemetry_fresh']]
        post_samples = [s for s in samples if s['elapsed_seconds'] >= ready]
        ceiling = next(s for s in fresh if s['controller']['state'] == 'Normal' and s['controller']['rate_rps'] == 20)
        transitions, previous, decisions = [], None, {}
        for s in samples:
            c = s['controller']
            key = (c['state'], c['rate_rps'], c['telemetry_fresh'], c['reason'])
            if key != previous:
                transitions.append(dict(elapsed_seconds=s['elapsed_seconds'], state=key[0], rate_rps=key[1], fresh=key[2], reason=key[3]))
                previous = key
            if c['last_decision']:
                decisions[c['last_decision']['at']] = c['last_decision']
        deferrals = t.get('admission_deferrals', {})
        waits = [(datetime.fromisoformat(r['retry_after'])-datetime.fromisoformat(r['deferred_at'])).total_seconds()
                 for records in deferrals.values() for r in records]
        metrics = [m for s in fresh for m in s['controller']['metrics'].values()]
        ranges = {k: [min(m[k] for m in metrics if m.get(k) is not None), max(m[k] for m in metrics if m.get(k) is not None)]
                  for k in ('p95_latency_ms', 'live_p95_latency_ms', 'error_rate', 'executor_queue_pressure', 'live_slo_violation_rate')}
        results[policy] = dict(source=str(path), generator=accuracy,
            generator_sensitivity={str(b): accuracy['skip_fraction'] <= b for b in (0, .0001, .001)},
            drain_interval_seconds=t['cloud_drain_interval_seconds'], readiness_seconds=ready,
            post_ready_exposure_seconds=stop-ready, full_run=client(t['live_reads']),
            fixed10=client(fixed) if stop >= ready+10 else None,
            post_ready=client([r for r in t['live_reads'] if r['started_elapsed'] >= ready]),
            workflow_p95=[{k:r[k] for k in ('site','urgent_original_workflows','routine_original_workflows',
                'urgent_workflow_p95_upper_seconds','routine_workflow_p95_upper_seconds')}
                for r in trial_metrics(t,workload)], transitions=transitions,
            first_normal_ceiling_seconds=ceiling['elapsed_seconds'], events_remaining_at_ceiling=88-ceiling['cloud_completed'],
            fresh_post_ready_snapshots=sum(s['controller']['telemetry_fresh'] for s in post_samples),
            post_ready_snapshots=len(post_samples), fresh_metric_ranges=ranges,
            last_sampled_counters={k:samples[-1]['controller'][k] for k in ('allowed','denied')},
            sampled_unique_decision_reasons=dict(Counter(r['reason'] for r in decisions.values())),
            recorded_deferrals_by_site={k:len(v) for k,v in deferrals.items()},
            recorded_deferral_reasons=dict(Counter(r['reason'] for v in deferrals.values() for r in v)),
            deferral_wait_range_seconds=[min(waits),max(waits)] if waits else None,
            failure_backoff_counters_by_site={k:sum(r['retry_count'] for r in v['recovery']) for k,v in t['sqlite_recovery'].items()},
            max_event_failure_counter=max(r['retry_count'] for v in t['sqlite_recovery'].values() for r in v['recovery']),
            grant_statuses=dict(Counter(g['status'] for g in t['grants'])),
            audit_actions=dict(Counter(a['action'] for a in t['audit'])), replay_loop_errors=len(t['replay_errors']))
    assert manifests[0] == manifests[1] and graphs[0] == graphs[1] and configs[0] == configs[1]
    assert set(results) == {'failure-backoff', 'bounded-deferral'}
    old, new = results['failure-backoff'], results['bounded-deferral']
    effects = dict(drain_relative_pct=100*(new['drain_interval_seconds'][1]/old['drain_interval_seconds'][1]-1),
        drain_relative_observer_bounds_pct=[100*(new['drain_interval_seconds'][0]/old['drain_interval_seconds'][1]-1),
            100*(new['drain_interval_seconds'][1]/old['drain_interval_seconds'][0]-1)],
        full_run_slo_difference_pp=100*(new['full_run']['fraction']-old['full_run']['fraction']),
        fixed10_slo_difference_pp=100*(new['fixed10']['fraction']-old['fixed10']['fraction']) if new['fixed10'] and old['fixed10'] else None,
        workflow_differences=[dict(site=n['site'], **{p+'_p95_relative_pct':
            100*(n[p+'_workflow_p95_upper_seconds']/o[p+'_workflow_p95_upper_seconds']-1) for p in ('urgent','routine')})
            for n,o in zip(new['workflow_p95'],old['workflow_p95'])])
    return dict(scope='Single paired seed 60202 at 84 RPS; descriptive experimental retry comparison, not proposal acceptance',
        paired_workload_and_source_and_other_configuration_equal=True, trials=results, effects=effects,
        source_sha256=hashes, recorded_source_manifest=manifests[0],
        limitations=['No inferential confidence interval or statistical significance with one seed.',
            'Failure-backoff counters include unclassified waits and ambiguous outcomes; they are not genuine-failure counts.',
            'Sampled last-decision reasons do not form a complete denial/attempt ledger.',
            'No matched constant-rate arm or RR baseline; original negative results remain unchanged.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    data=review(args.root)
    data['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with args.output.open('x',encoding='utf-8') as f:
        f.write(json.dumps(data,indent=2)+'\n')
    print(json.dumps(data['effects'],indent=2))


if __name__=='__main__':
    main()
