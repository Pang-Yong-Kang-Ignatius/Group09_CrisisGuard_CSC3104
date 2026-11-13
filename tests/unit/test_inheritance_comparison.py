from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from evaluation.experiments import compare_inheritance as runner
from evaluation.workloads.generate_workload import WorkloadConfig, generate_workload


def tiny_workload():
    # Independent oracle: P(1) -> U(5), alongside R(3), all captured simultaneously.
    events = []
    for event_id, priority, prerequisites, workflow in (
        ("P", 1, [], "urgent"), ("U", 5, ["P"], "urgent"), ("R", 3, [], "routine")
    ):
        events.append({
            "event_id": event_id, "site_id": "Hospital_A", "base_priority": priority,
            "prerequisite_ids": prerequisites, "payload": {"workflow_id": workflow},
            "status": "pending", "created_at": "2026-10-21T00:00:00+00:00",
            "event_type": "emergency_record" if priority == 5 else "patient_registration",
        })
    return {
        "config": {"total_events": 3, "snapshot_at": "2026-10-21T00:00:00+00:00"},
        "events": events,
        "workflows": [
            {"workflow_id": "urgent", "site_id": "Hospital_A", "urgent": True,
             "required_event_ids": ["P", "U"], "completion_event_id": "U", "dependency_depth": 1},
            {"workflow_id": "routine", "site_id": "Hospital_A", "urgent": False,
             "required_event_ids": ["R"], "completion_event_id": "R", "dependency_depth": 0},
        ],
    }


def test_known_order_and_completion_metrics():
    workload = tiny_workload()
    before = deepcopy(workload)
    report, events, workflows = runner.compare(workload, repetitions=2)
    off, on = report["summary"]
    assert off["urgent_completion_position_p95"] == 3
    assert on["urgent_completion_position_p95"] == 2
    assert off["routine_completion_position_p95"] == 1
    assert on["routine_completion_position_p95"] == 3
    assert off["routine_base_event_preceding_selections_p95"] == 1
    assert on["routine_base_event_preceding_selections_p95"] == 2
    for mode, expected in (("off", ["R", "P", "U"]), ("on", ["P", "U", "R"])):
        rows = [e for e in events if e["inheritance"] == mode]
        assert [e["event_id"] for e in sorted(rows, key=lambda e: e["completion_position"])] == expected
    assert len(workflows) == 4
    assert len(report["timing_samples"]) == 4
    assert workload == before


def test_percentile_empty_singleton_and_nearest_rank():
    assert runner.percentile([]) is None
    assert runner.percentile([7]) == 7
    assert runner.percentile(list(range(1, 21))) == 19


def test_hash_ignores_format_and_newline_style():
    workload = tiny_workload()
    reformatted = json.loads(json.dumps(workload, indent=4).replace("\n", "\r\n"))
    assert runner.canonical_hash(workload) == runner.canonical_hash(reformatted)


@pytest.mark.parametrize("depth", [0, 3, 8])
def test_generated_sites_preserve_safety_and_populations(depth):
    workload = generate_workload(WorkloadConfig(run_id="comparison", total_events=88,
        backlog_ratio=(10, 1), dependency_depth=depth, urgent_distribution="a_only"))
    report, events, workflows = runner.compare(workload, repetitions=2)
    for summary in report["summary"]:
        site, mode = summary["site_id"], summary["inheritance"]
        rows = [e for e in events if e["site_id"] == site and e["inheritance"] == mode]
        positions = {e["event_id"]: e["completion_position"] for e in rows}
        assert sorted(positions.values()) == list(range(1, len(rows) + 1))
        for event in workload["events"]:
            if event["site_id"] == site:
                assert all(positions[p] < positions[event["event_id"]] for p in event["prerequisite_ids"])
        assert summary["unresolved_events"] == 0
        if site == "Hospital_B":
            assert summary["urgent_workflow_count"] == 0
            assert summary["urgent_completion_position_p95"] is None
        if depth == 0:
            other = next(s for s in report["summary"] if s["site_id"] == site and s["inheritance"] != mode)
            assert summary["urgent_completion_position_p95"] == other["urgent_completion_position_p95"]


def test_early_stop_reports_unfinished_work(monkeypatch):
    monkeypatch.setattr(runner, "select_next_event", lambda *args, **kwargs: None)
    report, events, workflows = runner.compare(tiny_workload(), repetitions=1)
    for summary in report["summary"]:
        assert summary["unresolved_events"] == 3
        assert summary["urgent_unresolved_workflows"] == 1
        assert summary["routine_unresolved_workflows"] == 1
        assert summary["urgent_completion_position_p95"] is None
    assert all(not e["completed"] for e in events)
    assert all(not w["completed"] for w in workflows)


def test_rejects_scheduler_dependency_violation(monkeypatch):
    workload = tiny_workload()
    monkeypatch.setattr(runner, "select_next_event", lambda *args, **kwargs: workload["events"][1])
    with pytest.raises(ValueError, match="prerequisite"):
        runner.compare(workload, repetitions=1)


@pytest.mark.parametrize("repetitions,interval", [(0, 600), (1, 0), (1, float("nan"))])
def test_invalid_settings(repetitions, interval):
    with pytest.raises(ValueError):
        runner.compare(tiny_workload(), repetitions, interval)


def test_cli_outputs_and_preserves_existing_results(tmp_path):
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / "input.json"
    source.write_text(json.dumps(tiny_workload()), encoding="utf-8")
    output = tmp_path / "results"
    command = [sys.executable, "-m", "evaluation.experiments.compare_inheritance",
               "--workload", str(source), "--output-dir", str(output), "--repetitions", "1"]
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert {p.name for p in output.iterdir()} == {
        "summary.json", "summary.csv", "events.csv", "workflows.csv", "timing_samples.csv"
    }
    original = (output / "summary.json").read_bytes()
    report = json.loads(original)
    assert report["summary"][1]["urgent_completion_position_p95"] == 2
    assert subprocess.run(command, cwd=root, capture_output=True).returncode != 0
    assert (output / "summary.json").read_bytes() == original
