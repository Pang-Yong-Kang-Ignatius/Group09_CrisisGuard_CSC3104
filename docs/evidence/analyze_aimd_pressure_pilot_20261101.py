"""Read-only seed-60201 pilot review; writes only a new analysis directory."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from cloud.controller.admission import Settings, assess
from evaluation.experiments.analyze_recovery_matrix import generator_accuracy, p95, trial_metrics


def client(reads):
    failures = sum(r.get('status_code') != 200 for r in reads)
    late = sum(r.get('status_code') == 200 and r['latency_ms'] > 250 for r in reads)
    return dict(attempts=len(reads), failures=failures, late_successes=late,
        violations=failures+late, violation_fraction=(failures+late)/len(reads) if reads else None,
        successful_p95_ms=p95([r['latency_ms'] for r in reads if r.get('status_code') == 200]))


def review(root):
    paths = sorted(root.glob('run-*/summary.json'))
    assert len(paths) == 4, 'Exactly four completed pilot summaries required'
    results, hashes, traces, manifests, graphs = [], {}, [], [], []
    for path in paths:
        report = json.loads(path.read_text())
        assert report['result'] == 'passed' and report['planned_trials'] == 1
        assert report['source_unchanged_during_run'] and all(report['preservation'].values())
        assert not report['container_state_changes']
        assert len(report['trials']) == 1
        trial = report['trials'][0]
        raw = Path(trial['raw_path'])
        assert json.loads(raw.read_text()) == trial, 'Raw/summary mismatch'
        for source in (path, raw):
            hashes[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
        manifests.append(report['design']['source_sha256'])
        graphs.append(trial['workload_sha256'])
        assert trial['seed'] == 60201 and trial['integrity'] == trial['cleanup'] == 'passed'
        assert trial['result'] == 'passed' and len(trial['completion_intervals']) == 88
        assert all(s['integrity'] == 'ok' for s in trial['sqlite_recovery'].values())
        assert sum(g['status'] == 'completed' for g in trial['grants']) == 88
        config = trial['configuration']
        assert config['live_slo_ms'] == 250
        assert config['controller']['CONTROLLER_ADMISSION_HEALTH_POLICY'] == 'pressure-experimental'
        rows = trial_metrics(trial, report['design']['cases'][0]['workload'])
        accuracy = generator_accuracy(trial)
        assert accuracy['ledger_reconciled']
        ready, stop = trial['cloud_restoration_readiness_seconds'], trial['live_generator_stop_seconds']
        reads, samples = trial['live_reads'], trial['samples']
        post = [r for r in reads if r['started_elapsed'] >= ready]
        fixed = [r for r in reads if ready <= r['started_elapsed'] < ready+10]
        fresh = [s for s in samples if s['controller']['telemetry_fresh']]
        ceiling = next(s for s in fresh if s['controller']['state'] == 'Normal' and s['controller']['rate_rps'] == 20)
        transitions, previous, observations, assessments, decisions = [], None, set(), Counter(), {}
        with patch.dict('os.environ', CRISISGUARD_EXPERIMENTAL_POLICIES='1',
                        CONTROLLER_ADMISSION_HEALTH_POLICY='pressure-experimental'):
            for sample in samples:
                c = sample['controller']
                key = (c['state'], c['rate_rps'], c['telemetry_fresh'], c['reason'])
                if key != previous:
                    transitions.append(dict(elapsed_seconds=sample['elapsed_seconds'], state=key[0],
                        rate_rps=key[1], fresh=key[2], reason=key[3]))
                    previous = key
                if c['telemetry_fresh'] and c['observed_at'] not in observations:
                    observations.add(c['observed_at'])
                    tier, reason, healthy, _ = assess(c['metrics'], Settings(**c['settings']),
                                                    datetime.fromisoformat(c['server_time']))
                    assessments[str((tier, reason, healthy))] += 1
                if c['last_decision']:
                    decisions[c['last_decision']['at']] = c['last_decision']
        violations = [r for r in reads if r.get('status_code') != 200 or r['latency_ms'] > 250]
        result = dict(load_rps=config['live_read_rps'], admission=config['admission_mode'],
            source=str(path), generator=accuracy,
            generator_sensitivity={str(budget): accuracy['skip_fraction'] <= budget for budget in (0, .0001, .001)},
            drain_interval_seconds=trial['cloud_drain_interval_seconds'], readiness_seconds=ready,
            post_ready_exposure_seconds=stop-ready, whole_run=client(reads), post_ready=client(post),
            fixed10=client(fixed) if stop >= ready+10 else None,
            last_violation_completion_seconds=max((r['finished_elapsed'] for r in violations), default=None),
            workflow_p95=[{k: row[k] for k in ('site', 'urgent_original_workflows', 'routine_original_workflows',
                'urgent_workflow_p95_upper_seconds', 'routine_workflow_p95_upper_seconds')} for row in rows],
            transitions=transitions, fresh_samples=len(fresh), samples=len(samples),
            post_ready_samples=sum(s['elapsed_seconds'] >= ready for s in samples),
            post_ready_fresh_samples=sum(s['elapsed_seconds'] >= ready for s in fresh),
            distinct_fresh_assessments=dict(assessments), first_fresh_seconds=fresh[0]['elapsed_seconds'],
            first_normal_ceiling_seconds=ceiling['elapsed_seconds'],
            events_remaining_at_ceiling=88-ceiling['cloud_completed'],
            last_sampled_admission_counters={k: samples[-1]['controller'][k] for k in ('allowed', 'denied')},
            unique_sampled_decision_reasons=dict(Counter(d['reason'] for d in decisions.values())),
            retry_total=sum(r['retry_count'] for site in trial['sqlite_recovery'].values() for r in site['recovery']),
            maximum_event_retry_count=max(r['retry_count'] for site in trial['sqlite_recovery'].values() for r in site['recovery']),
            replay_errors=len(trial['replay_errors']), audit_actions=dict(Counter(a['action'] for a in trial['audit'])))
        results.append(result)
    assert all(m == manifests[0] for m in manifests) and len(set(graphs)) == 1
    pairs = []
    for load in (36, 84):
        pair = {r['admission']: r for r in results if r['load_rps'] == load}
        assert set(pair) == {'constant', 'aimd'}
        a, c = pair['aimd'], pair['constant']
        differences = []
        for ar, cr in zip(a['workflow_p95'], c['workflow_p95']):
            assert ar['site'] == cr['site']
            differences.append(dict(site=ar['site'], **{population+'_p95_relative_pct':
                100*(ar[population+'_workflow_p95_upper_seconds']/cr[population+'_workflow_p95_upper_seconds']-1)
                for population in ('urgent', 'routine')}))
        pairs.append(dict(load_rps=load,
            drain_relative_pct=100*(a['drain_interval_seconds'][1]/c['drain_interval_seconds'][1]-1),
            drain_relative_observer_bounds_pct=[100*(a['drain_interval_seconds'][0]/c['drain_interval_seconds'][1]-1),
                100*(a['drain_interval_seconds'][1]/c['drain_interval_seconds'][0]-1)],
            full_run_slo_difference_pp=100*(a['whole_run']['violation_fraction']-c['whole_run']['violation_fraction']),
            fixed10_slo_difference_pp=100*(a['fixed10']['violation_fraction']-c['fixed10']['violation_fraction'])
                if a['fixed10'] and c['fixed10'] else None, workflow_differences=differences))
    return dict(scope='One independent paired seed per load; descriptive pilot, no inferential CI or significance',
        paired_graph_and_source_manifests_equal=True, trials=results, pairs=pairs, source_sha256=hashes,
        caveats=['Denial reasons are sampled last decisions, not an exhaustive denial ledger.',
                 'Retry totals combine deferrals and failures; exact causal delay attribution is unavailable.',
                 'Retry jitter is not controlled by the workload seed.',
                 'Drain observer bounds are not statistical confidence intervals.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    data = review(args.root)
    data['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output/'assessment.json').write_text(json.dumps(data, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(data['pairs'], indent=2))


if __name__ == '__main__':
    main()
