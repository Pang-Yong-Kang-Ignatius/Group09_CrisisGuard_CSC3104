"""Reject false-positive smoke results without requiring a Docker engine."""
from copy import deepcopy

import pytest

from infrastructure.scripts import smoke_deployment as smoke


def stored_events(status="applied"):
    rows = deepcopy(smoke.event_chain("test", "split", "Hospital_A"))
    for index, row in enumerate(rows):
        row.update(status=status, applied_at=f"2026-10-23T12:00:0{index}+00:00")
    return rows


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "payload", "site", "status", "dependency"])
def test_reject_incomplete_corrupt_or_mixed_site_records(fault):
    expected = smoke.event_chain("test", "split", "Hospital_A")
    rows = stored_events()
    if fault == "missing":
        rows.pop()
    elif fault == "extra":
        rows.append(dict(rows[0], event_id="unexpected"))
    elif fault == "duplicate":
        rows[-1] = rows[0]
    elif fault == "payload":
        rows[0]["payload"] = {"patient_id": "changed"}
    elif fault == "site":
        rows[0]["site_id"] = "Hospital_B"
    elif fault == "status":
        rows[0]["status"] = "pending"
    else:
        rows[1]["prerequisite_ids"] = []
    with pytest.raises(RuntimeError):
        smoke.check_records(rows, expected, "applied")


def test_dependency_application_order_rejects_early_child():
    rows = stored_events()
    smoke.check_application_order(rows)
    rows[1]["applied_at"] = "2026-10-23T11:59:59+00:00"
    with pytest.raises(RuntimeError, match="prerequisite"):
        smoke.check_application_order(rows)


def healthy_state():
    return {"enabled": True, "cloud_reachable": True,
            "service_health": {"patient": True, "emergency": True, "admin": True},
            "replayable_backlog": 0, "last_error": None,
            "last_reconnected_at": "2026-10-23T12:00:00+00:00",
            "drain_completed_at": "2026-10-23T12:00:01+00:00"}


@pytest.mark.parametrize("fault", ["disabled", "partial_health", "backlog", "no_reconnect", "no_drain", "error"])
def test_timeout_includes_last_state_instead_of_false_success(monkeypatch, fault):
    state = healthy_state()
    if fault == "disabled":
        state["enabled"] = False
    elif fault == "partial_health":
        state["service_health"]["admin"] = False
    elif fault == "backlog":
        state["replayable_backlog"] = 1
    elif fault == "no_reconnect":
        state["last_reconnected_at"] = None
    elif fault == "no_drain":
        state["drain_completed_at"] = None
    else:
        state["last_error"] = "replay failed"
    monkeypatch.setattr(smoke, "request", lambda *args: state)
    with pytest.raises(RuntimeError, match="Timed out.*replayable_backlog"):
        smoke.wait_for_recovery(12345, True, 0, drained=True, timeout=0)


def test_waits_for_all_services_and_drain(monkeypatch):
    partial = healthy_state()
    partial["service_health"]["emergency"] = False
    final = healthy_state()
    states = iter([partial, final])
    monkeypatch.setattr(smoke, "request", lambda *args: next(states))
    monkeypatch.setattr(smoke.time, "sleep", lambda seconds: None)
    assert smoke.wait_for_recovery(12345, True, 0, drained=True) == final


def test_smoke_settings_override_ambient_replay_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_REPLAY", "0")
    monkeypatch.setenv("AUTO_REPLAY_INTERVAL_SECONDS", "9999")
    monkeypatch.setenv("COMPOSE_PROFILES", "unexpected")
    project = smoke.Project(tmp_path, "test", smoke.ROOT / "compose.yaml",
                            {"AUTO_REPLAY": "1", "AUTO_REPLAY_INTERVAL_SECONDS": "1"},
                            "test-image", ["hospital-a", "hospital-b"])
    assert "AUTO_REPLAY" not in project.env
    assert "AUTO_REPLAY_INTERVAL_SECONDS" not in project.env
    assert "COMPOSE_PROFILES" not in project.env
    assert (tmp_path / "test.env").read_text() == "AUTO_REPLAY=1\nAUTO_REPLAY_INTERVAL_SECONDS=1\n"


def test_cached_empty_backlog_does_not_hide_pending_capture(monkeypatch):
    expected = smoke.event_chain("test", "split", "Hospital_A")
    rows = iter([stored_events("pending"), stored_events("applied")])
    probes = []

    def request(port, path):
        probes.append(path)
        return healthy_state() if path == "/recovery/status" else next(rows)

    monkeypatch.setattr(smoke, "request", request)
    monkeypatch.setattr(smoke.time, "sleep", lambda seconds: None)
    smoke.wait_for_recovery(12345, True, 0, expected_events=expected)
    assert probes.count("/events") == 2


def test_pending_capture_times_out_even_with_cached_empty_backlog(monkeypatch):
    monkeypatch.setattr(smoke, "request", lambda port, path:
                        healthy_state() if path == "/recovery/status" else stored_events("pending"))
    with pytest.raises(RuntimeError, match="event_statuses=.*pending"):
        smoke.wait_for_recovery(12345, True, 0, timeout=0,
                                expected_events=smoke.event_chain("test", "split", "Hospital_A"))


@pytest.mark.parametrize("fault", ["missing", "extra", "payload", "site", "dependency"])
def test_applied_wait_retains_full_record_assertions(monkeypatch, fault):
    expected = smoke.event_chain("test", "split", "Hospital_A")
    rows = stored_events()
    if fault == "missing":
        rows.pop()
    elif fault == "extra":
        rows.append(dict(rows[0], event_id="unexpected"))
    elif fault == "payload":
        rows[0]["payload"] = {"patient_id": "changed"}
    elif fault == "site":
        rows[0]["site_id"] = "Hospital_B"
    else:
        rows[1]["prerequisite_ids"] = []
    monkeypatch.setattr(smoke, "request", lambda port, path:
                        healthy_state() if path == "/recovery/status" else rows)
    with pytest.raises(RuntimeError):
        smoke.wait_for_recovery(12345, True, 0, timeout=0, expected_events=expected)
