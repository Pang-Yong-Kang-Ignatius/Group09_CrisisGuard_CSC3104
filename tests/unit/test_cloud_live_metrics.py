"""Live/replay provenance and real executor queue occupancy; no database access."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import anyio
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cloud import telemetry as module
from cloud.telemetry import Telemetry, setup_telemetry
from cloud.traffic import classify_traffic, traffic_headers, KIND_HEADER, TOKEN_HEADER
from edge.capture_api import cloud_client


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("CLOUD_LIVE_TELEMETRY_TOKEN", "live-private-" * 4)
    monkeypatch.setenv("CLOUD_REPLAY_TELEMETRY_TOKEN", "replay-private-" * 4)


@pytest.mark.parametrize("kind", ["live", "replay"])
def test_authenticated_roles_and_wrong_role_tokens(credentials, kind):
    from starlette.datastructures import Headers
    headers = traffic_headers(kind)
    assert classify_traffic(Headers(headers)) == kind
    headers[KIND_HEADER] = "replay" if kind == "live" else "live"
    assert classify_traffic(Headers(headers)) == "unclassified"


@pytest.mark.parametrize("headers", [{}, {KIND_HEADER: "live"},
    {KIND_HEADER: "replay", TOKEN_HEADER: "wrong"},
    {"X-MetaGuard-Grant": "some-grant"}, {KIND_HEADER: "unknown"}])
def test_missing_spoofed_or_grant_only_is_unclassified(credentials, headers):
    from starlette.datastructures import Headers
    assert classify_traffic(Headers(headers)) == "unclassified"


def test_missing_weak_equal_and_contradictory_credentials_fail_closed(credentials, monkeypatch):
    from starlette.datastructures import Headers
    live = traffic_headers("live")
    assert classify_traffic(Headers({**live, "X-MetaGuard-Grant": "grant"})) == "unclassified"
    monkeypatch.setenv("CLOUD_REPLAY_TELEMETRY_TOKEN", live[TOKEN_HEADER])
    assert classify_traffic(Headers(live)) == "unclassified"
    monkeypatch.setenv("CLOUD_LIVE_TELEMETRY_TOKEN", "short")
    assert traffic_headers("live") == {}
    assert classify_traffic(Headers(live)) == "unclassified"
    monkeypatch.delenv("CLOUD_LIVE_TELEMETRY_TOKEN")
    assert traffic_headers("live") == {}


def test_exact_live_rank_mixed_population_and_window_expiry(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    metrics = Telemetry()
    assert metrics.snapshot()["live_p95_latency_ms"] is None
    for duration in range(1, 21):
        metrics.record(201, duration, traffic_kind="live")
    for _ in range(100):
        metrics.record(503, 9000, traffic_kind="replay")
    metrics.record(400, 5000)
    clock[0] = 60
    sample = metrics.snapshot()
    assert sample["live_p95_latency_ms"] == 19
    assert sample["p95_latency_ms"] == 9000
    assert [sample[k + "_latency_sample_count"] for k in ("live", "replay", "unclassified")] == [20, 100, 1]
    assert sample["latency_sample_count"] == 121
    clock[0] = 60.001
    sample = metrics.snapshot()
    assert sample["live_p95_latency_ms"] is None
    assert sample["live_latency_sample_count"] == sample["replay_latency_sample_count"] == sample["unclassified_latency_sample_count"] == 0
    assert sample["total_requests"] == 121


def test_concurrent_classified_accounting(monkeypatch):
    monkeypatch.setattr(module, "monotonic", lambda: 0.0)
    metrics = Telemetry()
    def record(n):
        metrics.start_request()
        metrics.record(201, n, complete_request=True,
                       traffic_kind=("live", "replay", "unclassified")[n % 3])
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(record, range(6000)))
    sample = metrics.snapshot()
    assert sample["in_flight_requests"] == 0
    assert sample["latency_sample_count"] == 6000
    assert sample["live_p95_latency_ms"] == 5697
    assert all(sample[k + "_latency_sample_count"] == 2000 for k in ("live", "replay", "unclassified"))


def test_real_middleware_records_live_errors_and_excludes_monitoring(credentials):
    app = FastAPI()
    setup_telemetry(app, "test", lambda: None)
    @app.get("/work")
    def work():
        return {"ok": True}
    with TestClient(app) as client:
        client.get("/work", headers=traffic_headers("live"))
        client.get("/missing", headers=traffic_headers("live"))
        client.get("/work", headers=traffic_headers("replay"))
        client.get("/work")
        sample = client.get("/metrics", headers=traffic_headers("live")).json()
    assert sample["live_latency_sample_count"] == 2
    assert sample["replay_latency_sample_count"] == sample["unclassified_latency_sample_count"] == 1
    assert sample["live_p95_latency_ms"] is not None
    assert sample["executor_queue_depth"] == 0
    assert sample["executor_queue_pressure"] == 0


@pytest.mark.parametrize("grant", [None, "existing-grant"])
def test_edge_transport_marks_both_legacy_and_granted_replay(credentials, monkeypatch, grant):
    monkeypatch.setenv("HOSPITAL_CONTROLLER_TOKEN", "existing-authorization")
    captured = {}
    def post(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(201, json={"status": "applied"}, request=httpx.Request("POST", url))
    monkeypatch.setattr(cloud_client.httpx, "post", post)
    event = {"event_type": "patient_registration", "event_id": "test-only"}
    assert cloud_client.send_to_cloud(event, grant_id=grant) == {"status": "applied"}
    assert captured["json"] is event
    assert captured["headers"][KIND_HEADER] == "replay"
    if grant:
        assert captured["headers"]["X-MetaGuard-Grant"] == grant
        assert captured["headers"]["Authorization"] == "Bearer existing-authorization"
    else:
        assert "Authorization" not in captured["headers"]


def test_edge_reconciliation_get_is_authenticated_replay(credentials, monkeypatch):
    from starlette.datastructures import Headers
    captured = {}

    def get(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(
            200,
            json={"status": "applied", "site_id": "Hospital_A"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(cloud_client.httpx, "get", get)
    event = {"event_type": "patient_registration", "event_id": "existing-event"}

    assert cloud_client.get_cloud_event_status(event) == {
        "state": "applied",
        "site_id": "Hospital_A",
    }
    assert captured["headers"] == traffic_headers("replay")
    assert classify_traffic(Headers(captured["headers"])) == "replay"


@pytest.mark.parametrize("credential", [None, "short"])
def test_edge_reconciliation_missing_or_weak_credentials_send_no_marker(
    monkeypatch, credential
):
    captured = {}
    if credential is None:
        monkeypatch.delenv("CLOUD_REPLAY_TELEMETRY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CLOUD_REPLAY_TELEMETRY_TOKEN", credential)

    def get(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(
            200,
            json={"status": "applied", "site_id": "Hospital_A"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(cloud_client.httpx, "get", get)
    result = cloud_client.get_cloud_event_status(
        {"event_type": "patient_registration", "event_id": "existing-event"}
    )

    assert result == {"state": "applied", "site_id": "Hospital_A"}
    assert "headers" not in captured


def test_edge_reconciliation_invalid_replay_credential_is_unclassified(
    credentials, monkeypatch
):
    from starlette.datastructures import Headers
    captured = {}
    expected = traffic_headers("replay")[TOKEN_HEADER]
    monkeypatch.setenv("CLOUD_REPLAY_TELEMETRY_TOKEN", "invalid-replay-private-" * 2)

    def get(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(
            404,
            json={"detail": {"code": "EVENT_NOT_FOUND"}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(cloud_client.httpx, "get", get)
    result = cloud_client.get_cloud_event_status(
        {"event_type": "patient_registration", "event_id": "missing-event"}
    )
    monkeypatch.setenv("CLOUD_REPLAY_TELEMETRY_TOKEN", expected)

    assert result == {"state": "not_found"}
    assert classify_traffic(Headers(captured["headers"])) == "unclassified"


@pytest.mark.parametrize("queued", [1, 3])
def test_real_executor_queue_under_saturation_and_drain(queued):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        app = FastAPI()
        setup_telemetry(app, "test", lambda: None)
        @app.get("/held")
        def held():
            entered.set()
            assert release.wait(10)
            return {"ok": True}
        limiter = anyio.to_thread.current_default_thread_limiter()
        old_limit = limiter.total_tokens
        limiter.total_tokens = 1
        tasks = []
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                tasks.append(asyncio.create_task(client.get("/held")))
                async def until(predicate):
                    with anyio.fail_after(5):
                        while not predicate():
                            await anyio.sleep(.001)
                await until(entered.is_set)
                for _ in range(queued):
                    tasks.append(asyncio.create_task(client.get("/held")))
                await until(lambda: limiter.statistics().tasks_waiting == queued)
                metrics = asyncio.create_task(client.get("/metrics"))
                tasks.append(metrics)
                # Metrics captures real occupancy before its own DB probe joins the queue.
                await until(lambda: limiter.statistics().tasks_waiting == queued + 1)
                release.set()
                sample = (await metrics).json()
                assert sample["executor_queue_depth"] == queued
                assert sample["executor_active_workers"] == sample["executor_worker_limit"] == 1
                assert sample["executor_queue_pressure"] == queued
                await asyncio.gather(*tasks)
                idle = (await client.get("/metrics")).json()
                assert idle["executor_queue_depth"] == idle["executor_active_workers"] == 0
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            limiter.total_tokens = old_limit
    asyncio.run(scenario())


def test_dashboard_direct_cloud_event_marks_live_without_forwarding_browser_credentials(credentials, monkeypatch):
    from dashboard import main as dashboard
    captured = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def request(self, method, url, **kwargs):
            captured.append(kwargs)
            return httpx.Response(201, json={"status": "applied"})
    monkeypatch.setattr(dashboard.httpx, "AsyncClient", Client)
    asyncio.run(dashboard.forward_request("patient", "POST", "/events", {"event_id": "test-only"}))
    assert captured[0]["headers"] == traffic_headers("live")
    asyncio.run(dashboard.forward_request("patient", "GET", "/metrics"))
    assert "headers" not in captured[1]


@pytest.mark.parametrize("service", ["patient", "emergency", "admin"])
def test_all_cloud_apps_expose_additive_fields_without_database_access(credentials, monkeypatch, service):
    import importlib
    from cloud.database import db
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def cursor(self):
            return self
        def execute(self, *args):
            pass
        def fetchone(self):
            return ("mock-database",)
    monkeypatch.setattr(db, "get_connection", Connection)
    app = importlib.import_module(f"cloud.{service}_service.main").app
    # No lifespan: never initialize schema or start external services.
    with TestClient(app) as client:
        before = client.get("/metrics").json()["live_latency_sample_count"]
        assert client.get("/", headers=traffic_headers("live")).status_code == 200
        sample = client.get("/metrics").json()
    assert sample["live_latency_sample_count"] == before + 1
    assert sample["live_p95_latency_ms"] is not None
    assert sample["executor_queue_depth"] == 0
    assert sample["database_healthy"] is True
    assert sample["window_seconds"] == 60


def test_live_exception_and_database_failure_preserve_controller_contract(credentials):
    from datetime import datetime, timezone
    from cloud.controller.admission import Settings, assess
    def probe():
        raise RuntimeError("private connection details")
    app = FastAPI()
    setup_telemetry(app, "test", probe)
    @app.get("/explode")
    def explode():
        raise RuntimeError("application error")
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/explode", headers=traffic_headers("live")).status_code == 500
        response = client.get("/metrics")
        sample = response.json()
    assert sample["live_latency_sample_count"] == 1
    assert sample["live_p95_latency_ms"] is not None
    assert sample["error_rate"] == 1
    assert sample["in_flight_requests"] == 0
    assert sample["database_healthy"] is False
    assert "private" not in response.text
    assert "live-private-" not in response.text
    samples = {name: dict(sample, service=name) for name in ("patient", "emergency", "admin")}
    legacy_keys = {"average_latency_ms", "error_rate", "throughput_rps", "database_healthy",
                   "sampled_at", "service", "window_seconds", "uptime_seconds",
                   "total_requests", "failed_requests", "window_requests"}
    legacy = {name: {key: value for key, value in item.items() if key in legacy_keys}
              for name, item in samples.items()}
    now = datetime.now(timezone.utc)
    assert assess(samples, Settings(), now) == assess(legacy, Settings(), now)

def test_live_slo_violation_rate_excludes_replay(monkeypatch):
    monkeypatch.setenv("CLOUD_LIVE_SLO_MS", "250")
    monkeypatch.setattr(module, "monotonic", lambda: 0.0)

    metrics = Telemetry()

    for duration in (100, 200, 300, 400):
        metrics.record(200, duration, traffic_kind="live")

    for _ in range(10):
        metrics.record(200, 1000, traffic_kind="replay")

    metrics.record(200, 5000, traffic_kind="unclassified")

    sample = metrics.snapshot()

    assert sample["live_slo_threshold_ms"] == 250
    assert sample["live_slo_violation_count"] == 2
    assert sample["live_slo_violation_rate"] == 0.5
    assert sample["live_latency_sample_count"] == 4


def test_live_slo_unconfigured_returns_null(monkeypatch):
    monkeypatch.delenv("CLOUD_LIVE_SLO_MS", raising=False)

    metrics = Telemetry()
    metrics.record(200, 500, traffic_kind="live")

    sample = metrics.snapshot()

    assert sample["live_slo_threshold_ms"] is None
    assert sample["live_slo_violation_count"] is None
    assert sample["live_slo_violation_rate"] is None


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_live_slo_rejected(monkeypatch, value):
    monkeypatch.setenv("CLOUD_LIVE_SLO_MS", value)

    with pytest.raises(ValueError):
        Telemetry()


def test_live_slo_window_expiry(monkeypatch):
    monkeypatch.setenv("CLOUD_LIVE_SLO_MS", "250")

    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])

    metrics = Telemetry()
    metrics.record(200, 300, traffic_kind="live")

    sample = metrics.snapshot()
    assert sample["live_slo_violation_count"] == 1
    assert sample["live_slo_violation_rate"] == 1.0

    clock[0] = 60.001

    sample = metrics.snapshot()
    assert sample["live_latency_sample_count"] == 0
    assert sample["live_slo_violation_count"] == 0
    assert sample["live_slo_violation_rate"] is None


@pytest.mark.parametrize("service", ["patient", "emergency", "admin"])
def test_successful_event_get_requires_live_provenance_not_just_http_200(credentials, monkeypatch, service):
    import importlib
    from unittest.mock import Mock
    from cloud.database import db

    def reject_database_access(*args, **kwargs):
        pytest.fail("Telemetry classification unit test attempted PostgreSQL access")

    # Guard both the shared DB boundary and aliases imported by service modules.
    monkeypatch.setattr(db, "get_connection", reject_database_access)
    monkeypatch.setattr(db.psycopg, "connect", reject_database_access)
    api = importlib.import_module(f"cloud.{service}_service.main")
    # TestClient runs the real service lifespan. Stub its DB boundary, not the
    # lifespan/middleware or live authentication, so this stays a unit test in CI.
    initialize = Mock()
    read_event = Mock(side_effect=lambda event_id: {"event_id": event_id, "status": "applied"})
    monkeypatch.setattr(api, "init_db", initialize)
    monkeypatch.setattr(api, "get_cloud_event", read_event)
    metrics = api.app.state.telemetry
    before = metrics.snapshot()
    with TestClient(api.app) as client:
        for _ in range(100):
            assert client.get("/events/KNOWN").status_code == 200
        unmarked = metrics.snapshot()
        assert unmarked["total_requests"] == before["total_requests"] + 100
        assert unmarked["unclassified_latency_sample_count"] == before["unclassified_latency_sample_count"] + 100
        assert unmarked["live_latency_sample_count"] == before["live_latency_sample_count"]
        assert client.get("/events/KNOWN", headers=traffic_headers("live")).status_code == 200
        assert client.get("/events/KNOWN", headers=traffic_headers("replay")).status_code == 200
        assert client.get("/events/KNOWN", headers={KIND_HEADER:"live", TOKEN_HEADER:"wrong"}).status_code == 200
    after = metrics.snapshot()
    assert after["live_latency_sample_count"] == before["live_latency_sample_count"] + 1
    assert after["replay_latency_sample_count"] == before["replay_latency_sample_count"] + 1
    assert after["unclassified_latency_sample_count"] == before["unclassified_latency_sample_count"] + 101
    assert after["live_p95_latency_ms"] is not None
    initialize.assert_called_once_with()
    assert read_event.call_count == 103
    read_event.assert_called_with("KNOWN")


def test_dashboard_connection_failure_is_not_an_idle_telemetry_sample(monkeypatch):
    from dashboard import main as dashboard
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, *args, **kwargs):
            raise httpx.ConnectError("connection unavailable")
    monkeypatch.setattr(dashboard.httpx, "AsyncClient", Client)
    result = asyncio.run(dashboard.forward_request("patient", "GET", "/metrics"))
    assert result["http_status"] is None
    assert result["response"]["code"] == "SERVICE_UNREACHABLE"
    assert "live_latency_sample_count" not in result["response"]
    assert "live_p95_latency_ms" not in result["response"]
