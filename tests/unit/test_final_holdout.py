from copy import deepcopy
from collections import Counter

import pytest

from evaluation.experiments.run_recovery_matrix import holdout_plan
from evaluation.experiments.assess_final_holdout import fixed_window
from evaluation.experiments.assess_acceptance import assess


def test_frozen_holdout_pairs_and_counterbalanced_order():
    cases = holdout_plan()
    assert cases == holdout_plan()
    assert len(cases) == 20 and sum(len(c['arms']) for c in cases) == 40
    assert {c['seed'] for c in cases} == set(range(60101, 60106))
    assert {c['live_read_rps'] for c in cases} == {36, 84}
    assert all(len(c['workload']['events']) == 88 for c in cases)
    for comparison in ('admission', 'scheduler'):
        group = [c for c in cases if c['comparison'] == comparison]
        assert len(group) == 10
        counts = Counter(tuple(c['arms'][0]) for c in group)
        assert sorted(counts.values()) == [5, 5]
        for rate in (36, 84):
            counts = Counter(tuple(c['arms'][0]) for c in group if c['live_read_rps'] == rate)
            assert sorted(counts.values()) == [2, 3]
        assert all(c['condition'].startswith('ratio1-1_') if comparison == 'admission'
                   else c['condition'].startswith('ratio10-1_depth3_a_only_') for c in group)


def test_fixed_window_preserves_primary_failures_and_counts_errors_once():
    trial = dict(cloud_restoration_readiness_seconds=5, live_generator_stop_seconds=16,
        configuration={'live_slo_ms': 250}, live_reads=[
            {'started_elapsed': 0, 'error': 'ConnectError'},
            {'started_elapsed': 5, 'status_code': 200, 'latency_ms': 250},
            {'started_elapsed': 6, 'status_code': 503, 'latency_ms': 400},
            {'started_elapsed': 14.9, 'error': 'Timeout'},
            {'started_elapsed': 15, 'status_code': 200, 'latency_ms': 300}])
    before = deepcopy(trial)
    result = fixed_window(trial)
    assert result['attempts'] == 3 and result['violations'] == 2
    assert result['fraction'] == pytest.approx(2/3)
    assert trial == before
    trial['live_generator_stop_seconds'] = 14.99
    assert fixed_window(trial)['status'] == 'incomplete'


@pytest.mark.parametrize('field', ['metaguard_retry_policy', 'scheduler', 'controller'])
def test_different_experimental_treatments_are_not_admission_pairs(field):
    from evaluation.experiments.run_recovery_matrix import plan
    cases = plan('pilot', pilot_seeds=range(5))
    trials = []
    for case in cases:
        for mode in ('aimd', 'constant'):
            config = {'admission_mode': mode, 'live_slo_ms': 250, 'live_read_rps': 1}
            if mode == 'aimd':
                config[field] = {'policy': 'different'} if field != 'metaguard_retry_policy' else 'bounded-deferral'
            trials.append(dict(condition=case['condition'], seed=case['seed'], policy='bounded_on',
                result='passed', configuration=config,
                completion_intervals={e['event_id']: [8, 8] for e in case['workload']['events']},
                cloud_drain_interval_seconds=[8, 8], samples=[],
                live_generator_stop_seconds=1, live_target_opportunities=1, live_generator_lag_skips=0,
                live_reads=[{'status_code': 200, 'latency_ms': 1}]))
    raw = dict(design={'cases': cases}, trials=trials, preservation={'states': True})
    result = assess([raw])
    assert all(not c['per_seed'] and c['status'] == 'not established' for c in result['comparisons'])
