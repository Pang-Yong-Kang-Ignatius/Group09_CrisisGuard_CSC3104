from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math

import httpx
import pytest

from cloud.controller.admission import Settings, assess, advance, settings
from edge.capture_api import grant_client
from tests.unit.test_metaguard_replay import local_event


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def metrics(now=NOW):
    return {name: {'service': name, 'sampled_at': now.isoformat(), 'database_healthy': True,
        'average_latency_ms': 10, 'error_rate': 0, 'throughput_rps': 1, 'uptime_seconds': 60,
        'window_seconds': 60, 'total_requests': 60, 'failed_requests': 0, 'window_requests': 60}
        for name in ('patient', 'emergency', 'admin')}


@pytest.mark.parametrize('field,value,tier', [('average_latency_ms', 250, 1),
    ('average_latency_ms', 1000, 2), ('error_rate', .05, 1), ('error_rate', .2, 2),
    ('throughput_rps', 20, 1), ('throughput_rps', 50, 2), ('database_healthy', False, 2)])
def test_real_metric_thresholds(field, value, tier):
    samples = metrics()
    samples['emergency'][field] = value
    assert assess(samples, Settings(), NOW)[0] == tier


@pytest.mark.parametrize('bad', [None, {}, [], {'error': 'unavailable'},
    {'average_latency_ms': math.nan}])
def test_missing_or_malformed_metrics_are_never_healthy(bad):
    samples = metrics()
    samples['patient'] = bad
    assert assess(samples, Settings(), NOW)[0] == 2


@pytest.mark.parametrize('age', [16, -10])
def test_stale_or_future_samples_trigger_recovery(age):
    assert assess(metrics(NOW-timedelta(seconds=age)), Settings(), NOW)[0] == 2


def test_recovery_is_stepwise_and_requires_consecutive_healthy_samples_and_dwell():
    config = Settings()
    row = {'state': 'Recovery', 'state_since': NOW, 'healthy_count': 0, 'reason': 'overload'}
    advance(row, 0, True, NOW+timedelta(seconds=5), config)
    advance(row, 0, True, NOW+timedelta(seconds=10), config)
    assert row['state'] == 'Recovery'
    advance(row, 0, True, NOW+timedelta(seconds=15), config)
    assert row['state'] == 'Caution'
    for offset in (20, 25):
        advance(row, 0, True, NOW+timedelta(seconds=offset), config)
        assert row['state'] == 'Caution'
    advance(row, 0, True, NOW+timedelta(seconds=30), config)
    assert row['state'] == 'Normal'


def test_hysteresis_band_does_not_flap_and_overload_degrades_immediately():
    config = Settings()
    row = {'state': 'Caution', 'state_since': NOW, 'healthy_count': 0, 'reason': 'latency'}
    for value in (249, 251, 240, 252, 200):
        samples = metrics(NOW+timedelta(seconds=30))
        samples['patient']['average_latency_ms'] = value
        tier, _, healthy, _ = assess(samples, config, NOW+timedelta(seconds=30))
        advance(row, tier, healthy, NOW+timedelta(seconds=30), config)
        assert row['state'] == 'Caution' and row['healthy_count'] == 0
    advance(row, 2, False, NOW+timedelta(seconds=31), config)
    assert row['state'] == 'Recovery'


@pytest.mark.parametrize('overrides', [{'normal_rps': 0}, {'caution_rps': 3},
    {'recovery_latency_ms': 200}, {'healthy_latency_ms': 300}, {'poll_seconds': 14},
    {'normal_rps': math.inf}, {'recovery_error_rate': 2}])
def test_unsafe_configuration_rejected(overrides):
    with pytest.raises(ValueError):
        replace(Settings(), **overrides)


def test_float_configuration_is_supported(monkeypatch):
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLL_SECONDS', '0.5')
    assert settings().poll_seconds == .5


