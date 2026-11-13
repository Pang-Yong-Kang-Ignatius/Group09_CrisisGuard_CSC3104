"""Replay policy must affect real selection without weakening dependency safety."""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from dashboard import main as dashboard
from edge.capture_api import grant_client, main, scheduled_replay
from edge.database import db
from edge.scheduler.config import SchedulerSettings, load_scheduler_settings
from edge.scheduler.dag import DependencyDAG
from edge.scheduler.live_preview import inspect_hospital_queue
from edge.scheduler.priority import calculate_aged_priority, calculate_effective_priorities


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in ("SCHEDULER_ENABLE_INHERITANCE", "SCHEDULER_AGEING_INTERVAL_SECONDS",
                 "REPLAY_INHERITANCE", "REPLAY_SELECTION_POLICY", "CRISISGUARD_EXPERIMENTAL_POLICIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("METAGUARD_ENABLED", "0")


def test_default_policy_preserves_existing_behavior():
    assert load_scheduler_settings({}) == SchedulerSettings(True, 600.0)


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), (" TRUE ", True),
    ("0", False), ("false", False), (" False ", False),
])
def test_explicit_inheritance_configuration(raw, expected):
    settings = load_scheduler_settings({"SCHEDULER_ENABLE_INHERITANCE": raw})
    assert settings.enable_inheritance is expected


@pytest.mark.parametrize("raw", ["", "yes", "off", "2", False, None])
def test_invalid_inheritance_configuration_rejected(raw):
    with pytest.raises(ValueError, match="SCHEDULER_ENABLE_INHERITANCE"):
        load_scheduler_settings({"SCHEDULER_ENABLE_INHERITANCE": raw})


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "-inf", "1e999", "", "abc", True])
def test_invalid_ageing_configuration_rejected(raw):
    with pytest.raises(ValueError, match="SCHEDULER_AGEING_INTERVAL_SECONDS"):
        load_scheduler_settings({"SCHEDULER_AGEING_INTERVAL_SECONDS": raw})


@pytest.mark.parametrize("interval", [True, False, float("nan"), float("inf"), -float("inf"), 0, -1, "600", None])
@pytest.mark.parametrize("helper", ["aged", "effective"])
def test_helpers_reject_invalid_intervals_even_without_work(interval, helper):
    with pytest.raises(ValueError, match="Ageing interval"):
        if helper == "aged":
            calculate_aged_priority({"base_priority": 3}, ageing_interval_seconds=interval)
        else:
            calculate_effective_priorities(DependencyDAG([]), set(), ageing_interval_seconds=interval)


@pytest.mark.parametrize("now", [datetime(2026, 1, 1), "2026-01-01T00:00:00+00:00", 42])
@pytest.mark.parametrize("helper", ["aged", "effective"])
def test_helpers_validate_evaluation_time_before_early_return(now, helper):
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        if helper == "aged":
            calculate_aged_priority({"base_priority": 3}, now=now)
        else:
            calculate_effective_priorities(DependencyDAG([]), set(), now=now)


