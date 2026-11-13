"""Recalculate observer-based trial metrics and paired seed uncertainty."""
import argparse
import csv
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import re
import statistics


def p95(values):
    return sorted(values)[math.ceil(.95*len(values))-1] if values else None


# Protocol amendment: engineering load-accuracy budget, separate from D3 targets.
GENERATOR_SKIP_FRACTION_MAX = .001
GENERATOR_SENSITIVITY = (0., .0001, GENERATOR_SKIP_FRACTION_MAX)


def generator_accuracy(trial):
    """Reconcile scheduled opportunities with issued attempts and genuine misses."""
    rate = trial.get('configuration', {}).get('live_read_rps')
    stop = trial.get('live_generator_stop_seconds')
    target = trial.get('live_target_opportunities')
    issued = len(trial['live_reads'])
    cap = trial.get('live_generator_cap_skips', 0)
    lag = trial.get('live_generator_lag_skips', 0)
    counts_valid = all(type(v) is int and v >= 0 for v in (target, cap, lag))
    clock_valid = (type(rate) in (int, float) and math.isfinite(rate) and rate > 0
                   and type(stop) in (int, float) and math.isfinite(stop) and stop > 0)
    reconciled = bool(counts_valid and clock_valid and target == math.ceil(stop*rate)
                      and issued + cap + lag == target and target > 0)
    indices = [r.get('offered_index') for r in trial['live_reads']]
    if any(index is not None for index in indices):
        reconciled &= (all(type(index) is int and 1 <= index <= target for index in indices)
                       and len(set(indices)) == issued) if counts_valid else False
    return {'scheduled_opportunities': target, 'issued_attempts': issued,
            'cap_skips': cap, 'lag_skips': lag, 'ledger_reconciled': reconciled,
            'skip_fraction': (cap+lag)/target if reconciled else None,
            'issuance_ratio': issued/target if reconciled else None,
            'issued_rps': issued/stop if clock_valid else None,
            'scheduled_rps': target/stop if clock_valid and counts_valid else None}


