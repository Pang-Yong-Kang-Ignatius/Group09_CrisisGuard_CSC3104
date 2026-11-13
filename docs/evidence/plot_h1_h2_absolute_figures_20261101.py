"""Reproduce two measured constant/AIMD comparisons; never overwrite artifacts."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics

from docs.evidence.plot_h1_h2_submission_20261101 import verify, ROOT

SPECS = (
    ('drain', 'cloud_drain_upper_seconds', 'Cloud backlog drain time', 'Drain time (seconds)', 'Change (%)', 1, 20),
    ('slo', 'client_live_end_to_end_violation_rate', 'End-to-end live SLO violation rate', 'Violation rate (%)', 'Difference (percentage points)', 100, 1),
)


def write_csv(path, rows):
    with path.open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    assessment, pairs, summaries, sources = verify()
    prefixes = {kind: f'cp8-h1-h2-{kind}-constant-vs-aimd-20261101' for kind, *_ in SPECS}
    extensions = ('.png', '.pdf', '-data.csv', '-summary.csv', '-caption.md')
    paths = [args.output_dir / (prefix+ext) for prefix in prefixes.values() for ext in extensions]
    provenance_path = args.output_dir / 'cp8-h1-h2-comparisons-20261101-provenance.json'
    if any(p.exists() for p in paths + [provenance_path]):
        raise FileExistsError('Choose a new output directory: existing evidence is never overwritten')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'pdf.fonttype': 42, 'ps.fonttype': 42})
    colors = ('#0072B2', '#D55E00', '#009E73', '#CC79A7', '#6D6518')
    measurements = {(r['condition'], r['seed'], r['admission_mode']): r
                    for r in assessment['per_site_measurements'] if r['site'] == 'Hospital_A'}
    for kind, metric, title, ylabel, effect_label, scale, target in SPECS:
        data = [r for r in pairs if r['metric'] == metric]
        groups = [r for r in summaries if r['metric'] == metric]
        exported, summary_rows = [], []
        fig, axes = plt.subplots(1, 3, figsize=(11, 5), gridspec_kw={'width_ratios': [1, 1, 1.35]})
        fig.subplots_adjust(left=.075, right=.98, top=.77, bottom=.25, wspace=.5)
        maximum = max(r[arm]*scale for r in data for arm in ('constant', 'aimd'))
        for load_index, percent in enumerate((30, 70)):
            selected = sorted((r for r in data if r['condition'].endswith(str(percent))), key=lambda r: r['seed'])
            assert len(selected) == 5 and {r['seed'] for r in selected} == set(range(3104, 3109))
            ax = axes[load_index]
            for j, row in enumerate(selected):
                ax.plot([0, 1], [row['constant']*scale, row['aimd']*scale], 'o-', color=colors[j], linewidth=1.1, markersize=5, alpha=.85)
                constant = measurements[row['condition'], row['seed'], 'constant']
                aimd = measurements[row['condition'], row['seed'], 'aimd']
                entry = dict(condition=row['condition'], read_load_percent=percent, read_rps=percent*1.2,
                             seed=row['seed'], constant_value=row['constant']*scale, aimd_value=row['aimd']*scale,
                             measurement_unit='seconds' if kind == 'drain' else 'percent',
                             paired_effect=row['effect'], paired_effect_unit=row['units'])
                if kind == 'drain':
                    entry.update(constant_observer_lower_seconds=constant['cloud_drain_lower_seconds'],
                                 aimd_observer_lower_seconds=aimd['cloud_drain_lower_seconds'])
                else:
                    entry.update(constant_attempts=constant['live_attempted_requests'],
                                 constant_violations=constant['client_live_end_to_end_violation_count'],
                                 aimd_attempts=aimd['live_attempted_requests'],
                                 aimd_violations=aimd['client_live_end_to_end_violation_count'])
                for mode, m in (('constant', constant), ('aimd', aimd)):
                    entry[mode+'_cap_skips'] = m['generator_accuracy']['cap_skips']
                    entry[mode+'_lag_skips'] = m['generator_accuracy']['lag_skips']
                exported.append(entry)
            means = [statistics.fmean(r[mode]*scale for r in selected) for mode in ('constant', 'aimd')]
            ax.scatter([0, 1], means, marker='D', color='#152938', s=40, zorder=5)
            ax.set_xticks([0, 1], ['Constant-rate', 'AIMD'])
            ax.set_xlim(-.25, 1.25)
            ax.set_ylim(0, maximum*1.15)
            ax.set_title(f'{percent}% load ({percent*1.2:g} RPS)\n5 paired seeds', fontsize=11)
            if load_index == 0:
                ax.set_ylabel(ylabel)
            ax.grid(axis='y', alpha=.2)
            group = next(r for r in groups if r['condition'].endswith(str(percent)))
            ci = [group['ci_low'], group['ci_high']]
            axes[2].errorbar(load_index, group['mean'], yerr=[[group['mean']-ci[0]], [ci[1]-group['mean']]],
                             fmt='D', color='#152938', capsize=5, markersize=6, zorder=5)
            axes[2].scatter([load_index+(j-2)*.035 for j in range(5)], [r['effect'] for r in selected],
                            color=colors, s=23, alpha=.85, zorder=3)
            comparison = next(c for c in assessment['comparisons'] if c['comparison'] == 'admission'
                              and c['condition'] == group['condition'] and c['metric'] == metric)
            summary_rows.append(dict(condition=group['condition'], n_pairs=5,
                                     constant_mean=means[0], aimd_mean=means[1], measurement_unit=entry['measurement_unit'],
                                     mean_paired_effect=group['mean'], effect_unit=group['units'],
                                     paired_bootstrap_95_low=ci[0], paired_bootstrap_95_high=ci[1],
                                     exact_sign_flip_p=group['p'], target_max=target, status=group['status'],
                                     strict_zero_pairs=comparison['generator_accuracy_sensitivity'][0]['n'],
                                     issuance_9999_pairs=comparison['generator_accuracy_sensitivity'][1]['n']))
        axes[2].axhline(0, color='gray', linewidth=.7)
        axes[2].axhline(target, color='#A33B31', linestyle='--', linewidth=1.2)
        axes[2].set_xticks([0, 1], ['30% load', '70% load'])
        axes[2].set_ylabel(effect_label)
        axes[2].set_title('Paired AIMD minus constant\nMean and 95% bootstrap interval', fontsize=11)
        axes[2].grid(axis='y', alpha=.2)
        axes[2].set_xlim(-.45, 1.45)
        fig.suptitle(title+': constant-rate vs AIMD', y=.98, fontsize=15)
        fig.text(.5, .87, 'Balanced 88-event backlog; 5 paired seeds per load. Lower values are better.', ha='center', fontsize=10)
        handles = [Line2D([0], [0], color=color, marker='o', label=str(seed)) for color, seed in zip(colors, range(3104, 3109))]
        handles += [Line2D([0], [0], color='#152938', marker='D', linestyle='None', label='Mean'),
                    Line2D([0], [0], color='#A33B31', linestyle='--', label=f'Effect guardrail: +{target}'+('%' if kind == 'drain' else ' pp'))]
        fig.legend(handles=handles, loc='lower center', bbox_to_anchor=(.5, .09), ncol=7, fontsize=8, title='Paired seed / summary', title_fontsize=9, frameon=False)
        fig.text(.5, .025, '36 / 84 RPS = 30% / 70% of a provisional 120 RPS read-only reference; not maximum mixed-workload capacity.',
                 ha='center', fontsize=8)
        prefix = prefixes[kind]
        fig.savefig(args.output_dir / (prefix+'.png'), dpi=220)
        fig.savefig(args.output_dir / (prefix+'.pdf'), metadata={'Title': title, 'Author': 'CrisisGuard evaluation'})
        plt.close(fig)
        write_csv(args.output_dir / (prefix+'-data.csv'), exported)
        write_csv(args.output_dir / (prefix+'-summary.csv'), summary_rows)
        interpretation = ('AIMD increased mean paired drain time by 278.52% and 276.34%; the +20% recovery guardrail failed at both loads.'
                          if kind == 'drain' else
                          'AIMD reduced the mean paired violation fraction by 2.95 and 3.01 percentage points; the <=+1 pp degradation guardrail was met in this reduced study.')
        definition = ('Cloud drain is the upper endpoint of the monotonic original-backlog completion observation interval, in seconds. Lower observer endpoints accompany the CSV; they are not bootstrap limits.'
                      if kind == 'drain' else
                      'End-to-end violations are non-200 responses OR transport failures OR client latency >250 ms, counted once per issued attempt. Rates are percentages; paired effects are percentage-point differences. Generator misses remain separate, not successes or Cloud failures. These are per-trial fractions, not a request-pooled estimate.')
        caption = f'''# {title}: constant-rate versus AIMD

Left and center: five actual paired seed measurements (3104-3108) per load;
connected colors identify the same seed and black diamonds indicate arithmetic
means. Right: individual paired effects and their mean with a 95% paired
percentile-bootstrap interval over five seeds. No marginal confidence interval
is implied by the mean diamonds on the measurement panels. The dashed line is
the unchanged effect guardrail. {definition}

{interpretation} Effects are averages of per-seed ratios/differences, not ratios
of pooled means. Exact two-sided sign-flip p=.0625 for both comparisons; no p<.05
claim. The 30%/70% loads are 36/84 RPS of a provisional 120 RPS read-only reference,
not maximum sustainable mixed-workload capacity. The full D2 matrix and urgent
improvement against RR are not established.

All 20 selected trials are valid under primary 99.9% issuance. Strict-zero and
99.99% sensitivities retain five pairs at 30% but only three at 70%; primary 70%
decisions depend on the disclosed protocol amendment. One corrected AIMD arm was
reused across sessions; one invalid constant arm was replaced after a genuine
stop-boundary issuance fix. Original invalid evidence remains preserved. The
underlying study includes only one balanced maximum-depth3 88-event workload.

Source: `evaluation/results/replacement-3108-constant-20261101-160903/submission-assessment.json`.
The verifier reconstructs the assessment from selected original summaries,
independently checks raw-summary outcome counts/timing, and verifies source hashes.
Source hashes and script hashes are recorded in the accompanying provenance JSON.
No experiments are rerun. Reproduce to new paths with:

`python -m docs.evidence.plot_h1_h2_absolute_figures_20261101 --output-dir NEW_DIRECTORY`
'''
        (args.output_dir / (prefix+'-caption.md')).write_text(caption, encoding='utf-8')
    provenance = {'assessment': 'evaluation/results/replacement-3108-constant-20261101-160903/submission-assessment.json',
                  'verified_trials': 20, 'paired_seeds_per_load': 5,
                  'sources_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                  'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'verifier_sha256': hashlib.sha256((ROOT/'docs/evidence/plot_h1_h2_submission_20261101.py').read_bytes()).hexdigest(),
                  'generator_validity_protocol': assessment['generator_validity_protocol']}
    provenance_path.write_text(json.dumps(provenance, indent=2)+'\n', encoding='utf-8')
    print('Verified 20 valid trials; exported two PNG/PDF figures, paired data, summaries, captions and source hashes.')


if __name__ == '__main__':
    main()
