from copy import deepcopy
import csv
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from evaluation.experiments.analyze_inheritance_results import (
    audit_matrix, generate_report, markdown_report, number, recompute_pair,
    summarize,
)
from evaluation.experiments.run_inheritance_matrix import MatrixConfig, run_matrix


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    path = tmp_path_factory.mktemp("analysis-input") / "matrix"
    run_matrix(MatrixConfig(run_id="analysis-test", total_events=44, seeds=(-7, 102), repetitions=1),
               path, progress=lambda _: None)
    return path


def table(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def save_table(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_valid_matrix_and_negative_seed(baseline):
    result = audit_matrix(baseline)
    assert result["audit"]["audit_status"] == "passed"
    assert result["audit"]["case_count"] == 16
    assert result["audit"]["paired_site_results"] == 32
    assert result["audit"]["condition_site_groups"] == 16
    assert result["audit"]["off_unresolved_events_total"] == 0
    assert result["audit"]["depth0_control_failures"] == []
    assert set(p["seed"] for p in result["pairs"]) == {-7, 102}
    assert len(result["audit"]["input_sha256"]) == 5


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "", None, True])
def test_invalid_numbers(value):
    with pytest.raises(ValueError):
        number(value)


def known_modes():
    rows = []
    for mode, urgent, routine in (("off", 10, 20), ("on", 4, 22)):
        rows.append({
            "case_id": "demo", "condition": "ratio1-1_depth3_balanced", "seed": "9",
            "site_id": "Hospital_A", "inheritance": mode, "event_count": "24",
            "completed_events": "24", "unresolved_events": "0", "workflow_count": "6",
            "urgent_workflow_count": "3", "urgent_completed_workflows": "3", "urgent_unresolved_workflows": "0",
            "routine_workflow_count": "3", "routine_completed_workflows": "3", "routine_unresolved_workflows": "0",
            "urgent_completion_position_p95": str(urgent),
            "routine_completion_position_p95": str(routine), "scheduler_call_cpu_ms_median": "1.2",
        })
    return rows


def test_independent_numeric_oracle_and_incomplete_exclusion():
    off, on = known_modes()
    before = deepcopy((off, on))
    pair = recompute_pair(off, on)
    assert pair["urgent_p95_position_reduction"] == 6
    assert pair["urgent_p95_position_reduction_pct"] == 60
    assert pair["routine_p95_position_reduction"] == -2
    assert pair["routine_p95_position_reduction_pct"] == -10
    assert (off, on) == before
    on.update(completed_events="23", unresolved_events="1",
              urgent_completed_workflows="2", urgent_unresolved_workflows="1")
    partial = recompute_pair(off, on)
    assert not partial["urgent_pair_eligible"]
    assert partial["urgent_p95_position_reduction"] is None
    summary = summarize([partial])[0]
    assert summary["urgent_incomplete_pairs"] == 1
    assert summary["urgent_mean_p95_position_reduction"] is None


@pytest.mark.parametrize("damage", ["paired_number", "paired_nan", "duplicate_pair", "missing_mode",
                                    "duplicate_mode", "stale_summary", "incomplete_marker", "failure_marker",
                                    "bad_event_total", "bad_population"])
def test_corrupt_or_partial_results_rejected(baseline, tmp_path, damage):
    path = tmp_path / "copy"
    shutil.copytree(baseline, path)
    if damage.startswith("paired") or damage == "duplicate_pair":
        file = path / "paired_differences.csv"
        rows = table(file)
        if damage == "duplicate_pair":
            rows.append(rows[0].copy())
        else:
            rows[0]["urgent_p95_position_reduction"] = "nan" if damage == "paired_nan" else "9999"
        save_table(file, rows)
    elif damage in ("missing_mode", "duplicate_mode", "bad_event_total", "bad_population"):
        file = path / "per_seed_metrics.csv"
        rows = table(file)
        if damage == "missing_mode":
            rows.pop()
        elif damage == "duplicate_mode":
            rows.append(rows[0].copy())
        elif damage == "bad_event_total":
            rows[0]["completed_events"] = "999"
        else:
            rows[0]["urgent_completed_workflows"] = "999"
        save_table(file, rows)
    elif damage == "stale_summary":
        file = path / "condition_summary.csv"
        rows = table(file)
        rows[0]["routine_mean_p95_position_reduction"] = "999"
        save_table(file, rows)
    elif damage == "incomplete_marker":
        file = path / "matrix_summary.json"
        data = json.loads(file.read_text())
        data["status"] = "running"
        file.write_text(json.dumps(data))
    else:
        (path / "failure.json").write_text('{}')
    with pytest.raises(ValueError):
        audit_matrix(path)


def test_depth0_failure_remains_visible(baseline, tmp_path):
    path = tmp_path / "control"
    shutil.copytree(baseline, path)
    modes = table(path / "per_seed_metrics.csv")
    changed = next(row for row in modes if "depth0_balanced" in row["condition"]
                   and row["site_id"] == "Hospital_A" and row["inheritance"] == "on")
    changed["urgent_completion_position_p95"] = str(int(changed["urgent_completion_position_p95"]) + 1)
    save_table(path / "per_seed_metrics.csv", modes)
    lookup = {(r["case_id"], r["site_id"], r["inheritance"]): r for r in modes}
    pairs = [recompute_pair(lookup[(case, site, "off")], lookup[(case, site, "on")])
             for case, site, mode in lookup if mode == "off"]
    save_table(path / "paired_differences.csv", pairs)
    groups = summarize(pairs)
    save_table(path / "condition_summary.csv", groups)
    completion = json.loads((path / "matrix_summary.json").read_text())
    completion["condition_site_groups"] = groups
    (path / "matrix_summary.json").write_text(json.dumps(completion))
    analysis = audit_matrix(path)
    assert analysis["audit"]["audit_status"] == "control_failed"
    assert len(analysis["audit"]["depth0_control_failures"]) == 1
    assert "Control failures requiring investigation" in markdown_report(analysis)


def test_report_and_figures_preserve_input_and_refuse_overwrite(baseline, tmp_path):
    original = {p: p.read_bytes() for p in baseline.glob('*') if p.is_file()}
    output = tmp_path / "report"
    result = generate_report(baseline, output)
    assert result["audit_status"] == "passed"
    for name in ("urgent_position_reduction", "routine_position_change", "scheduler_cpu_ratio"):
        for extension in ("png", "svg", "pdf"):
            assert (output / f"{name}.{extension}").stat().st_size > 2000
    report = (output / "results_report.md").read_text()
    assert "undefined" in report
    assert "not a confidence interval" in report or "not confidence intervals" in report
    assert (output / "analysis_audit.json").exists()
    assert all(path.read_bytes() == data for path, data in original.items())
    with pytest.raises(ValueError, match="already exists"):
        generate_report(baseline, output)


def test_cli_audit_only_and_missing_inputs(baseline, tmp_path):
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-m", "evaluation.experiments.analyze_inheritance_results",
               "--matrix-dir", str(baseline), "--audit-only"]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["audit_status"] == "passed"
    result = subprocess.run([*command[:-2], str(tmp_path), "--audit-only"], cwd=root,
                            capture_output=True, text=True)
    assert result.returncode != 0
