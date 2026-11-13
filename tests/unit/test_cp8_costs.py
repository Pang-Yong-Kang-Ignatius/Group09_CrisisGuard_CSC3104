from copy import deepcopy
from unittest.mock import Mock

import pytest

from evaluation.experiments.calibrate_event_costs import batch_cost
from evaluation.experiments.run_recovery_matrix import create_cost_delay, plan, validate_cost_profile, missed_live_opportunities, write_json, admission_settings


def profile():
    return {'units_by_type': {kind: 10 for kind in ('patient_registration', 'encounter_creation', 'emergency_record', 'patient_discharge')},
            'quantum': 10, 'heavy_event_units': 100, 'heavy_delay_seconds': .05}


def test_batch_cost_retains_rounding_uncertainty_and_population():
    before = dict(window_requests=10, total_requests=100, replay_latency_sample_count=10,
                  live_latency_sample_count=0, unclassified_latency_sample_count=0, average_latency_ms=1)
    after = dict(window_requests=30, total_requests=120, replay_latency_sample_count=30,
                 live_latency_sample_count=0, unclassified_latency_sample_count=0, average_latency_ms=3)
    result = batch_cost(before, after, 20)
    assert result['mean_service_wall_ms'] == 4
    assert result['rounding_interval_ms'] == [3.99, 4.01]
    contaminated = {**after, 'live_latency_sample_count': 1}
    with pytest.raises(AssertionError):
        batch_cost(before, contaminated, 20)
    with pytest.raises(AssertionError):
        batch_cost(before, {**after, 'window_requests': 29}, 20)


def test_cost_stress_is_paired_and_limits_expensive_work():
    cases = plan('cost-stress', cost_profile=profile())
    assert len(cases) == 5 and len({c['seed'] for c in cases}) == 5
    for case in cases:
        assert len(case['workload']['events']) == 44
        assert len(case['policies']) == 7
        expensive = [e for e in case['workload']['events'] if e['payload'].get('cp8_expensive')]
        assert len(expensive) == 5 and all(not e['prerequisite_ids'] for e in expensive)
        assert case['bypass_limits'] == {'bounded_b2': 2, 'bounded_b4': 4}
        for event in expensive:
            eid = event['event_id']
            assert case['event_cost_units']['drr_on'][eid] == 100
            assert case['event_cost_units']['drr_under'][eid] == 50
            assert case['event_cost_units']['drr_over'][eid] == 200
    assert cases == plan('cost-stress', cost_profile=profile())
    calibrated = plan('matrix', cost_profile=profile())[0]
    assert calibrated['quantum'] == 10 and 'heavy_delay_seconds' not in calibrated
    assert set(calibrated['event_cost_units']['drr_on'].values()) == {10}


@pytest.mark.parametrize('field,value', [('quantum', True), ('heavy_event_units', 501), ('heavy_delay_seconds', float('nan'))])
def test_invalid_profile_rejected_before_resources(field, value):
    data = deepcopy(profile())
    data[field] = value
    with pytest.raises(ValueError):
        validate_cost_profile(data)


def test_cost_delay_boundary_cannot_execute_unsafe_sql():
    conn = Mock()
    for value in (-1, 0, .51, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            create_cost_delay(conn, value)
    conn.execute.assert_not_called()


def test_live_generator_distinguishes_cap_from_driver_lag():
    assert missed_live_opportunities(1, 0, 1.2, 2) == (2, 0)
    assert missed_live_opportunities(1, 1.3, 1.4, 2) == (0, 2)
    assert missed_live_opportunities(1, 1.3, 1.7, 2) == (1, 2)
    assert missed_live_opportunities(1, 0, .2, 2) == (0, 0)


def test_checkpoint_failure_keeps_previous_evidence(tmp_path):
    path = tmp_path/'raw.json'
    write_json(path, {'result': 'previous checkpoint'})
    previous = path.read_bytes()
    circular = []
    circular.append(circular)
    with pytest.raises(ValueError, match='Circular reference'):
        write_json(path, circular)
    assert path.read_bytes() == previous


def test_constant_rate_is_distinct_from_state_fixed_and_keeps_common_caps(monkeypatch):
    from cloud.controller import admission
    constant = admission_settings('constant', 20)
    state_fixed = admission_settings('state-fixed', 20)
    assert constant['CONTROLLER_ADMISSION_POLICY'] == state_fixed['CONTROLLER_ADMISSION_POLICY'] == 'fixed'
    assert [float(constant['CONTROLLER_ADMISSION_'+state+'_RPS']) for state in ('NORMAL', 'CAUTION', 'RECOVERY')] == [20, 20, 20]
    assert [float(state_fixed['CONTROLLER_ADMISSION_'+state+'_RPS']) for state in ('NORMAL', 'CAUTION', 'RECOVERY')] == [20, 5, 2]
    assert all(constant[k] == state_fixed[k] for k in constant if k.startswith('CONTROLLER_ADMISSION_') and not k.endswith('_RPS'))
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY', 'fixed')
    config = admission.Settings(normal_rps=20, caution_rps=20, recovery_rps=20)
    assert all(admission.effective_rate({'state': state}, config) == 20 for state in ('Normal', 'Caution', 'Recovery'))


@pytest.mark.parametrize('mode', ['aimd', 'state-fixed', 'constant'])
def test_experiment_freezes_base_scheduler_settings_against_host_overrides(monkeypatch, mode):
    monkeypatch.setenv('SCHEDULER_ENABLE_INHERITANCE', 'invalid-host-value')
    monkeypatch.setenv('SCHEDULER_AGEING_INTERVAL_SECONDS', '30')
    settings = admission_settings(mode, 20)
    assert settings['SCHEDULER_ENABLE_INHERITANCE'] == '1'
    assert settings['SCHEDULER_AGEING_INTERVAL_SECONDS'] == '600'
