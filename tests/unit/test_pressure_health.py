from datetime import timedelta

import pytest

from cloud.controller import admission
from evaluation.experiments.run_recovery_matrix import admission_settings
from tests.unit.test_admission import NOW, metrics


def pressure_metrics(now=NOW):
    samples = metrics(now)
    for sample in samples.values():
        sample.update(throughput_rps=140, p95_latency_ms=16, latency_sample_count=60,
            live_p95_latency_ms=16, live_latency_sample_count=60,
            live_slo_threshold_ms=250, live_slo_violation_count=0, live_slo_violation_rate=0,
            executor_queue_depth=0, executor_worker_limit=40, executor_queue_pressure=0,
            executor_queue_sampled_at=now.isoformat())
    return samples


@pytest.fixture
def experimental(monkeypatch):
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES', '1')
    monkeypatch.setenv('CONTROLLER_ADMISSION_HEALTH_POLICY', 'pressure-experimental')
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY', 'aimd')
    return admission.Settings()


def test_legacy_default_and_experimental_gate(monkeypatch):
    monkeypatch.delenv('CONTROLLER_ADMISSION_HEALTH_POLICY', raising=False)
    assert admission.health_policy() == 'throughput'
    assert admission.assess(pressure_metrics(), admission.Settings(), NOW)[0] == 2
    monkeypatch.setenv('CONTROLLER_ADMISSION_HEALTH_POLICY', 'pressure-experimental')
    monkeypatch.delenv('CRISISGUARD_EXPERIMENTAL_POLICIES', raising=False)
    with pytest.raises(ValueError, match='opt-in'):
        admission.settings()
    monkeypatch.setenv('CONTROLLER_ADMISSION_HEALTH_POLICY', 'typo')
    with pytest.raises(ValueError, match='Unknown'):
        admission.settings()


def test_healthy_high_demand_and_real_idle_services(experimental):
    data = pressure_metrics()
    data['patient']['throughput_rps'] = 10000  # Load descriptor, not an overload label.
    for name in ('emergency', 'admin'):
        data[name].update(throughput_rps=0, window_requests=0, latency_sample_count=0,
            p95_latency_ms=None, live_latency_sample_count=0, live_p95_latency_ms=None,
            live_slo_violation_count=0, live_slo_violation_rate=None)
    assert admission.assess(data, experimental, NOW) == (0, 'healthy', True, NOW)


@pytest.mark.parametrize('changes,tier,reason', [
    ({'p95_latency_ms': 250}, 1, 'p95_latency_ms'),
    ({'live_p95_latency_ms': 1000}, 2, 'live_p95_latency_ms'),
    ({'error_rate': .05}, 1, 'error_rate'),
    ({'error_rate': .2}, 2, 'error_rate'),
    ({'live_slo_violation_count': 3, 'live_slo_violation_rate': .05}, 1, 'live_slo_violation_rate'),
    ({'live_slo_violation_count': 12, 'live_slo_violation_rate': .2}, 2, 'live_slo_violation_rate'),
    ({'executor_queue_depth': 1, 'executor_queue_pressure': .025}, 1, 'executor_queue_pressure'),
    ({'executor_queue_depth': 40, 'executor_queue_pressure': 1}, 2, 'executor_queue_pressure'),
    ({'executor_queue_depth': 80, 'executor_queue_pressure': 2}, 2, 'executor_queue_pressure'),
    ({'database_healthy': False}, 2, 'database_unhealthy'),
])
def test_unhealthy_conditions_halve_admission_repeatedly(experimental, changes, tier, reason):
    samples = pressure_metrics()
    samples['patient'].update(changes)
    severity, why, healthy, observed = admission.assess(samples, experimental, NOW)
    assert (severity, why, healthy, observed) == (tier, 'patient:'+reason, False, NOW)
    row = dict(state='Normal', state_since=NOW, healthy_count=0, reason=why,
               rate_rps=2., rate_healthy_count=2)
    for i in range(5):
        admission.advance(row, severity, healthy, NOW+timedelta(seconds=i), experimental)
    assert row['rate_rps'] == experimental.recovery_rps
    assert row['rate_healthy_count'] == 0
    assert row['state'] == ('Recovery' if tier == 2 else 'Caution')


