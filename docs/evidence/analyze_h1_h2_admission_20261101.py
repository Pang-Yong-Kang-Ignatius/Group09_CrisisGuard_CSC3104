"""Read-only admission/state and exposure diagnosis of the preserved reduced study."""
import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics

from cloud.controller.admission import Settings, assess
from docs.evidence.plot_h1_h2_submission_20261101 import ROOT, verify


def live_counts(reads, threshold=250):
    failures = sum(r.get('status_code') != 200 for r in reads)
    late = sum(r.get('status_code') == 200 and r['latency_ms'] > threshold for r in reads)
    return {'attempts': len(reads), 'request_failures': failures, 'late_successes': late,
            'violations': failures+late,
            'violation_fraction': (failures+late)/len(reads) if reads else None}


def diagnose(trial):
    snapshots, trace = [], []
    floor_time = fresh_time = recovery_time = span = 0.
    seen = set()
    end = trial['cloud_drain_interval_seconds'][1]
    for i, sample in enumerate(trial['samples']):
        c = sample['controller']
        config = Settings(**c['settings'])
        now = datetime.fromisoformat(c['server_time'])
        tier, reason, healthy, _ = assess(c['metrics'], config, now)
        age = (now-datetime.fromisoformat(c['observed_at'])).total_seconds() if c['observed_at'] else None
        dt = max(0., min(end, trial['samples'][i+1]['elapsed_seconds'] if i+1 < len(trial['samples']) else end)-sample['elapsed_seconds'])
        span += dt
        recovery_time += dt*(c['state'] == 'Recovery')
        if c['telemetry_fresh']:
            fresh_time += dt
            floor_time += dt*(c['rate_rps'] == config.recovery_rps)
        if not c['telemetry_fresh'] and c.get('last_decision', {}).get('allowed'):
            # Last decision is historical; it does NOT prove admission while stale.
            pass
        key = (c['observed_at'], c['state'], c['rate_rps'], c['reason'], json.dumps(c['metrics'], sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        row = {'condition': trial['condition'], 'seed': trial['seed'], 'admission': trial['configuration']['admission_mode'],
               'elapsed_seconds': sample['elapsed_seconds'], 'state': c['state'], 'rate_rps': c['rate_rps'],
               'inflight_limit': c['inflight_limit'], 'telemetry_fresh': c['telemetry_fresh'],
               'observed_age_seconds': age, 'stored_reason': c['reason'], 'recomputed_tier': tier,
               'recomputed_reason': reason, 'healthy': healthy}
        for name in ('patient', 'emergency', 'admin'):
            for metric in ('throughput_rps', 'average_latency_ms', 'error_rate'):
                row[name+'_'+metric] = c['metrics'].get(name, {}).get(metric)
        trace.append(row)
        snapshots.append((tier, reason, healthy))
    ready = trial['cloud_restoration_readiness_seconds']
    pre = [r for r in trial['live_reads'] if r['started_elapsed'] < ready]
    post = [r for r in trial['live_reads'] if r['started_elapsed'] >= ready]
    return {'condition': trial['condition'], 'seed': trial['seed'], 'admission': trial['configuration']['admission_mode'],
            'drain_seconds': end, 'readiness_seconds': ready, 'live_stop_seconds': trial['live_generator_stop_seconds'],
            'fresh_observed_seconds': fresh_time, 'fresh_time_at_rate_floor_fraction': floor_time/fresh_time if fresh_time else None,
            'observed_recovery_fraction': recovery_time/span if span else None,
            'throughput_trigger_snapshots': sum('throughput_rps' in r for _, r, _ in snapshots),
            'missing_or_stale_snapshots': sum(r == 'telemetry_missing_invalid_or_stale' for _, r, _ in snapshots),
            'healthy_snapshots': sum(h for _, _, h in snapshots),
            'rates_observed': sorted({r['rate_rps'] for r in trace}),
            'retry_journal_total': sum(r['retry_count'] for site in trial['sqlite_recovery'].values() for r in site['recovery']),
            'whole_run': live_counts(trial['live_reads']), 'before_readiness': live_counts(pre), 'after_readiness': live_counts(post)}, trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory only')
    args = parser.parse_args()
    _, _, _, sources = verify()
    trials, summaries, traces = [], [], []
    for source in sources[:-2]:
        for trial in json.loads(source.read_text())['trials']:
            if trial['policy'] != 'bounded_on':
                continue
            trials.append(trial)
            summary, trace = diagnose(trial)
            summary['source_summary'] = str(source.relative_to(ROOT))
            summary['raw_trial_sha256'] = hashlib.sha256(Path(trial['raw_path']).read_bytes()).hexdigest()
            summaries.append(summary)
            traces.extend(trace)
    assert len(summaries) == 20
    matched_windows = []
    for condition in sorted({t['condition'] for t in trials}):
        for seed in range(3104, 3109):
            pair = [t for t in trials if t['condition'] == condition and t['seed'] == seed]
            assert len(pair) == 2
            length = min(t['live_generator_stop_seconds']-t['cloud_restoration_readiness_seconds'] for t in pair)
            row = {'condition': condition, 'seed': seed, 'common_post_ready_seconds': length}
            for t in pair:
                ready = t['cloud_restoration_readiness_seconds']
                row[t['configuration']['admission_mode']] = live_counts([r for r in t['live_reads'] if ready <= r['started_elapsed'] < ready+length])
            matched_windows.append(row)
    args.output.mkdir(parents=True, exist_ok=False)
    output = {'scope': 'Retrospective diagnosis only; original primary outcomes unchanged', 'trials': summaries,
              'matched_post_ready_windows': matched_windows,
              'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (args.output/'diagnosis.json').write_text(json.dumps(output, indent=2)+'\n', encoding='utf-8')
    with (args.output/'admission-trace.csv').open('x', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(traces[0])); writer.writeheader(); writer.writerows(traces)
    for load in ('30', '70'):
        for mode in ('constant', 'aimd'):
            selected = [s for s in summaries if s['condition'].endswith(load) and s['admission'] == mode]
            print(load, mode, 'mean drain', round(statistics.fmean(s['drain_seconds'] for s in selected), 3),
                  'violations', sum(s['whole_run']['violations'] for s in selected),
                  'post-ready violations', sum(s['after_readiness']['violations'] for s in selected),
                  'AIMD-floor fresh fractions', [round(s['fresh_time_at_rate_floor_fraction'], 4) for s in selected])


if __name__ == '__main__':
    main()
