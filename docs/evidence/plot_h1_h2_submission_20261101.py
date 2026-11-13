"""Verify the completed reduced study and reproduce its figure without Docker.

Run from the repository root with --output-dir pointing to new artifact paths.
Existing outputs are never overwritten. CSV contains all plotted paired effects.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

from evaluation.experiments.assess_acceptance import assess

ROOT = Path(__file__).resolve().parents[2]
FINAL = ROOT / 'evaluation/results/replacement-3108-constant-20261101-160903'
STUDY = ROOT / 'evaluation/results/h1-h2-submission-20261101-151506'
PREFIX = 'cp8-reduced-admission-final-20261101'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def equivalent(a, b):
    # Container-resource summation order can differ at floating-point roundoff.
    if isinstance(a, float) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-10)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equivalent(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equivalent(x, y) for x, y in zip(a, b))
    return a == b


def verify():
    affected = STUDY / 'ratio1-1_depth3_balanced_load70-seed3108/summary.json'
    view = FINAL / 'original-seed3108-analysis-selection.json'
    selection = read(view)
    assert selection['analysis_selection']['source_sha256'] == hashlib.sha256(affected.read_bytes()).hexdigest()
    original = read(affected)
    assert selection['trials'] == [t for t in original['trials'] if t['configuration']['admission_mode'] == 'aimd']
    paths = [ROOT / 'evaluation/results/h1-h2-corrected-20261101-143329/ratio1-1_depth3_balanced_load30-seed3104/summary.json']
    paths += [p for p in sorted(STUDY.rglob('summary.json')) if p != affected]
    paths += [view, FINAL / 'summary.json']
    reports = [read(p) for p in paths]
    assessment_path = FINAL / 'submission-assessment.json'
    recorded = read(assessment_path)
    recalculated = assess(reports, policies=['bounded_on'])
    assert equivalent(recalculated, recorded), 'Assessment does not reproduce from selected summaries'
    assert len(recorded['trial_validity']) == 20 and all(t['valid'] for t in recorded['trial_validity'])
    # Independently reconstruct timing and failed/late-request outcomes from raw
    # summary fields, rather than copying the assessor's per-site measurements.
    metrics = {}
    for raw in reports:
        cases = {(c['condition'], c['seed']): c for c in raw['design']['cases']}
        for t in raw['trials']:
            if t['policy'] != 'bounded_on':
                continue
            mode = t['configuration']['admission_mode']
            key = (t['condition'], t['seed'], mode)
            assert key not in metrics
            reads = t['live_reads']
            failures = sum(r.get('status_code') != 200 or r.get('latency_ms', math.inf) > t['configuration']['live_slo_ms'] for r in reads)
            values = {('global', 'cloud_drain_upper_seconds'): t['cloud_drain_interval_seconds'][1],
                      ('global', 'client_live_end_to_end_violation_rate'): failures / len(reads)}
            workload = cases[t['condition'], t['seed']]['workload']
            for site in ('Hospital_A', 'Hospital_B'):
                waits = [max(t['completion_intervals'][eid][1] for eid in w['required_event_ids']) - t['reconnection_observations'][site]['interval_seconds'][0]
                         for w in workload['workflows'] if w['site_id'] == site and not w['urgent']]
                values[site, 'routine_post_reconnect_p95_upper_seconds'] = sorted(waits)[math.ceil(.95 * len(waits)) - 1]
            metrics[key] = values
    pairs, summaries = [], []
    for c in recorded['comparisons']:
        if c['comparison'] != 'admission' or c['metric'].startswith('urgent_'):
            continue
        effects = []
        for seed in range(3104, 3109):
            a = metrics[c['condition'], seed, 'aimd'][c['site'], c['metric']]
            b = metrics[c['condition'], seed, 'constant'][c['site'], c['metric']]
            effect = 100 * (a-b) if c['metric'].endswith('violation_rate') else 100 * (a/b-1)
            effects.append(effect)
            pairs.append(dict(condition=c['condition'], seed=seed, site=c['site'], metric=c['metric'],
                              constant=b, aimd=a, effect=effect, units=c['units']))
        assert math.isclose(statistics.fmean(effects), c['mean'], abs_tol=1e-10)
        assert c['uncertainty']['n'] == 5 and c['excluded_pairs'] == 0
        summaries.append(dict(condition=c['condition'], site=c['site'], metric=c['metric'], units=c['units'],
                              mean=c['mean'], ci_low=c['uncertainty']['bootstrap_95pct'][0],
                              ci_high=c['uncertainty']['bootstrap_95pct'][1], p=c['uncertainty']['exact_sign_flip_p'],
                              target_max=c['target_max'], status=c['status']))
    return recorded, pairs, summaries, paths + [assessment_path, affected]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    assessment, pairs, summaries, sources = verify()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {ext: args.output_dir / (PREFIX + ext) for ext in ('.png', '-pairs.csv', '-summary.csv', '-provenance.json', '-caption.md')}
    if any(p.exists() for p in outputs.values()):
        raise FileExistsError('Choose new output paths; existing evidence is preserved')
    for suffix, rows in (('-pairs.csv', pairs), ('-summary.csv', summaries)):
        with outputs[suffix].open('x', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.7), constrained_layout=True)
    specs = [('cloud_drain_upper_seconds', 'Original backlog drain', 'Paired change (%)', 20),
             ('routine_post_reconnect_p95_upper_seconds', 'Routine workflow p95', 'Paired change (%)', 20),
             ('client_live_end_to_end_violation_rate', 'End-to-end live SLO violations', 'Paired difference (percentage points)', 1)]
    for ax, (metric, title, ylabel, target) in zip(axes, specs):
        groups = [r for r in summaries if r['metric'] == metric]
        for i, group in enumerate(groups):
            values = [r['effect'] for r in pairs if all(r[k] == group[k] for k in ('condition', 'site', 'metric'))]
            ax.scatter([i + (j-2)*.035 for j in range(5)], values, s=22, alpha=.65, color='#386a99', label='Seed effects' if i == 0 else None)
            ax.errorbar(i, group['mean'], yerr=[[group['mean']-group['ci_low']], [group['ci_high']-group['mean']]], fmt='D', capsize=4, color='#142e48', label='Mean; 95% paired bootstrap' if i == 0 else None)
        labels = [('30%' if r['condition'].endswith('30') else '70%') + (' / '+r['site'][-1] if r['site'] != 'global' else '') for r in groups]
        ax.set_xticks(range(len(groups)), labels)
        ax.axhline(target, linestyle='--', color='#ae4439', label=f'Target maximum: {target:g}')
        ax.axhline(0, color='gray', linewidth=.6)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_xlabel('Read-load reference' + (' / hospital' if len(groups) == 4 else ''))
        ax.grid(axis='y', alpha=.2)
        ax.legend(fontsize=7, loc='best')
    fig.suptitle('AIMD minus constant-rate: reduced read-only study (5 paired seeds per load)')
    fig.savefig(outputs['.png'], dpi=180)
    plt.close(fig)
    provenance = {'sources_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                  'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'generator_validity_protocol': assessment['generator_validity_protocol'], 'verified_trials': 20}
    outputs['-provenance.json'].write_text(json.dumps(provenance, indent=2)+'\n', encoding='utf-8')
    outputs['-caption.md'].write_text('''Paired AIMD-minus-constant effects at 36/84 read RPS (30%/70% of the provisional
120 RPS read-only reference), balanced 88-event maximum-depth3 backlogs, seeds
3104-3108. Dots are seed effects; diamonds and bars are the mean and 95% paired
percentile-bootstrap interval. Percent changes are averaged per-seed ratios,
not ratios of pooled means; SLO differences are percentage points. Dashed lines
are unchanged guardrail maxima. Lower is better. AIMD drained more slowly and
routine p95 regressed, while end-to-end SLO violations decreased. All displayed
exact two-sided sign-flip p-values are .0625: no p<.05 claim. Observer intervals
are separate and are retained in the assessment, not these bootstrap bars.

Primary generator validity requires 99.9% issuance; stricter zero-skip/99.99%
rules retain five pairs at 30% but only three at 70%. Thus 70% acceptance decisions
depend on the amended primary protocol. One AIMD arm was reused and one invalid
constant arm was replaced after a stop-boundary issuance fix; the original is
retained and excluded explicitly. No RR urgent-improvement or full D2/D3 claim.
Underlying pairs, summary statistics and hashed source paths accompany this PNG.
Reproduce into new paths with:

`python -m docs.evidence.plot_h1_h2_submission_20261101 --output-dir NEW_DIRECTORY`
''', encoding='utf-8')
    print('Verified 20 valid trials, five paired seeds per load, and all plotted effects; wrote '+str(outputs['.png']))


if __name__ == '__main__':
    main()
