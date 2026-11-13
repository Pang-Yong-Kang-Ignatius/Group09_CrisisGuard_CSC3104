import pytest
from evaluation.experiments.run_recovery_matrix import acceptance_plan, state_changes
from evaluation.experiments.assess_acceptance import assess


def test_d2_loads_and_paired_arms():
    cases = acceptance_plan(100)
    assert len(cases) == 80
    assert len({c['condition'] for c in cases}) == 16
    assert {c['live_read_rps'] for c in cases} == {30, 70}
    assert sum(len(c['arms']) for c in cases) == 490
    assert all(len(c['workload']['events']) == 88 for c in cases)
    assert cases == acceptance_plan(100)
    assert all(a == 'aimd' for c in cases for p, a in c['arms'] if p != 'bounded_on')
    assert sum(('bounded_on','constant') in c['arms'] for c in cases) == 10
    assert all('fifo' not in c['policies'] for c in cases)


@pytest.mark.parametrize('capacity,total', [(float('nan'),440),(0,440),(100,44)])
def test_invalid_acceptance_plan(capacity, total):
    with pytest.raises(ValueError):
        acceptance_plan(capacity,total)


def test_state_changes_preserve_missing_and_added():
    assert state_changes({'a':'restarting','b':'exited'}, {'a':'running','c':'created'}) == {
        'a': {'before':'restarting','after':'running'}, 'b': {'before':'exited','after':None},
        'c': {'before':None,'after':'created'}}


def test_no_evidence_never_passes():
    result = assess([])
    assert result['comparisons'] == []
    assert result['correctness']['status'] == 'not established'


def test_matched_seed_effects_and_failed_preservation_exclusion():
    from evaluation.experiments.run_recovery_matrix import plan
    cases = plan('pilot', pilot_seeds=range(5))
    trials = []
    for case in cases:
        for mode, duration in [('constant', 10), ('aimd', 8)]:
            trials.append(dict(condition=case['condition'], seed=case['seed'], policy='bounded_on',
                result='passed', configuration={'admission_mode':mode,'live_slo_ms':250,'live_read_rps':1},
                completion_intervals={e['event_id']:[duration,duration] for e in case['workload']['events']},
                cloud_drain_interval_seconds=[duration,duration], samples=[],
                live_generator_stop_seconds=1, live_target_opportunities=1, live_generator_lag_skips=0, live_reads=[{'status_code':200,'latency_ms':1}]))
    raw = dict(design={'cases':cases},trials=trials,preservation={'states':True})
    result = assess([raw])
    drain = [c for c in result['comparisons'] if c['comparison']=='admission' and c['metric']=='cloud_drain_upper_seconds']
    assert len(drain)==1
    assert all(c['mean']==pytest.approx(-20) and c['status']=='achieved' for c in drain)
    raw['preservation']['states']=False
    assert all(c['status']=='not established' for c in assess([raw])['comparisons'])
    with pytest.raises(ValueError,match='Duplicate'):
        assess([raw,raw])


@pytest.fixture
def paired_report():
    from evaluation.experiments.run_recovery_matrix import plan
    cases = plan('pilot', pilot_seeds=range(5))
    trials = []
    for case in cases:
        for mode, duration in [('constant', 10), ('aimd', 8)]:
            trials.append(dict(condition=case['condition'], seed=case['seed'], policy='bounded_on',
                result='passed', cleanup='passed', configuration={'admission_mode':mode,'live_slo_ms':250,'live_read_rps':1},
                completion_intervals={e['event_id']:[duration,duration] for e in case['workload']['events']},
                cloud_drain_interval_seconds=[duration,duration], samples=[],
                live_generator_stop_seconds=4, live_target_opportunities=4, live_generator_lag_skips=0,
                reconnection_observations={site:{'interval_seconds':[0,0]} for site in ('Hospital_A','Hospital_B')},
                live_reads=[{'status_code':200,'latency_ms':250}, {'status_code':200,'latency_ms':251},
                            {'status_code':503,'latency_ms':400}, {'error':'ConnectError'}]))
    return dict(design={'cases':cases}, trials=trials, preservation={'states':True}, source_unchanged_during_run=True)


def test_failed_requests_are_outcomes_not_pair_exclusions(paired_report):
    result = assess([paired_report])
    admission = [c for c in result['comparisons'] if c['comparison']=='admission']
    assert all(c['uncertainty']['n']==5 and c['excluded_pairs']==0 for c in admission)
    slo = next(c for c in admission if c['metric']=='client_live_end_to_end_violation_rate')
    assert slo['mean']==0 and slo['status']=='achieved' and slo['site']=='global'
    row = result['per_site_measurements'][0]
    assert row['client_live_slo_violation_rate']==.5
    assert row['client_live_end_to_end_violation_rate']==.75
    assert row['client_live_success_p95_ms']==251
    assert row['live_transport_failures']==row['live_http_non_200_responses']==1


