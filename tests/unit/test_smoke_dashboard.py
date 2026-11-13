"""Keep dashboard smoke assertions from accepting misleading HTTP 200s."""
from copy import deepcopy
import json

import pytest

from infrastructure.scripts import smoke_deployment as smoke


def dashboard_views(status="applied", reachable=True):
    chains = [smoke.event_chain("test", "combined", site) for site in ("Hospital_A", "Hospital_B")]
    hospitals = {}
    history = []
    for name, chain in zip(smoke.HOSPITAL_NAMES, chains):
        rows = [dict(event, status=status) for event in deepcopy(chain)]
        hospitals[name] = {"status": "healthy", "total": 4,
                           "pending": 4 if status == "pending" else 0,
                           "applied": 4 if status == "applied" else 0, "events": rows}
        if status == "applied":
            history += [dict(row, applied_at="2026-10-24T00:00:00+00:00") for row in rows]
    services = {name: {"service": name, "http_status": 200 if reachable else None,
                       "response": {"service": name, "status": "healthy"} if reachable
                       else {"code": "SERVICE_UNREACHABLE"}} for name in smoke.SERVICE_NAMES}
    return services, hospitals, {"count": len(history), "events": history}, chains


@pytest.mark.parametrize("fault", ["nested_failure", "misrouted_service", "mixed_hospitals", "wrong_counts",
                                   "missing_history", "changed_payload", "missing_timestamp"])
def test_rejects_broken_dashboard_views(fault):
    services, hospitals, history, chains = dashboard_views()
    if fault == "nested_failure":
        services["admin"]["http_status"] = None
    elif fault == "misrouted_service":
        services["admin"]["response"]["service"] = "patient"
    elif fault == "mixed_hospitals":
        hospitals["hospital-b"]["events"] = hospitals["hospital-a"]["events"]
    elif fault == "wrong_counts":
        hospitals["hospital-a"]["pending"] = 4
    elif fault == "missing_history":
        history["events"].pop()
    elif fault == "changed_payload":
        history["events"][0]["payload"] = {"patient_id": "changed"}
    else:
        history["events"][0]["applied_at"] = ""
    with pytest.raises((RuntimeError, ValueError)):
        smoke.check_dashboard_views(services, hospitals, history, chains, "applied", True)


def test_outage_requires_explicit_unreachable_and_preserved_pending_data():
    services, hospitals, history, chains = dashboard_views("pending", False)
    smoke.check_dashboard_views(services, hospitals, history, chains, "pending", False)
    services["patient"]["http_status"] = 200
    with pytest.raises(RuntimeError, match="outage"):
        smoke.check_dashboard_views(services, hospitals, history, chains, "pending", False)


def metrics():
    return {name: {"service": name, "http_status": 200, "response": {
        "service": name, "database_healthy": True, "total_requests": 4, "failed_requests": 0,
        "window_requests": 4, "average_latency_ms": 1.0, "throughput_rps": 0.1,
        "error_rate": 0.0, "window_seconds": 60}} for name in smoke.SERVICE_NAMES}


@pytest.mark.parametrize("fault", ["unreachable", "wrong_service", "database_down", "negative",
                                   "nan", "no_traffic", "impossible_error_rate"])
def test_rejects_invalid_or_empty_metrics_for_exercised_service(fault):
    data = metrics()
    result = data["emergency"]
    body = result["response"]
    if fault == "unreachable":
        result["http_status"] = None
    elif fault == "wrong_service":
        body["service"] = "patient"
    elif fault == "database_down":
        body["database_healthy"] = False
    elif fault == "negative":
        body["average_latency_ms"] = -1
    elif fault == "nan":
        body["throughput_rps"] = float("nan")
    elif fault == "no_traffic":
        body["total_requests"] = body["window_requests"] = 0
    else:
        body["error_rate"] = 1.1
    with pytest.raises(RuntimeError):
        smoke.check_dashboard_metrics(data, {"emergency"})


def test_zero_traffic_is_valid_after_restart():
    data = metrics()
    for result in data.values():
        result["response"]["total_requests"] = result["response"]["window_requests"] = 0
    smoke.check_dashboard_metrics(data, set())


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_split_dashboard_uses_loopback_port_and_selected_hospital_endpoints(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(smoke.sys, "platform", platform)
    monkeypatch.setenv("DASHBOARD_PORT", "8080")
    settings = {"POSTGRES_USER": "test", "POSTGRES_PASSWORD": "test", "POSTGRES_DB": "test",
                "DASHBOARD_PORT": 49000, "HOSPITAL_A_PORT": 49001, "HOSPITAL_B_PORT": 49002}
    project = smoke.Project(tmp_path, "test", smoke.DOCKER / "compose.cloud.yaml", settings,
                            "test-image", ["patient", "emergency", "admin", "dashboard"],
                            peer_network="cg-smoke-test-peers")
    override = json.loads((tmp_path / "test.json").read_text())
    dashboard = override["services"]["dashboard"]
    assert dashboard["ports"] == ["127.0.0.1:49000:8080"]
    assert dashboard["environment"]["DASHBOARD_HOSPITAL_A_URL"] == "http://hospital-a:8000"
    assert dashboard["environment"]["DASHBOARD_HOSPITAL_B_URL"] == "http://hospital-b:8000"
    assert "DASHBOARD_PORT" not in project.env
    assert "extra_hosts" not in dashboard
    assert "postgres" not in override["services"]
    assert override["networks"] == {"smoke-peers": {"external": True, "name": "cg-smoke-test-peers"}}
    assert set(dashboard["networks"]) == {"default", "smoke-peers"}


@pytest.mark.parametrize("site", ["a", "b"])
def test_split_hospital_private_alias_is_site_specific(tmp_path, site):
    smoke.Project(tmp_path, "test", smoke.DOCKER / "compose.hospital.yaml", {},
                  "test-image", ["hospital"], peer_network="cg-smoke-test-peers",
                  peer_aliases={"hospital": "hospital-" + site})
    service = json.loads((tmp_path / "test.json").read_text())["services"]["hospital"]
    assert service["networks"]["smoke-peers"]["aliases"] == ["hospital-" + site]
    assert "ports" not in service  # Keep the base Compose loopback binding.


def test_peer_network_cleanup_uses_created_id_not_a_reused_name(monkeypatch):
    commands = []
    def fake(args, **kwargs):
        commands.append(args)
        return "created-network-id"
    monkeypatch.setattr(smoke, "run", fake)
    network = smoke.PeerNetwork("cg-smoke-test-peers")
    network.create()
    network.remove()
    network.remove()
    assert "--internal" in commands[0]
    assert commands[1] == ["docker", "network", "rm", "created-network-id"]
    assert len(commands) == 2


def test_existing_peer_network_is_not_removed_after_create_failure(monkeypatch):
    import subprocess
    commands = []
    def fail(args, **kwargs):
        commands.append(args)
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(smoke, "run", fail)
    network = smoke.PeerNetwork("cg-smoke-test-peers")
    with pytest.raises(subprocess.CalledProcessError): network.create()
    network.remove()
    assert len(commands) == 1
