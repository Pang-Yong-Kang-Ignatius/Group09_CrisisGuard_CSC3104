"""Deterministic recent-window telemetry oracles and real middleware boundaries."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cloud import telemetry as module
from cloud.telemetry import Telemetry, setup_telemetry
from cloud.controller.admission import Settings, assess


@pytest.fixture
def clock(monkeypatch):
    value = [0.0]
    monkeypatch.setattr(module, 'monotonic', lambda: value[0])
    return value


def test_empty_and_idle_windows_preserve_counters_but_not_historical_latency(clock):
    metrics = Telemetry()
    empty = metrics.snapshot()
    assert empty['p95_latency_ms'] is None
    assert empty['latency_sample_count'] == empty['window_requests'] == 0
    assert empty['average_latency_ms'] == empty['error_rate'] == empty['throughput_rps'] == 0
    metrics.record(503, 900)
    clock[0] = 61
    idle = metrics.snapshot()
    assert idle['p95_latency_ms'] is None and idle['latency_sample_count'] == 0
    assert idle['average_latency_ms'] == idle['error_rate'] == idle['throughput_rps'] == 0
    assert idle['total_requests'] == idle['failed_requests'] == 1


@pytest.mark.parametrize('durations,expected', [([7.123], 7.12), ([2, 10], 10),
    (list(range(1, 20)), 19), (list(range(1, 21)), 19), ([0]*19+[1000], 0),
    ([1000]+[0]*18, 1000), ([9, 3, 8, 1], 9)])
def test_nearest_rank_p95_low_samples_and_tail_boundary(clock, durations, expected):
    metrics = Telemetry()
    for duration in reversed(durations):
        metrics.record(200, duration)
    result = metrics.snapshot()
    assert result['p95_latency_ms'] == expected
    assert result['latency_sample_count'] == len(durations)
    assert result['average_latency_ms'] == round(sum(durations)/len(durations), 2)


def test_recent_window_boundary_and_expiry(clock):
    metrics = Telemetry()
    metrics.record(500, 1000)
    clock[0] = 60
    metrics.record(201, 10)
    assert metrics.snapshot()['window_requests'] == 2  # Inclusive cutoff.
    clock[0] = 60.001
    result = metrics.snapshot()
    assert result['window_requests'] == 1 and result['p95_latency_ms'] == 10
    assert result['error_rate'] == 0 and result['failed_requests'] == 1
    assert result['average_latency_ms'] == 10


def test_error_fraction_and_effective_throughput_denominator(clock):
    metrics = Telemetry()
    for status in [200, 201, 400, 422, 500, 503]:
        metrics.record(status, 5)
    clock[0] = 10
    result = metrics.snapshot()
    assert result['error_rate'] == .3333  # 4xx are not server errors.
    assert result['throughput_rps'] == .6 and result['observation_seconds'] == 10
    assert result['failed_requests'] == 2 and result['total_requests'] == 6
    clock[0] = 60
    assert metrics.snapshot()['throughput_rps'] == .1
    assert metrics.snapshot()['observation_seconds'] == 60


def test_high_volume_exact_percentile_and_concurrent_accounting(clock):
    metrics = Telemetry()
    def request(number):
        metrics.start_request()
        metrics.record(503 if number % 10 == 0 else 201, number, complete_request=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(request, range(1, 10001)))
    clock[0] = 60
    result = metrics.snapshot()
    assert result['p95_latency_ms'] == 9500
    assert result['latency_sample_count'] == result['total_requests'] == 10000
    assert result['failed_requests'] == 1000 and result['error_rate'] == .1
    assert result['throughput_rps'] == 166.6667
    assert result['in_flight_requests'] == 0


def test_normal_middleware_latency_and_monitoring_exclusion(monkeypatch):
    ticks = iter([1.0, 1.012345])
    monkeypatch.setattr(module, 'perf_counter', lambda: next(ticks))
    app = FastAPI()
    setup_telemetry(app, 'patient', lambda: None)
    @app.get('/work')
    def work():
        return {'ok': True}
    @app.get('/health')
    def health():
        return {'ok': True}
    with TestClient(app) as client:
        assert client.get('/work').status_code == 200
        result = client.get('/metrics').json()
        client.get('/health')
        assert client.get('/metrics').json()['total_requests'] == 1
    assert result['p95_latency_ms'] == result['average_latency_ms'] == 12.35
    assert result['latency_sample_count'] == 1 and result['in_flight_requests'] == 0
    assert result['database_healthy'] is True


def test_active_request_gauge_counts_work_not_monitoring_or_completed_requests():
    entered, release = threading.Event(), threading.Event()
    app = FastAPI()
    setup_telemetry(app, 'patient', lambda: None)
    @app.get('/held')
    def held():
        entered.set()
        assert release.wait(10)
        return {'ok': True}
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.get, '/held')
        try:
            assert entered.wait(5)
            for _ in range(3):
                result = client.get('/metrics').json()
                assert result['in_flight_requests'] == 1
                assert result['latency_sample_count'] == 0
                assert result['p95_latency_ms'] is None
        finally:
            release.set()
        assert request.result().status_code == 200
        result = client.get('/metrics').json()
        assert result['in_flight_requests'] == 0 and result['window_requests'] == 1


def test_exception_records_failure_releases_active_gauge_and_database_probe_is_real():
    healthy = [True]
    def probe():
        if not healthy[0]:
            raise RuntimeError('private database error')
    app = FastAPI()
    setup_telemetry(app, 'patient', probe)
    @app.get('/explode')
    def explode():
        raise RuntimeError('application failure')
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get('/metrics').json()['database_healthy'] is True
        assert client.get('/explode').status_code == 500
        healthy[0] = False
        response = client.get('/metrics')
        result = response.json()
    assert result['database_healthy'] is False
    assert result['in_flight_requests'] == 0
    assert result['failed_requests'] == 1 and result['error_rate'] == 1
    assert result['latency_sample_count'] == 1
    assert 'private database error' not in response.text


@pytest.mark.parametrize('populated', [False, True])
def test_extended_metrics_remain_compatible_with_unmodified_metaguard(clock, populated):
    now = datetime.now(timezone.utc)
    samples = {}
    for service in ('patient', 'emergency', 'admin'):
        metrics = Telemetry()
        if populated:
            metrics.record(201, 10)
        clock[0] += 60
        samples[service] = {**metrics.snapshot(), 'service': service,
            'sampled_at': now.isoformat(), 'database_healthy': True}
    assert assess(samples, Settings(), now)[:3] == (0, 'healthy', True)
    legacy = {name: {k:v for k,v in sample.items() if k not in
        ('p95_latency_ms', 'latency_sample_count', 'observation_seconds', 'in_flight_requests')}
        for name,sample in samples.items()}
    assert assess(legacy, Settings(), now) == assess(samples, Settings(), now)