def memory_bytes(value):
    number, unit = re.fullmatch(r'([0-9.]+)([A-Za-z]+)', value.split('/')[0].strip()).groups()
    return float(number)*{'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3}[unit]


def ticket_witnesses(trial):
    policy = trial.get('configuration', {}).get('scheduling')
    if policy not in ('bounded', 'fifo'):
        return []
    limit = trial['configuration'].get('bypass_limit', 1) if policy == 'bounded' else 0
    grants = {g['grant_id']: g for g in trial.get('grants', [])}
    allocations = [{**a, **grants[a['grant_id']]} for a in trial.get('audit', [])
                   if a['action'] == 'allocate' and a['grant_id'] in grants]
    seen, result = set(), []
    for sample in trial['samples']:
        tickets = sample.get('tickets', [])
        for ticket in tickets:
            key = (ticket['hospital_id'], ticket['queue_position'])
            if key in seen:
                continue
            seen.add(key)
            candidates = [a for a in allocations if (a['hospital_id'], a['original_queue_position']) == key
                          and a['id'] > ticket['observed_audit_id']]
            if not candidates:
                continue
            selected = min(candidates, key=lambda a: a['id'])
            others = sum(ticket['observed_audit_id'] < a['id'] < selected['id'] for a in allocations)
            bound = sum(t['queue_position'] < ticket['queue_position'] for t in tickets)+max(0, limit-ticket['bypass_count'])
            result.append({'site': key[0], 'queue_position': key[1], 'other_allocations': others,
                           'remaining_predecessor_plus_bypass_bound': bound, 'violated': others > bound})
    return result


def trial_metrics(trial, workload):
    intervals = trial['completion_intervals']
    rows = []
    end = trial.get('cloud_drain_interval_seconds', [None, None])[1]
    # Unequal backlogs are normalized only for this finite prefix metric.
    halfway = next((s for s in trial['samples'] if s['cloud_completed'] >= len(workload['events'])/2), None)
    resources = [s['containers'] for s in trial.get('resource_samples', []) if 'containers' in s]
    witnesses = ticket_witnesses(trial)
    reference_costs = trial.get('configuration', {}).get('reference_event_cost_units', {})
    estimated_costs = trial.get('configuration', {}).get('estimated_event_cost_units', {})
    prefix_fractions = [sum(intervals[e['event_id']][1] <= halfway['elapsed_seconds'] for e in workload['events']
                            if e['site_id'] == site and e['event_id'] in intervals)/sum(e['site_id'] == site for e in workload['events'])
                        for site in ('Hospital_A', 'Hospital_B')] if halfway else []
    prefix_costs = {site: sum(reference_costs.get(e['event_id'], 1) for e in workload['events']
                              if e['site_id'] == site and e['event_id'] in intervals and intervals[e['event_id']][1] <= halfway['elapsed_seconds'])
                    for site in ('Hospital_A', 'Hospital_B')} if halfway else {}
    original_costs = {site: sum(reference_costs.get(e['event_id'], 1) for e in workload['events'] if e['site_id'] == site)
                      for site in ('Hospital_A', 'Hospital_B')}
    for site in ('Hospital_A', 'Hospital_B'):
        original = [e for e in workload['events'] if e['site_id'] == site]
        ids = {e['event_id'] for e in original}
        completed = ids & intervals.keys()
        reconnect = trial.get('reconnection_observations', {}).get(site, {}).get('interval_seconds')
        new_generator = 'live_generator_lag_skips' in trial
        recorded_skips = trial.get('live_generator_cap_skips', 0)+trial.get('live_generator_lag_skips', 0)
        rate = trial.get('configuration', {}).get('live_read_rps', 0)
        slo_ms = trial.get('configuration', {}).get('live_slo_ms')
        successful_reads = [r for r in trial['live_reads'] if r.get('status_code') == 200]

        # Latency SLO violations among successful HTTP 200 requests.
        slo_violations = (
            sum(r.get('latency_ms', float('inf')) > slo_ms for r in successful_reads)
            if slo_ms is not None else None
        )

        # End-to-end violations include HTTP errors, connection failures,
        # and requests exceeding the latency threshold.
        end_to_end_violations = (
            sum(
                r.get('status_code') != 200
                or r.get('latency_ms', float('inf')) > slo_ms
                for r in trial['live_reads']
            )
            if slo_ms is not None else None
        )
        accuracy = generator_accuracy(trial)
        row = {'generator_accuracy': accuracy, 'condition': trial['condition'], 'seed': trial['seed'], 'policy': trial['policy'], 'site': site,
               'result': trial['result'], 'original_events': len(ids), 'completed_events': len(completed),
               'unfinished_events': len(ids-completed), 'startup_seconds': trial.get('startup_seconds'),
               'cloud_drain_lower_seconds': trial.get('cloud_drain_interval_seconds', [None, None])[0],
               'cloud_drain_upper_seconds': end, 'unique_event_throughput_rps': len(completed)/end if end else None,
               'site_drain_upper_seconds': max((intervals[eid][1] for eid in completed), default=None) if ids == completed else None,
               'reconnection_observed_lower_seconds': reconnect[0] if reconnect else None,
               'reconnection_observed_upper_seconds': reconnect[1] if reconnect else None,
               'site_drain_post_reconnect_lower_seconds': max(0, max(intervals[eid][0] for eid in completed)-reconnect[1]) if reconnect and ids == completed else None,
               'site_drain_post_reconnect_upper_seconds': max(0, max(intervals[eid][1] for eid in completed)-reconnect[0]) if reconnect and ids == completed else None,
               'live_offered': len(trial['live_reads'])+recorded_skips,
               'live_offered_basis': 'explicit stop-clock opportunities' if new_generator else 'legacy recorded opportunities; exact stop not retained',
               'live_target_offer_lower_bound': math.ceil(end*rate) if end and rate else None,
               'live_target_offer_upper_bound': math.ceil(trial['local_ack_drain_seconds']*rate) if trial.get('local_ack_drain_seconds') and rate else None,
               'live_completed_requests': sum('status_code' in r for r in trial['live_reads']),
               'live_successes': len(successful_reads),
               'live_attempted_requests': len(trial['live_reads']),
               'live_http_non_200_responses': sum('status_code' in r and r['status_code'] != 200 for r in trial['live_reads']),
               'live_transport_failures': sum('status_code' not in r for r in trial['live_reads']),
               'live_non_200_or_transport_failures': sum(r.get('status_code') != 200 for r in trial['live_reads']),
               'live_generator_recorded_skips': recorded_skips,
               'live_generator_cap_skips': trial.get('live_generator_cap_skips', 0) if new_generator or recorded_skips == 0 else None,
               'live_generator_lag_skips': trial.get('live_generator_lag_skips', 0) if new_generator or recorded_skips == 0 else None,
               'live_generator_unattributed_legacy_skips': recorded_skips if not new_generator else 0,
               'client_live_read_p95_ms': p95([r['latency_ms'] for r in trial['live_reads'] if 'latency_ms' in r]),
               'client_live_success_p95_ms': p95([r['latency_ms'] for r in successful_reads if 'latency_ms' in r]),
               'client_live_start_lag_p95_ms': p95([r['start_lag_ms'] for r in trial['live_reads']
                                                   if 'start_lag_ms' in r]),
               'client_live_slo_threshold_ms': slo_ms,
               'client_live_slo_violation_count': slo_violations,
                'client_live_slo_violation_rate': (
                    slo_violations / len(successful_reads)
                    if slo_violations is not None and successful_reads
                    else None
                ),

                # Sensitivity only: unknown outcomes for unissued opportunities.
                'scheduled_opportunity_slo_fraction_bounds': (
                    [end_to_end_violations/accuracy['scheduled_opportunities'],
                     (end_to_end_violations+recorded_skips)/accuracy['scheduled_opportunities']]
                    if accuracy['ledger_reconciled'] and end_to_end_violations is not None else None),
                'client_live_end_to_end_violation_count': end_to_end_violations,
                'client_live_end_to_end_violation_rate': (
                    end_to_end_violations / len(trial['live_reads'])
                    if end_to_end_violations is not None and trial['live_reads']
                    else None
                ),

               'grant_count': sum(g['hospital_id'] == site for g in trial.get('grants', [])),
               'completed_grant_count': sum(g['hospital_id'] == site and g['status'] == 'completed' for g in trial.get('grants', [])),
               'superseded_grant_count': sum(g['hospital_id'] == site and g['status'] == 'superseded' for g in trial.get('grants', [])),
               'grant_share': sum(g['hospital_id'] == site for g in trial.get('grants', []))/len(trial['grants']) if trial.get('grants') else None,
               'completion_share': len(completed)/len(intervals) if intervals else None,
               'completed_unit_cost_share': len(completed)/len(intervals) if intervals else None,
               'allocated_unit_cost_share': sum(g['hospital_id'] == site for g in trial.get('grants', []))/len(trial['grants']) if trial.get('grants') else None,
               'completed_reference_wall_cost_share': sum(reference_costs.get(eid, 1) for eid in completed)/sum(reference_costs.get(eid, 1) for eid in intervals) if intervals else None,
               'allocated_reference_wall_cost_share': sum(reference_costs.get(g['event_id'], 1) for g in trial.get('grants', []) if g['hospital_id'] == site)/sum(reference_costs.get(g['event_id'], 1) for g in trial.get('grants', [])) if trial.get('grants') else None,
               'allocated_scheduler_estimate_cost_share': sum(estimated_costs.get(g['event_id'], 1) for g in trial.get('grants', []) if g['hospital_id'] == site)/sum(estimated_costs.get(g['event_id'], 1) for g in trial.get('grants', [])) if trial.get('grants') else None,
               'half_drain_reference_wall_cost_share': prefix_costs[site]/sum(prefix_costs.values()) if prefix_costs and sum(prefix_costs.values()) else None,
               'half_drain_normalized_reference_cost_fraction': prefix_costs[site]/original_costs[site] if prefix_costs else None,
               'sampled_ticket_witnesses': sum(w['site'] == site for w in witnesses),
               'sampled_ticket_bound_violations': sum(w['site'] == site and w['violated'] for w in witnesses),
               'half_drain_normalized_fraction_jain': sum(prefix_fractions)**2/(2*sum(v*v for v in prefix_fractions)) if prefix_fractions and any(prefix_fractions) else None,
               'site_retry_total': sum(r['retry_count'] for r in trial.get('sqlite_recovery', {}).get(site, {}).get('recovery', [])),
               'sampled_project_cpu_percent_max': max((sum(float(r['CPUPerc'].rstrip('%')) for r in s) for s in resources), default=None),
               'sampled_project_memory_mib_max': max((sum(memory_bytes(r['MemUsage']) for r in s)/1024**2 for s in resources), default=None),
               'observed_poll_gap_max_seconds': max((s['elapsed_seconds']-s0['query_started_seconds'] for s0, s in zip(trial['samples'], trial['samples'][1:])), default=None),
               'completion_fraction_at_half_global_drain': sum(intervals[eid][1] <= halfway['elapsed_seconds'] for eid in completed)/len(ids) if halfway else None}
        for urgent, label in ((True, 'urgent'), (False, 'routine')):
            workflows = [w for w in workload['workflows'] if w['site_id'] == site and w['urgent'] == urgent]
            complete = [w for w in workflows if all(eid in intervals for eid in w['required_event_ids'])]
            row[label+'_original_workflows'] = len(workflows)
            row[label+'_completed_workflows'] = len(complete)
            row[label+'_unfinished_workflows'] = len(workflows)-len(complete)
            for bound, index in (('lower', 0), ('upper', 1)):
                values = [max(intervals[eid][index] for eid in w['required_event_ids']) for w in complete]
                # Keep partial-population latency undefined; do not reward dropped slow work.
                row[f'{label}_workflow_p95_{bound}_seconds'] = p95(values) if len(complete) == len(workflows) else None
                row[f'{label}_workflow_mean_{bound}_seconds'] = statistics.fmean(values) if values and len(complete) == len(workflows) else None
                post_reconnect = [max(0, v-reconnect[1-index]) for v in values] if reconnect else []
                row[f'{label}_post_reconnect_p95_{bound}_seconds'] = p95(post_reconnect) if len(complete) == len(workflows) else None
        rows.append(row)
    return rows


def paired(differences):
    if not differences:
        return {'n': 0, 'mean_policy_minus_rr_on': None, 'bootstrap_95pct': None, 'exact_sign_flip_p': None}
    mean = statistics.fmean(differences)
    rng = random.Random(20261030)
    boot = sorted(statistics.fmean(rng.choices(differences, k=len(differences))) for _ in range(10000))
    # A two-sided exact paired test with n=5 cannot attain p < .05; retain it.
    p = sum(abs(statistics.fmean(d*s for d, s in zip(differences, signs))) >= abs(mean)-1e-12
            for signs in itertools.product((-1, 1), repeat=len(differences)))/2**len(differences)
    return {'n': len(differences), 'mean_policy_minus_rr_on': mean,
            'bootstrap_95pct': [boot[249], boot[9749]], 'exact_sign_flip_p': p}


def analyze(raw, output):
    if len({t.get('configuration', {}).get('admission_mode') for t in raw['trials']}) > 1:
        raise ValueError('Mixed admission arms require assess_acceptance; refusing to collapse paired arms')
    output.mkdir(parents=True, exist_ok=False)
    cases = {(c['condition'], c['seed']): c for c in raw['design']['cases']}
    rows = [row for trial in raw['trials'] for row in trial_metrics(trial, cases[(trial['condition'], trial['seed'])]['workload'])]
    with (output/'per_site_metrics.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    comparisons = []
    metric_names = ['urgent_workflow_p95_upper_seconds', 'routine_workflow_p95_upper_seconds', 'cloud_drain_upper_seconds']
    for condition in sorted({r['condition'] for r in rows}):
        for site in ('Hospital_A', 'Hospital_B'):
            group = [r for r in rows if r['condition'] == condition and r['site'] == site]
            for policy in sorted({r['policy'] for r in group}-{'rr_on'}):
                expected_seeds = {c['seed'] for c in cases.values() if c['condition'] == condition
                                  and policy in c['policies'] and 'rr_on' in c['policies']}
                if not expected_seeds:
                    continue
                for metric in metric_names:
                    control = {r['seed']: r for r in group if r['policy'] == 'rr_on'}
                    candidates = [r for r in group if r['policy'] == policy]
                    eligible = [r for r in candidates if r['seed'] in control and r['result'] == control[r['seed']]['result'] == 'passed'
                                and r[metric] is not None and control[r['seed']][metric] is not None]
                    lower_metric = metric.replace('_upper_', '_lower_')
                    differences = [r[metric]-control[r['seed']][metric] for r in eligible]
                    observation_lower = [r[lower_metric]-control[r['seed']][metric] for r in eligible]
                    observation_upper = [r[metric]-control[r['seed']][lower_metric] for r in eligible]
                    common = [r for r in candidates if r['seed'] in control]
                    population = 'urgent' if metric.startswith('urgent') else 'routine' if metric.startswith('routine') else None
                    relative = [100*(r[metric]/control[r['seed']][metric]-1) for r in eligible if control[r['seed']][metric] > 0]
                    comparisons.append({'condition': condition, 'site': site, 'policy': policy, 'metric': metric,
                        'mean_paired_relative_change_pct': statistics.fmean(relative) if relative else None,
                        'relative_effect_pairs': len(relative),
                        'excluded_or_missing_seed_pairs': len(expected_seeds)-len(eligible),
                        'missing_seed_pairs': len(expected_seeds)-len(common),
                        'empty_population_seed_pairs': sum(r[population+'_original_workflows'] == 0 or control[r['seed']][population+'_original_workflows'] == 0 for r in common) if population else 0,
                        'failed_or_unfinished_seed_pairs': sum(r['result'] != 'passed' or control[r['seed']]['result'] != 'passed' or
                            r['unfinished_events'] > 0 or control[r['seed']]['unfinished_events'] > 0 for r in common),
                        'mean_difference_observation_interval': [statistics.fmean(observation_lower), statistics.fmean(observation_upper)] if eligible else None,
                        **paired(differences)})
    summary = {'executed_trials': len(raw['trials']), 'planned_trials': raw['planned_trials'],
        'analyzer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'passed_trials': sum(t['result'] == 'passed' for t in raw['trials']),
        'total_original_events': sum(len(cases[(t['condition'], t['seed'])]['workload']['events']) for t in raw['trials']),
        'total_observed_events': sum(len(t['completion_intervals']) for t in raw['trials']),
        'cleanup_all_passed': all(t.get('cleanup') == 'passed' for t in raw['trials']),
        'sampled_ticket_witnesses': sum(r['sampled_ticket_witnesses'] for r in rows),
        'sampled_ticket_bound_violations': sum(r['sampled_ticket_bound_violations'] for r in rows),
        'preservation': raw['preservation'], 'paired_comparisons': comparisons,
        'acceptance_targets': 'NOT ESTABLISHED: uncalibrated live load and no supported grant-enforced live writes; bounded screen/cost stress is not full D2/D3',
        'raw_backup_directory': raw['raw_backup_directory']}
    (output/'statistics.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    policies = sorted({r['policy'] for r in rows})
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), constrained_layout=True)
    for ax, metric, title in zip(axes, ('cloud_drain_upper_seconds', 'urgent_workflow_p95_upper_seconds'), ('Original backlog drain', 'Urgent workflow p95')):
        for i, policy in enumerate(policies):
            values = [r[metric] for r in rows if r['policy'] == policy and r['site'] == 'Hospital_A' and r[metric] is not None]
            ax.scatter([i]*len(values), values, s=15, alpha=.6)
        ax.set_xticks(range(len(policies)), policies, rotation=45, ha='right')
        ax.set_ylabel('Observer upper-bound elapsed seconds')
        ax.set_title(title+' (A for urgent)')
        ax.grid(axis='y', alpha=.2)
    fig.suptitle('Bounded real replay '+raw['design']['configuration']['mode']+'; individual trial points')
    fig.savefig(output/'recovery_comparison.png', dpi=160)
    plt.close(fig)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    summary = analyze(json.loads(args.raw.read_text(encoding='utf-8')), args.output)
    print(json.dumps({k: v for k, v in summary.items() if k != 'paired_comparisons'}, indent=2))


if __name__ == '__main__':
    main()