@pytest.mark.parametrize('key,value', [
    ('sampled_at', (NOW-timedelta(seconds=16)).isoformat()),
    ('executor_queue_sampled_at', (NOW-timedelta(seconds=16)).isoformat()),
    ('executor_queue_sampled_at', (NOW+timedelta(seconds=3)).isoformat()),
    ('executor_queue_pressure', float('nan')), ('executor_queue_pressure', .1),
    ('executor_worker_limit', 0), ('executor_queue_depth', True),
    ('p95_latency_ms', None), ('live_p95_latency_ms', None),
    ('live_latency_sample_count', 61), ('live_slo_threshold_ms', None),
    ('live_slo_violation_count', 61), ('live_slo_violation_rate', .1),
])
def test_invalid_or_stale_pressure_metrics_fail_closed(experimental, key, value):
    data = pressure_metrics()
    data['patient'][key] = value
    assert admission.assess(data, experimental, NOW) == (2, 'telemetry_missing_invalid_or_stale', False, None)


@pytest.mark.parametrize('missing', ['admin', 'live_p95_latency_ms', 'executor_queue_depth'])
def test_missing_source_or_required_field_fails_closed(experimental, missing):
    data = pressure_metrics()
    if missing == 'admin':
        data.pop(missing)
    else:
        data['admin'].pop(missing)
    assert admission.assess(data, experimental, NOW)[0:4:3] == (2, None)


def test_replay_tail_protects_service_without_live_samples(experimental):
    data = pressure_metrics()
    data['emergency'].update(live_latency_sample_count=0, live_p95_latency_ms=None,
        live_slo_violation_count=0, live_slo_violation_rate=None, p95_latency_ms=1000)
    assert admission.assess(data, experimental, NOW)[:3] == (2, 'emergency:p95_latency_ms', False)


def test_recovery_is_gradual_and_dead_band_resets_votes(experimental):
    row = dict(state='Recovery', state_since=NOW, healthy_count=0, reason='outage',
               rate_rps=.2, rate_healthy_count=0)
    for i in range(1, 7):
        now = NOW+timedelta(seconds=5*i)
        data = pressure_metrics(now)
        tier, _, healthy, _ = admission.assess(data, experimental, now)
        admission.advance(row, tier, healthy, now, experimental)
        assert row['state'] == ('Recovery' if i < 3 else 'Caution' if i < 6 else 'Normal')
        assert row['rate_rps'] == pytest.approx(.2 + .2*(i//3))
    data['patient']['p95_latency_ms'] = 200  # Between healthy and caution limits.
    tier, _, healthy, _ = admission.assess(data, experimental, now)
    assert tier == 0 and not healthy
    admission.advance(row, 0, True, now, experimental)
    admission.advance(row, tier, healthy, now, experimental)
    assert row['rate_healthy_count'] == 0 and row['rate_rps'] == pytest.approx(.6)


def test_pilot_opt_in_preserves_default_settings_and_rejects_mixed_profiles():
    def public(settings):
        return {k: v for k, v in settings.items() if k.startswith('CONTROLLER_ADMISSION_')}
    baseline = public(admission_settings('aimd', 20))
    changed = public(admission_settings('aimd', 20, health_policy='pressure-experimental'))
    assert changed == dict(baseline, CONTROLLER_ADMISSION_HEALTH_POLICY='pressure-experimental')
    with pytest.raises(ValueError):
        admission_settings('aimd', 20, (10, 20, 50), 'pressure-experimental')


@pytest.mark.parametrize('extra', [[], ['--mode', 'admission', '--live-slo-ms', '250'],
                                  ['--live-slo-ms', '250', '--reuse-summary', 'unused.json']])
def test_pilot_cli_rejects_uncontrolled_experimental_studies(monkeypatch, tmp_path, extra):
    from evaluation.experiments.run_recovery_matrix import main
    monkeypatch.setattr('sys.argv', ['runner', '--output', str(tmp_path/'new'), '--plan-only',
        '--admission-health-policy', 'pressure-experimental', *extra])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not (tmp_path/'new').exists()
