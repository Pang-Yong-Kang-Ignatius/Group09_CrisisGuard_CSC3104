import pytest
from evaluation.experiments.analyze_recovery_matrix import p95, paired, trial_metrics, memory_bytes, ticket_witnesses
from evaluation.experiments.run_recovery_matrix import plan, POLICIES, verify


def test_frozen_paired_matrix():
    cases = plan('matrix')
    assert len(cases) == 40
    assert cases == plan('matrix')
    assert len({c['condition'] for c in cases}) == 8
    assert {c['seed'] for c in cases} == set(range(3104, 3109))
    assert all(set(c['policies']) == set(POLICIES) and len(c['workload']['events']) == 44 for c in cases)
    assert len({tuple(c['policies']) for c in cases}) > 5


def test_paired_test_small_sample_limit_and_empty_populations():
    assert memory_bytes('1MiB / 8GiB') == 1024**2
    assert p95([]) is None and p95(list(range(1, 21))) == 19
    assert paired([1]*5)['exact_sign_flip_p'] == .0625
    assert paired([0]*5)['exact_sign_flip_p'] == 1
    assert paired([])['mean_policy_minus_rr_on'] is None


def test_unfinished_work_remains_in_denominator():
    workload = plan('pilot')[0]['workload']
    event = workload['events'][0]
    trial = dict(condition='x', seed=1, policy='bounded_on', result='failed',
                 completion_intervals={event['event_id']: [1, 2]}, samples=[], live_reads=[])
    rows = trial_metrics(trial, workload)
    assert sum(r['original_events'] for r in rows) == 44
    assert sum(r['unfinished_events'] for r in rows) == 43
    assert all(r['routine_workflow_p95_upper_seconds'] is None for r in rows)


def test_authenticated_live_read_slo_metrics_have_explicit_denominators():
    workload = plan('pilot')[0]['workload']
    trial = dict(condition='x', seed=1, policy='bounded_on', result='failed',
                 completion_intervals={}, samples=[],
                 configuration={'live_read_rps': 2, 'live_slo_ms': 250},
                 live_generator_cap_skips=1, live_generator_lag_skips=0,
                 live_reads=[{'status_code': 200, 'latency_ms': 100, 'start_lag_ms': 2},
                             {'status_code': 200, 'latency_ms': 300, 'start_lag_ms': 4},
                             {'status_code': 503, 'latency_ms': 500, 'start_lag_ms': 8}])
    rows = trial_metrics(trial, workload)
    assert all(r['live_offered'] == 4 and r['live_successes'] == 2 for r in rows)
    assert all(r['client_live_slo_violation_count'] == 1 for r in rows)
    assert all(r['client_live_slo_violation_rate'] == .5 for r in rows)
    assert all(r['client_live_success_p95_ms'] == 300 for r in rows)
    assert all(r['client_live_start_lag_p95_ms'] == 8 for r in rows)


def test_no_live_slo_threshold_does_not_invent_violations():
    workload = plan('pilot')[0]['workload']
    trial = dict(condition='x', seed=1, policy='bounded_on', result='failed',
                 completion_intervals={}, samples=[], live_reads=[], configuration={})
    assert all(r['client_live_slo_violation_rate'] is None
               and r['client_live_slo_violation_count'] is None
               for r in trial_metrics(trial, workload))


def test_pilot_seed_batch_reuses_identical_workload_definitions():
    cases = plan('pilot', pilot_seeds=(9191, 9192, 9193))
    assert [case['seed'] for case in cases] == [9191, 9192, 9193]
    assert len({case['condition'] for case in cases}) == 1
    assert all(case['policies'] == ['bounded_on'] for case in cases)
    assert plan('pilot')[0]['workload'] == cases[0]['workload']


def test_exact_verification_rejects_loss():
    with pytest.raises(AssertionError, match='Event-set mismatch'):
        verify({'a': {}}, [], [], [], [], {})


def test_single_policy_pilot_retains_denominator_without_inventing_pairs(tmp_path):
    from evaluation.experiments.analyze_recovery_matrix import analyze
    case = plan('pilot')[0]
    event = case['workload']['events'][0]
    trial = dict(condition=case['condition'], seed=case['seed'], policy='bounded_on', result='failed',
                 completion_intervals={event['event_id']: [1, 2]}, samples=[], live_reads=[])
    raw = dict(design={'cases': [case], 'configuration': {'mode': 'pilot'}}, trials=[trial],
               planned_trials=1, preservation={}, raw_backup_directory=str(tmp_path))
    result = analyze(raw, tmp_path/'analysis')
    assert result['total_original_events'] == 44 and result['total_observed_events'] == 1
    assert result['passed_trials'] == 0 and result['paired_comparisons'] == []


def test_ticket_witness_counts_allocations_not_completions():
    trial = {'configuration': {'scheduling': 'bounded'}, 'samples': [{'tickets': [
        {'hospital_id': 'A', 'queue_position': 1, 'bypass_count': 0, 'observed_audit_id': 0}]}],
        'grants': [{'grant_id': 'b', 'hospital_id': 'B', 'original_queue_position': 2},
                   {'grant_id': 'a', 'hospital_id': 'A', 'original_queue_position': 1}],
        'audit': [{'grant_id': 'b', 'action': 'allocate', 'id': 1}, {'grant_id': 'a', 'action': 'allocate', 'id': 2}]}
    assert ticket_witnesses(trial)[0]['other_allocations'] == 1
    assert not ticket_witnesses(trial)[0]['violated']
    trial['configuration']['scheduling'] = 'fifo'
    assert ticket_witnesses(trial)[0]['violated']