def test_zero_samples_leave_slo_unknown_and_do_not_claim_valid_load(paired_report):
    for trial in paired_report['trials']:
        trial['live_reads']=[]
    result = assess([paired_report])
    admission = [c for c in result['comparisons'] if c['comparison']=='admission']
    assert next(c for c in admission if c['metric']=='client_live_end_to_end_violation_rate')['status']=='not established'
    assert next(c for c in admission if c['metric']=='cloud_drain_upper_seconds')['status']=='not established'
    assert result['per_site_measurements'][0]['client_live_slo_violation_rate'] is None


@pytest.mark.parametrize('invalid', ['skips','preservation','failed','unfinished','source','cleanup','missing','mismatch'])
def test_invalid_trials_and_unmatched_pairs(paired_report, invalid):
    trial = paired_report['trials'][1]
    if invalid=='skips': trial['live_generator_cap_skips']=1
    elif invalid=='preservation': paired_report['preservation']['states']=False
    elif invalid=='failed': trial['result']='failed'
    elif invalid=='unfinished': trial['completion_intervals'].pop(next(iter(trial['completion_intervals'])))
    elif invalid=='source': paired_report['source_unchanged_during_run']=False
    elif invalid=='cleanup': trial['cleanup']='failed'
    elif invalid=='missing': paired_report['trials'].remove(trial)
    elif invalid=='mismatch': trial['configuration']['live_read_rps']=999
    comparisons = [c for c in assess([paired_report])['comparisons'] if c['comparison']=='admission']
    assert all(c['status']=='not established' and c['excluded_pairs']>0 for c in comparisons)


def test_all_requests_failed_is_one_not_missing(paired_report):
    for trial in paired_report['trials']:
        trial['live_reads']=[{'error':'ConnectError'}, {'status_code':500,'latency_ms':2}]
        trial['live_target_opportunities']=2
        trial['live_generator_stop_seconds']=2
    result = assess([paired_report])
    row = result['per_site_measurements'][0]
    assert row['client_live_end_to_end_violation_rate']==1
    assert row['client_live_slo_violation_rate'] is None


def test_same_adaptive_scheduler_comparison(paired_report):
    for trial in paired_report['trials']:
        trial['policy'] = 'rr_on' if trial['configuration']['admission_mode']=='constant' else 'bounded_on'
        trial['configuration']['admission_mode']='aimd'
    comparisons = [c for c in assess([paired_report])['comparisons'] if c['comparison']=='scheduler']
    assert all(c['uncertainty']['n']==5 for c in comparisons)


def test_common_workloads_and_cost_profile():
    from evaluation.experiments.run_recovery_matrix import plan
    import json
    from pathlib import Path
    profile=json.loads(Path('docs/evidence/cp8-cost-profile-20261030.json').read_text())
    cases=acceptance_plan(120,88,profile)
    original={(c['condition'],c['seed']):c for c in plan('matrix',88)}
    for case in cases:
        assert case['workload']==original[(case['condition'].rsplit('_load',1)[0],case['seed'])]['workload']
        assert set(case['event_cost_units']) >= set(case['policies'])
        assert all(len(w['required_event_ids']) in (1,4) for w in case['workload']['workflows'])
    assert {c['live_read_rps'] for c in cases}=={36,84}
    assert len({tuple(c['arms']) for c in cases})>5


def test_mixed_admission_analysis_does_not_collapse_arms(paired_report, tmp_path):
    from evaluation.experiments.analyze_recovery_matrix import analyze
    with pytest.raises(ValueError, match='Mixed admission'):
        analyze(paired_report, tmp_path/'must-not-exist')
    assert not (tmp_path/'must-not-exist').exists()


def test_absent_threshold_cannot_pass_slo(paired_report):
    for trial in paired_report['trials']:
        trial['configuration']['live_slo_ms']=None
    result=assess([paired_report])
    assert all(c['status']=='not established' for c in result['comparisons'] if c['metric']=='client_live_end_to_end_violation_rate')


