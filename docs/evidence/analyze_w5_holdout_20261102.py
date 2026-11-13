"""Audit W5 originals and reuse the existing acceptance/latency analysis."""
import copy
import argparse
import csv
import hashlib
import json
from pathlib import Path

from evaluation.experiments.assess_acceptance import assess
from evaluation.experiments.analyze_recovery_matrix import generator_accuracy, paired, trial_metrics
from evaluation.experiments.run_recovery_matrix import verify
from evaluation.workloads.generate_workload import capture_request
from docs.evidence.analyze_aimd_deferral_pilot_20261101 import client

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'evaluation/results/w5-holdout-20261102'


def main():
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-name', default='w5-holdout-20261102')
    parser.add_argument('--evidence-directory', type=Path,
                        help='Default: run output directory; use docs/evidence only for initial publication')
    args = parser.parse_args()
    assert Path(args.output_name).name == args.output_name
    OUT = ROOT / 'evaluation/results' / args.output_name
    evidence = args.evidence_directory or OUT
    protocol = json.loads((OUT / 'protocol.json').read_text())
    replacement = None
    replacement_root = ROOT / 'evaluation/results/w5-replacement-60103-20261102'
    if OUT.name == 'w5-holdout-20261102-r3' and (replacement_root / 'run/summary.json').exists():
        rp = json.loads((replacement_root / 'replacement-protocol.json').read_text())
        bad = Path(rp['original_source'])
        control = Path(rp['paired_control'])
        assert hashlib.sha256(bad.read_bytes()).hexdigest() == rp['original_sha256']
        assert hashlib.sha256(control.read_bytes()).hexdigest() == rp['control_sha256']
        bad_report = json.loads(bad.read_text())
        candidate = json.loads((replacement_root / 'run/summary.json').read_text())
        assert candidate['design']['source_sha256'] == bad_report['design']['source_sha256']
        cg = generator_accuracy(candidate['trials'][0])
        use = candidate['result'] == 'passed' and cg['ledger_reconciled'] and cg['skip_fraction'] <= .001
        replacement = dict(protocol=rp, used=use, generator=cg,
                           source=str(replacement_root / 'run/summary.json'),
                           sha256=hashlib.sha256((replacement_root / 'run/summary.json').read_bytes()).hexdigest(),
                           original_invalid_generator=generator_accuracy(bad_report['trials'][0]))
    reports, audit, hashes, fingerprints, pair_graphs, pair_configs = [], [], {}, [], {}, {}
    for job in protocol['jobs']:
        path = OUT / ('run-' + job['name']) / 'summary.json'
        plan_path = OUT / ('plan-' + job['name']) / 'plan.json'
        if replacement and replacement['used'] and job['name'] == '84-60103-aimd':
            path = Path(replacement['source'])
            plan_path = replacement_root / 'plan/plan.json'
        original = json.loads(path.read_text())
        assert original['result'] == 'passed' and original['planned_trials'] == len(original['trials']) == 1
        assert original['source_unchanged_during_run'] and all(original['preservation'].values())
        assert not original['container_state_changes']
        t = original['trials'][0]
        raw = Path(t['raw_path'])
        assert json.loads(raw.read_text()) == t
        assert t['integrity'] == t['cleanup'] == t['result'] == 'passed'
        assert t['seed'] == job['seed'] and t['configuration']['admission_mode'] == job['arm']
        assert t['configuration']['live_read_rps'] == job['load']
        assert t['configuration']['metaguard_retry_policy'] == 'bounded-deferral'
        assert t['configuration']['controller']['CONTROLLER_ADMISSION_HEALTH_POLICY'] == 'pressure-experimental'
        assert t['duplicate_probe_status'] == 409 and len(t['completion_intervals']) == 88
        assert len(t['postgres_events']) == sum(g['status'] == 'completed' for g in t['grants']) == 88
        assert not t['remaining_tickets']
        verify({e['event_id']: capture_request(e) for e in original['design']['cases'][0]['workload']['events']},
               t['postgres_events'], t['grants'], t['audit'], t['remaining_tickets'], t['local'])
        assert all(s['integrity'] == 'ok' and all(not r['quarantined'] and not r['in_flight'] for r in s['recovery'])
                   for s in t['sqlite_recovery'].values())
        assert t['retained_database_volumes']
        for source in (path, raw):
            hashes[str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
        plan = json.loads(plan_path.read_text())
        assert plan['source_sha256'] == original['design']['source_sha256']
        fingerprints.append(original['design']['source_sha256'])
        key = (job['load'], job['seed'])
        pair_graphs.setdefault(key, []).append(t['workload_sha256'])
        config = copy.deepcopy(t['configuration'])
        config.pop('loopback_ports')
        config.pop('admission_mode')
        controller = config['controller']
        for k in ('CONTROLLER_ADMISSION_POLICY', 'CONTROLLER_ADMISSION_CAUTION_RPS', 'CONTROLLER_ADMISSION_RECOVERY_RPS'):
            controller.pop(k)
        pair_configs.setdefault(key, []).append(config)
        ready, stop = t['cloud_restoration_readiness_seconds'], t['live_generator_stop_seconds']
        fixed = [r for r in t['live_reads'] if ready <= r['started_elapsed'] < ready+10]
        transitions, previous = [], None
        for s in t['samples']:
            c = s['controller']
            state = (c['state'], c['rate_rps'], c['telemetry_fresh'], c['reason'])
            if state != previous:
                transitions.append(dict(elapsed_seconds=s['elapsed_seconds'], state=state[0], rate=state[1], fresh=state[2], reason=state[3]))
                previous = state
        audit.append(dict(name=job['name'], source=str(path), seed=job['seed'], load=job['load'], arm=job['arm'],
                          generator=generator_accuracy(t), drain=t['cloud_drain_interval_seconds'],
                          full_run=client(t['live_reads']), fixed10=client(fixed) if stop >= ready+10 else None,
                          post_ready=client([r for r in t['live_reads'] if r['started_elapsed'] >= ready]),
                          readiness=ready, post_ready_exposure=stop-ready, transitions=transitions,
                          site_metrics=trial_metrics(t, original['design']['cases'][0]['workload']),
                          postgres_tests=t['postgres_test_output'], retained_volumes=t['retained_database_volumes']))
        # Pilot condition IDs omit offered load. Add it only in a derived copy,
        # retaining every original summary/raw file and its hash unchanged.
        derived = copy.deepcopy(original)
        suffix = '_load30' if job['load'] == 36 else '_load70'
        derived['design']['cases'][0]['condition'] += suffix
        derived['trials'][0]['condition'] += suffix
        reports.append(derived)
    assert all(f == fingerprints[0] for f in fingerprints)
    assert all(len(v) == 2 and v[0] == v[1] for v in pair_graphs.values())
    assert all(len(v) == 2 and v[0] == v[1] for v in pair_configs.values())
    result = assess(reports, policies=['bounded_on'])
    result['generator_validity_protocol']['amendment'] = 'W5 .001 primary and strict sensitivities frozen prospectively in protocol.json; no outcome-based exclusions.'
    fixed_effects = {}
    for load in (36, 84):
        effects = []
        for seed in range(60101, 60106):
            arms = {a['arm']: a for a in audit if a['seed'] == seed and a['load'] == load}
            a, c = arms['aimd']['fixed10'], arms['constant']['fixed10']
            if a is not None and c is not None:
                effects.append(100*(a['fraction']-c['fraction']))
        fixed_effects[load] = paired(effects)
        fixed_effects[load]['mean_aimd_minus_constant_pp'] = fixed_effects[load].pop('mean_policy_minus_rr_on')
    result.update(audit=audit, source_hashes=hashes, fixed10_slo_pp=fixed_effects,
                  normalization='Derived pilot condition IDs add _load30/_load70 only; original files unchanged.',
                  source_pairing='All 20 fingerprints identical; exact paired workload/config audited except admission policy/state rates and assigned ports.')
    result['recorded_correctness_checks'] = dict(status='passed', trials=len(audit), original_events=88*len(audit),
        checks=['event identity/content', 'prerequisite application and grant order', 'complete original populations',
                'no outstanding tickets/grants', 'completed-grant duplicate POST fenced with 409 and unchanged row count',
                'SQLite integrity and local applied ACK state'],
        limitation='Recorded scenarios only; not complete failure/eligibility or duplicate-domain-effect coverage.')
    result['generator_replacement'] = replacement
    rejected = []
    for name, reason in [('w5-holdout-20261102', 'Built-in Docker bridge ID changed during engine startup'),
                         ('w5-holdout-20261102-r2', 'Pre-existing local services started concurrently; user subsequently paused other tests')]:
        path = ROOT / 'evaluation/results' / name / 'run-36-60101-constant/summary.json'
        if OUT.name != 'w5-holdout-20261102-r3':
            continue
        previous = json.loads(path.read_text())
        assert previous['result'] == 'failed' and previous['trials'][0]['result'] == 'passed'
        assert previous['design']['source_sha256'] == fingerprints[0]
        rejected.append(dict(source=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                             reason=reason, preservation=previous['preservation'],
                             container_state_changes=previous['container_state_changes'],
                             drain=previous['trials'][0]['cloud_drain_interval_seconds'],
                             disposition='Preserved unchanged; excluded from matched analysis for failed preservation; full batch restarted without policy changes'))
    result['excluded_infrastructure_attempts'] = rejected
    target = OUT / 'assessment.json'
    with target.open('x') as f:
        json.dump(result, f, indent=2)
    compact = evidence / 'w5-holdout-20261102.json'
    with compact.open('x') as f:
        json.dump({k: v for k, v in result.items() if k not in ('audit', 'per_site_measurements')}, f, indent=2)
    rows = [dict(name=a['name'], source=a['source'], seed=a['seed'], load=a['load'], arm=a['arm'], drain_upper=a['drain'][1],
                 routine_A=a['site_metrics'][0]['routine_post_reconnect_p95_upper_seconds'],
                 routine_B=a['site_metrics'][1]['routine_post_reconnect_p95_upper_seconds'],
                 routine_workflow_A=a['site_metrics'][0]['routine_workflow_p95_upper_seconds'],
                 routine_workflow_B=a['site_metrics'][1]['routine_workflow_p95_upper_seconds'],
                 violations=a['full_run']['violations'], attempts=a['full_run']['attempts'],
                 fixed10_violations=a['fixed10']['violations'] if a['fixed10'] else None,
                 fixed10_attempts=a['fixed10']['attempts'] if a['fixed10'] else None,
                 issuance=1-a['generator']['skip_fraction']) for a in audit]
    with (evidence / 'w5-holdout-20261102.csv').open('x', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps([c for c in result['comparisons'] if c['comparison'] == 'admission' and c['acceptance_applicable']], indent=2))


if __name__ == '__main__':
    main()
