"""Verify independent calibration and summarize startup/post-readiness phases."""
import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path

from cloud.controller.admission import Settings, assess
from evaluation.experiments.analyze_recovery_matrix import generator_accuracy, p95


def client_phase(reads):
    success = [r for r in reads if r.get('status_code') == 200]
    return {'requests': len(reads), 'transport_failures': sum('status_code' not in r for r in reads),
            'http_failures': sum('status_code' in r and r['status_code'] != 200 for r in reads),
            'late_successes': sum(r['latency_ms'] > 250 for r in success),
            'client_p95_ms': p95([r['latency_ms'] for r in reads]),
            'successful_p95_ms': p95([r['latency_ms'] for r in success]),
            'start_lag_p95_ms': p95([r['start_lag_ms'] for r in reads])}


def bounds(values):
    finite = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return {'minimum': min(finite), 'maximum': max(finite)} if finite else None


def analyze(paths):
    results = []
    for path in paths:
        raw = json.loads(path.read_text())
        preserved = bool(raw.get('preservation')) and all(raw['preservation'].values())
        assert preserved and raw['source_unchanged_during_run']
        assert raw['planned_trials'] == len(raw['trials']) == 2
        for trial in raw['trials']:
            assert trial['result'] == trial['cleanup'] == trial['integrity'] == 'passed'
            expected = {e['event_id'] for e in trial['expected_events']}
            assert len(expected) == 176 and set(trial['completion_intervals']) == expected
            accuracy = generator_accuracy(trial)
            assert accuracy['ledger_reconciled']
            assert trial['configuration']['admission_mode'] == 'constant'
            ready = trial['cloud_restoration_readiness_seconds']
            row = {'source': path.as_posix(), 'seed': trial['seed'],
                   'read_rps': trial['configuration']['live_read_rps'], 'generator': accuracy,
                   'valid_at_999_issuance': accuracy['skip_fraction'] <= .001,
                   'valid_at_9999_issuance': accuracy['skip_fraction'] <= .0001,
                   'valid_at_zero_skips': accuracy['skip_fraction'] == 0,
                   'completed_events': len(expected), 'preservation_passed': preserved,
                   'readiness_seconds': ready, 'drain_seconds': trial['cloud_drain_interval_seconds'][1],
                   'post_ready_duration_seconds': trial['live_generator_stop_seconds']-ready, 'phases': {}}
            for phase in ('startup', 'post_ready'):
                reads = [r for r in trial['live_reads'] if (r['started_elapsed'] >= ready) == (phase == 'post_ready')]
                samples = [s for s in trial['samples'] if (s['elapsed_seconds'] >= ready) == (phase == 'post_ready')]
                states, reasons = {}, {}
                for sample in samples:
                    c = sample['controller']
                    _, reason, _, _ = assess(c['metrics'], Settings(**c['settings']), datetime.fromisoformat(c['server_time']))
                    states[c['state']] = states.get(c['state'], 0)+1
                    reasons[reason] = reasons.get(reason, 0)+1
                metrics = {}
                for name in ('patient', 'emergency', 'admin'):
                    metrics[name] = {field: bounds([s['controller']['metrics'].get(name, {}).get(field) for s in samples])
                                     for field in ('throughput_rps', 'average_latency_ms', 'p95_latency_ms',
                                                   'error_rate', 'live_p95_latency_ms', 'live_slo_violation_rate', 'executor_queue_depth')}
                row['phases'][phase] = {'client': client_phase(reads), 'controller_snapshots': len(samples),
                                       'fresh_snapshots': sum(s['controller']['telemetry_fresh'] for s in samples),
                                       'states': states, 'decision_reasons': reasons,
                                       'rates_observed': sorted({s['controller']['rate_rps'] for s in samples}),
                                       'cloud_metrics': metrics}
            results.append(row)
    assert {r['seed'] for r in results} == {50101, 50102} and len(results) == 4
    return {'decision': 'further calibration required', 'profile': None,
            'reason': 'Healthy post-ready demand only; no sustained overloaded calibration class. Legacy throughput thresholds yield false positives.',
            'trials': results, 'sources_sha256': {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = analyze([args.root/f'run-{rate}/summary.json' for rate in (60, 120)])
    result['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output/'assessment.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    rows = []
    for trial in result['trials']:
        for phase, data in trial['phases'].items():
            rows.append({'read_rps': trial['read_rps'], 'seed': trial['seed'], 'phase': phase,
                         'post_ready_duration_seconds': trial['post_ready_duration_seconds'], **data['client'],
                         'controller_snapshots': data['controller_snapshots'], 'fresh_snapshots': data['fresh_snapshots'],
                         'cap_skips_whole_trial': trial['generator']['cap_skips'], 'lag_skips_whole_trial': trial['generator']['lag_skips']})
    with (args.output/'phases.csv').open('x', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    print('Verified four preserved, complete trials; decision: further calibration required. No threshold profile generated.')


if __name__ == '__main__':
    main()