def test_definite_ack_throttle_preserves_grant_instead_of_reconciling(local_event, monkeypatch):
    from cloud.metaguard import event_digest
    from unittest.mock import Mock
    from uuid import uuid4
    gid = str(uuid4())
    calls = []
    def request(method, path, body=None):
        if path.endswith('/candidate'):
            return {'status': 'issued'}
        calls.append(path)
        if method == 'GET':
            return {'grant': {'grant_id': gid, 'status': 'issued', 'event_digest': event_digest(local_event)}}
        response = httpx.Response(429, request=httpx.Request('POST', 'http://controller/ack'))
        raise httpx.HTTPStatusError('Deferred', request=response.request, response=response)
    monkeypatch.setattr(grant_client, '_request', request)
    send = Mock()
    for _ in range(2):
        assert grant_client.transmit(local_event, send)['status'] == 'waiting_for_admission'
    assert not any(path.endswith('/reconcile') for path in calls)
    send.assert_not_called()

@pytest.mark.parametrize('limit', [0, -1, math.inf, math.nan])
def test_invalid_additive_step_is_rejected(limit):
    with pytest.raises(ValueError):
        replace(Settings(), additive_rps=limit)


def test_unknown_policy_fails_closed(monkeypatch):
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY', 'unknown')
    with pytest.raises(ValueError):
        settings()


def test_aimd_halves_then_adds_after_healthy_windows_and_respects_bounds(monkeypatch):
    from cloud.controller.admission import effective_rate
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY', 'aimd')
    config = replace(Settings(), additive_rps=.3, healthy_samples=2, dwell_seconds=1)
    row = {'state': 'Normal', 'healthy_count': 0, 'state_since': NOW,
           'reason': 'healthy', 'rate_rps': 2.0, 'rate_healthy_count': 0}
    for index, expected in enumerate([1.0, .5, .25, .2, .2]):
        advance(row, 1, False, NOW+timedelta(seconds=index+1), config)
        assert effective_rate(row, config) == expected
    for index in range(12):
        before = row['rate_rps']
        advance(row, 0, True, NOW+timedelta(seconds=10+index), config)
        assert row['rate_rps'] == (min(2., before+.3) if index % 2 else before)
    assert row['rate_rps'] == 2.0


def test_aimd_hysteresis_band_resets_healthy_votes_without_changing_rate(monkeypatch):
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY', 'aimd')
    config=replace(Settings(), healthy_samples=2)
    row={'state':'Normal','healthy_count':0,'state_since':NOW,'reason':'healthy',
         'rate_rps':.8,'rate_healthy_count':0}
    advance(row,0,True,NOW,config)
    advance(row,0,False,NOW+timedelta(seconds=5),config)
    advance(row,0,True,NOW+timedelta(seconds=10),config)
    assert row['rate_rps']==.8 and row['rate_healthy_count']==1


def test_fixed_policy_preserves_existing_rate_configuration(monkeypatch):
    from cloud.controller.admission import effective_rate
    monkeypatch.delenv('CONTROLLER_ADMISSION_POLICY',raising=False)
    config=Settings()
    row={'state':'Normal','healthy_count':0,'state_since':NOW,'reason':'healthy'}
    advance(row,1,False,NOW,config)
    assert row['state']=='Caution' and effective_rate(row,config)==.5
    advance(row,2,False,NOW,config)
    assert row['state']=='Recovery' and effective_rate(row,config)==.2


def test_database_failure_cannot_hide_another_missing_telemetry_source():
    samples=metrics()
    samples['patient']['database_healthy']=False
    samples.pop('admin')
    assert assess(samples,Settings(),NOW)[3] is None


@pytest.mark.parametrize('age,expected', [(0,True),(15,True),(15.001,False),(-2,True),(-2.001,False)])
def test_freshness_boundary(age,expected):
    from cloud.controller.admission import telemetry_fresh
    assert telemetry_fresh({'observed_at':NOW-timedelta(seconds=age)},NOW,Settings()) is expected