@pytest.fixture
def backlog(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hospital.db")
    db.init_db()
    for event_id, priority, dependencies in (
        ("A", 1, []), ("B", 5, ["A"]), ("C", 3, []),
        ("BLOCKED", 5, ["MISSING"]),
    ):
        db.save_event({
            "event_id": event_id, "site_id": "Hospital_A",
            "event_type": "patient_registration", "base_priority": priority,
            "prerequisite_ids": dependencies, "payload": {},
        })


def sender_recording(order):
    def send(event):
        if event["event_id"] == "B":
            assert next(e for e in db.get_events() if e["event_id"] == "A")["status"] == "applied"
        order.append(event["event_id"])
        return {"status": "applied", "event_id": event["event_id"]}
    return send


@pytest.mark.parametrize("inheritance,order", [("1", ["A", "B", "C"]), ("0", ["C", "A", "B"])])
@pytest.mark.parametrize("metaguard", [False, True])
def test_replay_and_default_preview_use_same_policy(backlog, monkeypatch, inheritance, order, metaguard):
    monkeypatch.setenv("SCHEDULER_ENABLE_INHERITANCE", inheritance)
    monkeypatch.setenv("METAGUARD_ENABLED", "1" if metaguard else "0")
    if metaguard:
        # Exercise the replay grant branch without connecting to a real controller.
        monkeypatch.setattr(grant_client, "pinned_event", lambda: None)
        monkeypatch.setattr(grant_client, "advertise_candidate", Mock())
        monkeypatch.setattr(grant_client, "transmit", lambda event, send: send(event))
    preview = inspect_hospital_queue()
    assert preview["next_event_id"] == order[0]
    assert preview["comparison_only"] is False
    assert preview["replay_policy"]["inheritance_enabled"] is (inheritance == "1")
    sent = []
    monkeypatch.setattr(scheduled_replay, "send_to_cloud", sender_recording(sent))
    scheduled_replay.replay_scheduled_events()
    assert sent == order
    blocked = next(e for e in db.get_events() if e["event_id"] == "BLOCKED")
    assert blocked["status"] == "pending"


def test_comparison_does_not_change_replay_policy_or_database(backlog, monkeypatch, caplog):
    monkeypatch.setenv("SCHEDULER_ENABLE_INHERITANCE", "0")
    before = db.get_events()
    preview = inspect_hospital_queue(enable_inheritance=True)
    assert preview["next_event_id"] == "A"
    assert preview["comparison_only"] is True
    assert preview["replay_policy"]["inheritance_enabled"] is False
    assert db.get_events() == before

    sent = []
    monkeypatch.setattr(scheduled_replay, "send_to_cloud", sender_recording(sent))
    with caplog.at_level(logging.INFO, logger=scheduled_replay.logger.name):
        scheduled_replay.replay_scheduled_events()
    assert sent == ["C", "A", "B"]
    traces = [json.loads(r.message.split(" ", 1)[1]) for r in caplog.records
              if r.message.startswith("scheduler.selection ")]
    assert traces[0]["scheduler_policy"] == {
        "inheritance_enabled": False, "ageing_interval_seconds": 600.0, "selection_policy": "priority",
    }


@pytest.mark.parametrize("interval,expected", [("600", "C"), ("30", "A")])
def test_ageing_setting_changes_actual_replay_order(backlog, monkeypatch, interval, expected):
    monkeypatch.setenv("SCHEDULER_ENABLE_INHERITANCE", "0")
    monkeypatch.setenv("SCHEDULER_AGEING_INTERVAL_SECONDS", interval)
    now = datetime.now(timezone.utc)
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE events SET created_at=?", (now.isoformat(),))
        conn.execute("UPDATE events SET created_at=? WHERE event_id='A'",
                     ((now - timedelta(seconds=100)).isoformat(),))
    preview = inspect_hospital_queue()
    assert preview["next_event_id"] == expected
    assert preview["ageing_interval_seconds"] == float(interval)
    sent = []
    monkeypatch.setattr(scheduled_replay, "send_to_cloud", sender_recording(sent))
    scheduled_replay.replay_scheduled_events()
    assert sent[0] == expected


def test_invalid_configuration_cannot_claim_or_send(monkeypatch):
    monkeypatch.setenv("SCHEDULER_AGEING_INTERVAL_SECONDS", "nan")
    read = Mock()
    claim = Mock()
    send = Mock()
    monkeypatch.setattr(scheduled_replay, "get_events", read)
    monkeypatch.setattr(scheduled_replay, "claim_event", claim)
    monkeypatch.setattr(scheduled_replay, "send_to_cloud", send)
    with pytest.raises(ValueError, match="SCHEDULER_AGEING_INTERVAL_SECONDS"):
        scheduled_replay.replay_scheduled_events()
    read.assert_not_called()
    claim.assert_not_called()
    send.assert_not_called()


def test_invalid_configuration_rejects_startup_before_database_initialization(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLE_INHERITANCE", "yes")
    initialize = Mock()
    monkeypatch.setattr(main, "init_db", initialize)
    with pytest.raises(ValueError, match="SCHEDULER_ENABLE_INHERITANCE"):
        with TestClient(main.app):
            pass
    initialize.assert_not_called()


def test_inspector_api_defaults_to_active_policy_and_comparison_is_read_only(backlog, monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLE_INHERITANCE", "0")
    client = TestClient(main.app)
    before = db.get_events()
    active = client.get("/scheduler/inspect").json()
    comparison = client.get("/scheduler/inspect?inheritance=true").json()
    assert active["next_event_id"] == "C"
    assert active["comparison_only"] is False
    assert comparison["next_event_id"] == "A"
    assert comparison["comparison_only"] is True
    assert comparison["replay_policy"] == active["replay_policy"]
    assert db.get_events() == before


@pytest.mark.parametrize("override,params", [(None, {}), (False, {"inheritance": "false"}), (True, {"inheritance": "true"})])
def test_dashboard_forwards_active_policy_or_explicit_comparison(monkeypatch, override, params):
    async def request(method, url, **kwargs):
        assert method == "GET"
        assert kwargs["params"] == params
        return httpx.Response(200, json={"comparison_only": override is not None},
                              request=httpx.Request(method, url))
    monkeypatch.setattr(dashboard, "hospital_request", request)
    result = asyncio.run(dashboard.dashboard_scheduler_inspect("hospital-a", inheritance=override))
    assert result == {"comparison_only": override is not None}


@pytest.mark.parametrize("values", [
    {"REPLAY_INHERITANCE": "0"},
    {"REPLAY_SELECTION_POLICY": "fifo"},
    {"SCHEDULER_ENABLE_INHERITANCE": "0", "REPLAY_INHERITANCE": "1"},
])
def test_experimental_settings_require_opt_in(values):
    with pytest.raises(ValueError, match="explicit opt-in"):
        load_scheduler_settings(values)


@pytest.mark.parametrize("base,override,expected", [("1", "0", False), ("0", "1", True)])
def test_shared_experimental_override_preserves_ageing(monkeypatch, base, override, expected):
    from cloud.controller.experimental import replay_settings
    for key, value in {"SCHEDULER_ENABLE_INHERITANCE": base,
                       "SCHEDULER_AGEING_INTERVAL_SECONDS": "30",
                       "CRISISGUARD_EXPERIMENTAL_POLICIES": "1",
                       "REPLAY_INHERITANCE": override,
                       "REPLAY_SELECTION_POLICY": "fifo"}.items():
        monkeypatch.setenv(key, value)
    settings = load_scheduler_settings()
    assert settings == SchedulerSettings(expected, 30.0, "fifo")
    assert replay_settings() == (expected, "fifo")


def test_explicit_default_experimental_alias_keeps_default_compatibility():
    assert load_scheduler_settings({"REPLAY_INHERITANCE": "1"}) == SchedulerSettings()


@pytest.mark.parametrize("value", [None, 1])
def test_experimental_mapping_rejects_non_string_inheritance(value):
    with pytest.raises(ValueError, match="experimental replay policy"):
        load_scheduler_settings({"CRISISGUARD_EXPERIMENTAL_POLICIES": "1", "REPLAY_INHERITANCE": value})


def test_invalid_base_policy_cannot_be_hidden_by_experimental_override():
    with pytest.raises(ValueError, match="SCHEDULER_ENABLE_INHERITANCE"):
        load_scheduler_settings({"SCHEDULER_ENABLE_INHERITANCE": "yes",
                                 "REPLAY_INHERITANCE": "1", "CRISISGUARD_EXPERIMENTAL_POLICIES": "1"})


def test_fifo_preview_replay_and_logs_share_policy(backlog, monkeypatch, caplog):
    monkeypatch.setenv("SCHEDULER_ENABLE_INHERITANCE", "0")
    monkeypatch.setenv("REPLAY_SELECTION_POLICY", "fifo")
    monkeypatch.setenv("CRISISGUARD_EXPERIMENTAL_POLICIES", "1")
    before = db.get_events()
    preview = inspect_hospital_queue()
    assert db.get_events() == before
    assert preview["next_event_id"] == preview["scheduler_next_event_id"] == "A"
    assert [row["event_id"] for row in preview["events"] if row["selected"]] == ["A"]
    assert preview["replay_policy"] == {"inheritance_enabled": False,
                                         "ageing_interval_seconds": 600.0, "selection_policy": "fifo"}
    sent = []
    monkeypatch.setattr(scheduled_replay, "send_to_cloud", sender_recording(sent))
    with caplog.at_level(logging.INFO, logger=scheduled_replay.logger.name):
        scheduled_replay.replay_scheduled_events()
    assert sent == ["A", "B", "C"]
    traces = [json.loads(record.message.split(" ", 1)[1]) for record in caplog.records
              if record.message.startswith("scheduler.selection ")]
    assert traces[0]["scheduler_policy"] == preview["replay_policy"]


def test_settings_reject_unknown_selection_policy():
    with pytest.raises(ValueError, match="selection_policy"):
        SchedulerSettings(selection_policy="random")