def test_failure_difference_is_reported_as_failed_slo_not_excluded(paired_report):
    for trial in paired_report['trials']:
        trial['live_reads'] = ([{'status_code':200,'latency_ms':1}] if trial['configuration']['admission_mode']=='constant' else [{'error':'ConnectError'}])
        trial['live_target_opportunities']=1
        trial['live_generator_stop_seconds']=1
    comparisons=[c for c in assess([paired_report])['comparisons'] if c['comparison']=='admission']
    slo=next(c for c in comparisons if c['metric']=='client_live_end_to_end_violation_rate')
    assert slo['mean']==100 and slo['status']=='failed'
    assert slo['uncertainty']['exact_sign_flip_p']==.0625
    assert next(c for c in comparisons if c['metric']=='cloud_drain_upper_seconds')['status']=='achieved'


@pytest.mark.parametrize('git_head', ['a' * 40, None], ids=['git-checkout', 'github-zip'])
def test_filtered_block_preserves_full_plan_randomization(tmp_path, monkeypatch, git_head):
    import hashlib
    import json
    import sys
    from types import SimpleNamespace
    from evaluation.experiments import run_recovery_matrix as matrix
    from infrastructure.scripts import project_verify as provenance
    condition='ratio10-1_depth3_a_only_load70'
    output=tmp_path/'plan'
    source=tmp_path/'source'
    source.mkdir()
    monkeypatch.setattr(provenance, 'ROOT', source)
    if git_head:
        (source/'.git').mkdir()
        monkeypatch.setattr(provenance, 'run', lambda command, **kwargs: SimpleNamespace(
            returncode=0, stdout=(str(source) if command[-1]=='--show-toplevel' else git_head)+'\n'))
    else:
        monkeypatch.setattr(provenance, 'run', lambda *args, **kwargs: pytest.fail('Git called for a ZIP'))
    monkeypatch.setattr(sys,'argv',['run','--mode','acceptance','--capacity-rps','120',
        '--activation','automatic','--live-read-target','preseed','--live-slo-ms','250',
        '--acceptance-condition',condition,'--acceptance-seed','3104','--plan-only','--output',str(output)])
    matrix.main()
    design=json.loads((output/'plan.json').read_text())
    assert design['git_head'] == git_head
    assert design['git_head_status'] == ('PASS' if git_head else 'NOT VERIFIED')
    generator = matrix.ROOT / 'evaluation/experiments/run_recovery_matrix.py'
    assert design['source_sha256']['evaluation/experiments/run_recovery_matrix.py'] == hashlib.sha256(generator.read_bytes()).hexdigest()
    assert len(design['cases'])==1
    expected=next(c for c in acceptance_plan(120) if c['condition']==condition and c['seed']==3104)
    assert design['cases'][0]['arms']==[list(a) for a in expected['arms']]
    assert len(design['cases'][0]['arms'])==7
    assert design['configuration']['total_events']==88
    assert design['acceptance_protocol']['scheduler_admission']=='aimd'


@pytest.mark.parametrize('missing,limit,n', [(1,.001,5),(1,0,0),(1,.0001,0),(2,.001,0)])
def test_accuracy_budget_and_strict_sensitivity(paired_report, missing, limit, n):
    for trial in paired_report['trials']:
        trial['configuration']['live_read_rps']=100
        trial['live_generator_stop_seconds']=10
        trial['live_target_opportunities']=1000
        trial['live_generator_lag_skips']=missing
        trial['live_reads']=[{'status_code':200,'latency_ms':1} for _ in range(1000-missing)]
    result=assess([paired_report],limit)
    drain=next(c for c in result['comparisons'] if c['comparison']=='admission' and c['metric']=='cloud_drain_upper_seconds')
    assert drain['uncertainty']['n']==n
    assert {v['max_skip_fraction']:v['n'] for v in drain['generator_accuracy_sensitivity']} == {0.:0,.0001:0,.001:5 if missing==1 else 0}
    row=result['per_site_measurements'][0]
    assert row['generator_accuracy']['lag_skips']==missing
    assert row['scheduled_opportunity_slo_fraction_bounds']==[0,missing/1000]
    assert all(t['valid'] == bool(n) for t in result['trial_validity'])


def test_no_loosening_past_predefined_budget():
    for limit in (.01, -1, float('nan')):
        with pytest.raises(ValueError):
            assess([],limit)


def test_numeric_ceiling_serialization_does_not_break_pairing(paired_report):
    for trial in paired_report['trials']:
        trial['configuration']['controller']={'CONTROLLER_ADMISSION_NORMAL_RPS': '20' if trial['configuration']['admission_mode']=='constant' else '20.0'}
    result=assess([paired_report],policies=['bounded_on'])
    assert all(c['uncertainty']['n']==5 for c in result['comparisons'] if c['comparison']=='admission')
    assert not assess([paired_report],policies=['rr_on'])['trial_validity']
