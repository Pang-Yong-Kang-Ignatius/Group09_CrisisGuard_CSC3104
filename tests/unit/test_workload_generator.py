"""Workload invariants and compatibility with the existing scheduler."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

import pytest

from evaluation.workloads.generate_workload import (
    CAPTURE_FIELDS, WorkloadConfig, capture_request, generate_workload,
    serialize, validate_workload,
)
from edge.scheduler.scheduler import select_next_event


BASE = WorkloadConfig(run_id="unit-test", total_events=88)


def test_reproducible_bytes_and_seed_variation():
    first = serialize(generate_workload(BASE))
    assert first == serialize(generate_workload(BASE))
    assert first != serialize(generate_workload(replace(BASE, seed=99)))
    assert first != serialize(generate_workload(replace(BASE, run_id="independent")))


@pytest.mark.parametrize("ratio", [(1, 1), (10, 1)])
@pytest.mark.parametrize("depth", [0, 3, 8])
@pytest.mark.parametrize("distribution", ["balanced", "a_only"])
def test_matrix_preserves_total_and_is_schedulable(ratio, depth, distribution):
    bundle = generate_workload(replace(BASE, backlog_ratio=ratio, dependency_depth=depth,
                                      urgent_distribution=distribution))
    assert sum(bundle["summary"]["events_by_site"].values()) == 88
    assert bundle["summary"]["events_by_site"]["Hospital_A"] == (44 if ratio == (1, 1) else 80)
    assert sum(w["urgent"] for w in bundle["workflows"]) == bundle["summary"]["urgent_workflow_count"]
    assert all(w["dependency_depth"] <= depth for w in bundle["workflows"])
    if distribution == "a_only":
        assert bundle["summary"]["urgent_workflows_by_site"]["Hospital_B"] == 0
    for site in ("Hospital_A", "Hospital_B"):
        events = [e for e in bundle["events"] if e["site_id"] == site]
        for inheritance in (True, False):
            applied = set()
            while len(applied) < len(events):
                selected = select_next_event(events, applied_ids=applied,
                    now=datetime.fromisoformat(bundle["config"]["snapshot_at"]),
                    enable_inheritance=inheritance)
                assert selected is not None
                assert set(selected["prerequisite_ids"]) <= applied
                assert selected["event_id"] not in applied
                applied.add(selected["event_id"])


def test_remainders_and_singleton_workflows_are_explicit():
    bundle = generate_workload(replace(BASE, total_events=11, backlog_ratio=(10, 1)))
    assert bundle["summary"]["events_by_site"] == {"Hospital_A": 10, "Hospital_B": 1}
    assert bundle["summary"]["workflow_depth_counts"] == {"0": 1, "1": 1, "3": 2}


def test_distribution_keeps_global_urgency_count_constant():
    balanced = generate_workload(BASE)
    concentrated = generate_workload(replace(BASE, urgent_distribution="a_only"))
    assert balanced["summary"]["urgent_workflow_count"] == concentrated["summary"]["urgent_workflow_count"]
    assert balanced["summary"]["workflow_count"] == concentrated["summary"]["workflow_count"]


def test_capture_projection_and_routine_prerequisites():
    bundle = generate_workload(BASE)
    by_id = {e["event_id"]: e for e in bundle["events"]}
    for event in bundle["events"]:
        assert set(capture_request(event)) == set(CAPTURE_FIELDS)
        assert "created_at" not in capture_request(event)
    for workflow in bundle["workflows"]:
        if workflow["urgent"]:
            assert by_id[workflow["completion_event_id"]]["base_priority"] == 5
            assert all(by_id[i]["base_priority"] <= 3 for i in workflow["required_event_ids"][:-1])


@pytest.mark.parametrize("changes", [
    {"total_events": 1}, {"total_events": 2, "backlog_ratio": (10, 1)},
    {"backlog_ratio": (0, 1)}, {"dependency_depth": -1},
    {"urgent_fraction": -0.1}, {"urgent_fraction": float("nan")},
    {"snapshot_at": "2026-10-21T00:00:00"}, {"capture_interval_seconds": -1},
    {"urgent_distribution": "unknown"}, {"run_id": "../bad"},
    {"urgent_distribution": "a_only", "urgent_fraction": 1.0},
])
def test_invalid_configs_fail(changes):
    with pytest.raises(ValueError):
        generate_workload(replace(BASE, **changes))


@pytest.mark.parametrize("corruption", ["duplicate", "missing", "cross_site", "cycle", "workflow"])
def test_validator_rejects_corrupt_graphs(corruption):
    bundle = deepcopy(generate_workload(replace(BASE, capture_interval_seconds=0)))
    first, second = bundle["events"][:2]
    if corruption == "duplicate":
        second["event_id"] = first["event_id"]
    elif corruption == "missing":
        first["prerequisite_ids"] = ["unknown"]
    elif corruption == "cross_site":
        first["prerequisite_ids"] = [next(e["event_id"] for e in bundle["events"] if e["site_id"] == "Hospital_B")]
    elif corruption == "cycle":
        first["prerequisite_ids"] = [second["event_id"]]
    else:
        bundle["workflows"][0]["completion_event_id"] = "unknown"
    with pytest.raises(ValueError):
        validate_workload(bundle)


def test_cli_generates_loadable_json_and_refuses_overwrite(tmp_path):
    script = Path(__file__).resolve().parents[2] / "evaluation/workloads/generate_workload.py"
    output = tmp_path / "sample.json"
    command = [sys.executable, str(script), "--run-id", "cli", "--total-events", "16", "--output", str(output)]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    original = output.read_bytes()
    validate_workload(json.loads(original))
    second = subprocess.run(command, capture_output=True, text=True)
    assert second.returncode != 0
    assert output.read_bytes() == original
