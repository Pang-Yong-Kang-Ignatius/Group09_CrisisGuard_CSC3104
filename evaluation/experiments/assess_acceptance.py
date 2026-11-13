"""Read-only D3 assessment; never upgrades exploratory evidence to acceptance."""
import argparse
import hashlib
import math
import json
from pathlib import Path
import statistics

from evaluation.experiments.analyze_recovery_matrix import (
    paired, trial_metrics, GENERATOR_SKIP_FRACTION_MAX, GENERATOR_SENSITIVITY,
)


def assess(reports, max_generator_skip_fraction=GENERATOR_SKIP_FRACTION_MAX, policies=None):
    if not math.isfinite(max_generator_skip_fraction) or not 0 <= max_generator_skip_fraction <= GENERATOR_SKIP_FRACTION_MAX:
        raise ValueError("Generator tolerance must be in [0, .001]; D3 targets are unchanged")
    indexed = {}
    expected_seeds = {}
    measurements = []
    for raw in reports:
        cases = {(c['condition'], c['seed']): c for c in raw['design']['cases']}
        for c in cases.values():
            expected_seeds.setdefault(c['condition'], set()).add(c['seed'])
        for trial in raw['trials']:
            if policies is not None and trial['policy'] not in policies:
                continue
            config = trial.get('configuration', {})
            case = cases[trial['condition'], trial['seed']]
            # Pair exact graph, load, SLO, admission ceiling and measurement settings.
            signature = json.dumps([case['workload'], {k: config.get(k) for k in (
                'live_read_rps', 'live_read_target', 'live_slo_ms', 'live_concurrency_cap',
                'poll_seconds', 'drain_timeout_seconds', 'replay_activation',
                'metaguard_retry_policy', 'scheduler')},
                {k:v for k,v in config.get('controller', {}).items() if k not in (
                    'CONTROLLER_ADMISSION_POLICY', 'CONTROLLER_ADMISSION_NORMAL_RPS',
                    'CONTROLLER_ADMISSION_CAUTION_RPS', 'CONTROLLER_ADMISSION_RECOVERY_RPS')},
                (float(config['controller']['CONTROLLER_ADMISSION_NORMAL_RPS']) if config.get('controller', {}).get('CONTROLLER_ADMISSION_NORMAL_RPS') is not None else None),
                {k:v for k,v in raw['design'].get('source_sha256', {}).items()
                 if k.startswith(('edge/', 'cloud/')) or k.endswith('.yaml')}], sort_keys=True)
            key = (trial['condition'], trial['seed'], hashlib.sha256(signature.encode()).hexdigest(),
                   trial['policy'], config.get('admission_mode'))
            if key in indexed:
                raise ValueError('Duplicate trial identity; refusing ambiguous seed pairing')
            rows = trial_metrics(trial, case['workload'])
            indexed[key] = (trial, rows, raw)
            measurements.extend(dict(row, admission_mode=config.get('admission_mode')) for row in rows)
    comparisons = []
    specs = [('urgent_post_reconnect_p95_upper_seconds', -10),
             ('routine_post_reconnect_p95_upper_seconds', 20),
             ('cloud_drain_upper_seconds', 20), ('client_live_end_to_end_violation_rate', 1)]
    for condition in sorted({k[0] for k in indexed}):
        for policy, mode in sorted({(k[3], k[4]) for k in indexed if k[0] == condition}, key=str):
            for comparison in ('scheduler', 'admission'):
                control_policy, control_mode = ('rr_on', mode) if comparison == 'scheduler' else (policy, 'constant')
                if (policy, mode) == (control_policy, control_mode):
                    continue
                for site_index, site in enumerate(('Hospital_A', 'Hospital_B')):
                    for metric, target in specs:
                        effects, observation, per_seed = [], [], []
                        sensitivity = {limit: [] for limit in sorted(set(GENERATOR_SENSITIVITY + (max_generator_skip_fraction,)))}
                        present_seeds = {k[1] for k in indexed if (k[0], k[3], k[4]) == (condition, policy, mode)}
                        excluded = len(expected_seeds[condition] - present_seeds)
                        if metric in ('cloud_drain_upper_seconds', 'client_live_end_to_end_violation_rate') and site_index:
                            continue
                        for key, (trial, rows, raw) in indexed.items():
                            if (key[0], key[3], key[4]) != (condition, policy, mode):
                                continue
                            control = indexed.get((*key[:3], control_policy, control_mode))
                            if not control:
                                excluded += 1
                                continue
                            ct, cr, craw = control
                            a, b = rows[site_index], cr[site_index]
                            av, bv = a.get(metric), b.get(metric)
                            valid = all(t['result'] == 'passed' for t in (trial, ct))
                            valid &= all(row['unfinished_events'] == 0 for row in rows + cr)
                            valid &= all(r.get('source_unchanged_during_run', True) and t.get('cleanup', 'passed') == 'passed' for t, r in ((trial, raw), (ct, craw)))
                            valid &= all(r.get('preservation') and all(r['preservation'].values()) for r in (raw, craw))
                            valid &= all(
                                r['unfinished_events'] == 0
                                for r in (a, b)
                            )

                            # Request failures are measured outcomes, not timing exclusions.
                            if not valid or av is None or bv is None or (metric != 'client_live_end_to_end_violation_rate' and bv <= 0):
                                excluded += 1
                                continue
                            effect = 100*(av-bv) if metric == 'client_live_end_to_end_violation_rate' else 100*(av/bv-1)
                            accuracy = [r['generator_accuracy'] for r in (a, b)]
                            for limit in sensitivity:
                                if all(g['ledger_reconciled'] and g['skip_fraction'] <= limit for g in accuracy):
                                    sensitivity[limit].append(effect)
                            if not all(g['ledger_reconciled'] and g['skip_fraction'] <= max_generator_skip_fraction for g in accuracy):
                                excluded += 1
                                continue
                            effects.append(effect)
                            per_seed.append(dict(seed=key[1], candidate=av, control=bv, effect=effect))
                            if metric != 'client_live_end_to_end_violation_rate':
                                lower = metric.replace('_upper_', '_lower_')
                                if b.get(lower) and a.get(lower) is not None:
                                    observation.append([100*(a[lower]/bv-1), 100*(av/b[lower]-1)])
                        uncertainty = paired(effects)
                        uncertainty['mean_effect'] = uncertainty.pop('mean_policy_minus_rr_on')
                        mean = statistics.fmean(effects) if effects else None
                        applicable = not metric.startswith('urgent_') or (condition.startswith('ratio10-1_depth3_a_only_') and policy == 'bounded_on' and comparison == 'scheduler' and mode == 'aimd' and site == 'Hospital_A')
                        total_pairs = len(expected_seeds[condition])
                        sensitivity_results = [dict(max_skip_fraction=limit, n=len(values),
                            excluded_pairs=total_pairs-len(values),
                            mean=statistics.fmean(values) if values else None,
                            status=('achieved' if statistics.fmean(values) <= target else 'failed')
                                if applicable and len(values) >= 5 and len(values) == total_pairs else 'not established')
                            for limit, values in sensitivity.items()]
                        comparisons.append(dict(per_seed=per_seed, generator_accuracy_sensitivity=sensitivity_results, acceptance_applicable=applicable, condition=condition, policy=policy, admission=mode,
                            comparison=comparison, site='global' if metric in ('cloud_drain_upper_seconds', 'client_live_end_to_end_violation_rate') else site, metric=metric, target_max=target,
                            units='percentage points' if metric == 'client_live_end_to_end_violation_rate' else 'percent',
                            mean=mean, uncertainty=uncertainty, excluded_pairs=excluded,
                            observer_relative_interval=[statistics.fmean(v[i] for v in observation) for i in (0, 1)] if observation else None,
                            status=('achieved' if mean <= target else 'failed') if applicable and len(effects) >= 5 and excluded == 0 else 'not established'))
    trial_validity = []
    for key, (trial, rows, raw) in indexed.items():
        g = rows[0]['generator_accuracy']
        valid = (trial['result'] == 'passed' and trial.get('cleanup', 'passed') == 'passed'
                 and raw.get('source_unchanged_during_run', True)
                 and bool(raw.get('preservation')) and all(raw['preservation'].values())
                 and all(row['unfinished_events'] == 0 for row in rows)
                 and g['ledger_reconciled'] and g['skip_fraction'] <= max_generator_skip_fraction)
        trial_validity.append(dict(condition=key[0], seed=key[1], policy=key[3], admission=key[4],
                                   valid=bool(valid), generator_accuracy=g))
    return {'trial_validity': trial_validity, 'generator_validity_protocol': {'max_skip_fraction': max_generator_skip_fraction,
        'minimum_issuance_ratio': 1-max_generator_skip_fraction, 'sensitivity_limits': list(GENERATOR_SENSITIVITY),
        'amendment': 'Changed after initial blocks; existing evidence is retrospective under this rule. D2 exact arrival matching is approximated, not proven by aggregate accuracy. Report sensitivity and start-lag diagnostics.'}, 'comparisons': comparisons, 'per_site_measurements': measurements,
        'slo_definition': 'End-to-end: non-200 or transport failure or latency > configured threshold, divided by all attempted requests; success-only latency reported separately. D3 does not specify failure accounting; this conservative operational definition must be frozen before execution. Generator skips remain separate; reconciled trials must satisfy the explicit generator accuracy budget. Skips never enter the Cloud SLO denominator.',
        'correctness': {'status': 'not established', 'reason': 'Passing functional trials check event sets, dependency and grant order; duplicate domain effects and complete grant-bound eligibility coverage require separate witnesses.'},
        'fairness': {'status': 'not established', 'reason': 'Per-site metrics report grant/completion/cost shares, retries, sampled bounds, CPU and memory. Finite-prefix fairness is not a sustained fairness guarantee.'},
        'full_D2_D3': 'not established: requires reviewed sustainable representative capacity, preregistered skewed dependency case, complete matrix and correctness witnesses',
        'uncertainty_note': 'Seed is the unit; bootstrap and exact sign-flip uncertainty are separate from observer intervals. Targets use point estimates, not a significance claim.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('summaries', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-generator-skip-fraction', type=float, default=GENERATOR_SKIP_FRACTION_MAX, help='Primary .001; use 0 for legacy strict analysis (cannot exceed .001)')
    parser.add_argument('--policy', action='append', help='Restrict assessment to selected policies, e.g. bounded_on for the reduced admission study')
    args = parser.parse_args()
    result = assess([json.loads(p.read_text()) for p in args.summaries], args.max_generator_skip_fraction, args.policy)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)


if __name__ == '__main__':
    main()
