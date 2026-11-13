from copy import deepcopy
from dataclasses import replace
import json

import pytest

from evaluation.experiments.run_inheritance_matrix import (
    MatrixConfig, aggregate_pairs, build_cases, paired_rows, run_matrix,
)


BASE = MatrixConfig(run_id="matrix-test", seeds=(101, 102), total_events=44, repetitions=1)


def test_plan_has_eight_conditions_and_distinct_seeds():
    cases = build_cases(BASE)
    assert len(cases) == 16
    assert len({case["case_id"] for case in cases}) == 16
    assert len({case["condition"] for case in cases}) == 8
    assert {case["config"].total_events for case in cases} == {44}
    assert len(build_cases(MatrixConfig(run_id="default"))) == 40


def summaries():
    rows = []
    for mode, urgent, routine in (("off", 10, 20), ("on", 4, 22)):
        rows.append({
            "site_id": "Hospital_A", "inheritance": mode, "event_count": 24,
            "unresolved_events": 0,
            "urgent_workflow_count": 3, "routine_workflow_count": 3,
            "urgent_unresolved_workflows": 0, "routine_unresolved_workflows": 0,
            "urgent_completion_position_p95": urgent,
            "routine_completion_position_p95": routine,
            "scheduler_call_cpu_ms_median": 1.0,
        })
    return rows


def test_paired_metric_sign_and_percentage():
    row = paired_rows(summaries(), {"case_id": "c1", "condition": "c", "seed": 1})[0]
    assert row["urgent_p95_position_reduction"] == 6
    assert row["urgent_p95_position_reduction_pct"] == 60
    assert row["routine_p95_position_reduction"] == -2
    assert row["routine_p95_position_reduction_pct"] == -10


def test_empty_and_unfinished_groups_are_excluded():
    data = summaries()
    for row in data:
        row["urgent_workflow_count"] = 0
        row["urgent_completion_position_p95"] = None
    data[1]["routine_unresolved_workflows"] = 1
    pairs = paired_rows(data, {"case_id": "c1", "condition": "c", "seed": 1})
    assert pairs[0]["urgent_p95_position_reduction"] is None
    assert pairs[0]["routine_p95_position_reduction"] is None
    aggregate = aggregate_pairs(pairs)[0]
    assert aggregate["urgent_empty_population_pairs"] == 1
    assert aggregate["routine_incomplete_pairs"] == 1
    assert aggregate["urgent_mean_p95_position_reduction"] is None


def test_aggregates_pair_differences_not_pooled_workflows():
    first = paired_rows(summaries(), {"case_id": "c1", "condition": "c", "seed": 1})[0]
    second = deepcopy(first)
    second.update(seed=2, urgent_p95_position_reduction=-2,
                  urgent_p95_position_reduction_pct=-20)
    result = aggregate_pairs([first, second])[0]
    assert result["seed_count"] == 2
    assert result["urgent_mean_p95_position_reduction"] == 2
    assert result["urgent_min_p95_position_reduction"] == -2
    assert result["urgent_max_p95_position_reduction"] == 6
    assert result["urgent_improved_pairs"] == 1
    assert result["urgent_worsened_pairs"] == 1
    with pytest.raises(ValueError, match="duplicate seed"):
        aggregate_pairs([first, first])


@pytest.mark.parametrize("changes", [
    {"seeds": ()}, {"seeds": (1, 1)}, {"repetitions": 0},
    {"ageing_interval_seconds": 0}, {"ageing_interval_seconds": float("nan")},
])
def test_invalid_plan(changes):
    with pytest.raises(ValueError):
        build_cases(replace(BASE, **changes))


def test_complete_matrix_outputs_and_depth_zero_control(tmp_path):
    directory = tmp_path / "matrix"
    result = run_matrix(BASE, directory, progress=lambda _: None)
    assert result["status"] == "completed"
    assert result["case_count"] == 16
    assert result["off_unresolved_events_total"] == result["on_unresolved_events_total"] == 0
    assert len(list((directory / "runs").iterdir())) == 16
    for row in result["condition_site_groups"]:
        assert row["seed_count"] == 2
        if "depth0" in row["condition"]:
            assert row["routine_mean_p95_position_reduction"] == 0
            assert row["urgent_mean_p95_position_reduction"] in (0, None)
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["source_file_sha256"]
    summary_bytes = (directory / "matrix_summary.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        run_matrix(BASE, directory, progress=lambda _: None)
    assert (directory / "matrix_summary.json").read_bytes() == summary_bytes


def test_invalid_workload_does_not_create_results(tmp_path):
    directory = tmp_path / "invalid"
    with pytest.raises(ValueError):
        run_matrix(replace(BASE, total_events=1), directory, progress=lambda _: None)
    assert not directory.exists()


def test_interrupted_case_keeps_failure_record_without_success_marker(tmp_path, monkeypatch):
    def fail_comparison(*args, **kwargs):
        raise ValueError("injected comparison failure")

    monkeypatch.setattr("evaluation.experiments.run_inheritance_matrix.compare", fail_comparison)
    directory = tmp_path / "failed"
    with pytest.raises(ValueError, match="injected"):
        run_matrix(BASE, directory, progress=lambda _: None)
    assert (directory / "manifest.json").exists()
    assert not (directory / "matrix_summary.json").exists()
    failure = json.loads((directory / "failure.json").read_text())
    assert failure["status"] == "failed"
    assert failure["completed_cases"] == 0
