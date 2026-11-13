"""H5 assessment: retain primary outcomes and separate fixed-readiness diagnostics."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics

from evaluation.experiments.assess_acceptance import assess
from evaluation.experiments.analyze_recovery_matrix import p95, paired


def fixed_window(trial, seconds=10):
    ready = trial.get('cloud_restoration_readiness_seconds')
    stop = trial.get('live_generator_stop_seconds')
    if ready is None or stop is None or stop < ready + seconds:
        return dict(status='incomplete', attempts=0, violations=None, fraction=None)
    reads = [r for r in trial['live_reads'] if ready <= r['started_elapsed'] < ready + seconds]
    threshold = trial['configuration']['live_slo_ms']
    violations = sum(r.get('status_code') != 200 or r['latency_ms'] > threshold for r in reads)
    return dict(status='complete' if reads else 'no observations',
        readiness_seconds=ready, seconds=seconds, attempts=len(reads), violations=violations,
        fraction=violations/len(reads) if reads else None,
        successful_p95_ms=p95([r['latency_ms'] for r in reads if r.get('status_code') == 200]))


def assessment(raw):
    result = assess([raw])
    result['comparisons'] = [c for c in result['comparisons']
        if c['policy'] == 'bounded_on' and c['admission'] == 'aimd'
        and (('ratio1-1_' in c['condition'] and c['comparison'] == 'admission')
             or ('ratio10-1_' in c['condition'] and c['comparison'] == 'scheduler'))]
    trials = {(t['condition'], t['seed'], t['policy'], t['configuration']['admission_mode']): t for t in raw['trials']}
    diagnostics = []
    for case in raw['design']['cases']:
        key = (case['condition'], case['seed'])
        candidate = trials.get((*key, 'bounded_on', 'aimd'))
        control = trials.get((*key, 'bounded_on', 'constant') if case['comparison'] == 'admission' else (*key, 'rr_on', 'aimd'))
        a = fixed_window(candidate) if candidate else dict(status='missing', fraction=None)
        b = fixed_window(control) if control else dict(status='missing', fraction=None)
        diagnostics.append(dict(condition=key[0], seed=key[1], comparison=case['comparison'],
            candidate=a, control=b, secondary_difference_pp=100*(a['fraction']-b['fraction'])
                if a['fraction'] is not None and b['fraction'] is not None else None))
    result['fixed10_secondary'] = diagnostics
    result['fixed10_summaries'] = []
    for condition in sorted({r['condition'] for r in diagnostics}):
        rows = [r for r in diagnostics if r['condition'] == condition]
        values = [r['secondary_difference_pp'] for r in rows if r['secondary_difference_pp'] is not None]
        result['fixed10_summaries'].append(dict(condition=condition, n=len(values),
            mean_difference_pp=statistics.fmean(values) if values else None, uncertainty=paired(values),
            scope='Secondary fixed-readiness window; never replaces whole-run SLO acceptance'))
    result['protocol'] = raw['design']['acceptance_protocol']
    result['outcomes'] = [dict(condition=t['condition'], seed=t['seed'], policy=t['policy'],
        admission=t['configuration']['admission_mode'], result=t['result'], cleanup=t.get('cleanup'),
        observed_events=len(t['completion_intervals']), error=t.get('error'),
        duplicate_probe_status=t.get('duplicate_probe_status'),
        deferrals_by_site={site: len(rows) for site, rows in t.get('admission_deferrals', {}).items()}) for t in raw['trials']]
    result['source_sha256'] = raw['design']['source_sha256']
    result['preservation'] = raw['preservation']
    result['source_unchanged_during_run'] = raw['source_unchanged_during_run']
    result['limitations'] = [
        'Two workload shapes at two provisional read loads, not full D2 or maximum sustainable mixed capacity.',
        'Pressure-health and bounded-deferral are experimental and require component review; healthy-load recovery is not overload protection.',
        'Routine workflow completion after observed reconnection is a waiting proxy; no isolated scheduler CPU cost.',
        'Five paired seeds cannot give a two-sided exact sign-flip p-value below .0625.',
        'A-only urgency gives no urgent B population. Both hospitals routine outcomes are retained.',
        'Whole-run end-to-end SLO is primary. Fixed ten-second readiness-aligned diagnostics are secondary.'
    ]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('summary', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.summary.read_text())
    result = assessment(raw)
    result['summary_sha256'] = hashlib.sha256(args.summary.read_bytes()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output/'assessment.json').write_text(json.dumps(result, indent=2)+'\n')
    for name, rows in (('measurements', result['per_site_measurements']),
                       ('comparisons', [{k:v for k,v in c.items() if k not in ('per_seed', 'uncertainty', 'generator_accuracy_sensitivity')}
                                         for c in result['comparisons']]),
                       ('paired-effects', [dict(condition=c['condition'], comparison=c['comparison'], site=c['site'], metric=c['metric'],
                                               **p) for c in result['comparisons'] for p in c['per_seed']])):
        if rows:
            with (args.output/f'{name}.csv').open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    print(json.dumps([dict(condition=c['condition'], metric=c['metric'], site=c['site'], n=c['uncertainty']['n'],
                           effect=c['mean'], status=c['status']) for c in result['comparisons']], indent=2))


if __name__ == '__main__':
    main()
