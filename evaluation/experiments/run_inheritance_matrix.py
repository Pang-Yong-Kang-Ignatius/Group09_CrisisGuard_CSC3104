"""Run paired local inheritance comparisons across conditions and workload seeds."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys

from evaluation.experiments.compare_inheritance import compare, write_csv
from evaluation.workloads.generate_workload import (
    WorkloadConfig, generate_workload, serialize,
)


@dataclass(frozen=True)
class MatrixConfig:
    run_id: str
    seeds: tuple[int, ...] = (3104, 3105, 3106, 3107, 3108)
    total_events: int = 440
    repetitions: int = 3
    urgent_fraction: float = 0.2
    snapshot_at: str = "2026-10-21T00:00:00+00:00"
    ageing_interval_seconds: float = 600


def build_cases(config: MatrixConfig) -> list[dict]:
    if not config.seeds or any(type(seed) is not int for seed in config.seeds):
        raise ValueError("provide at least one integer seed")
    if len(set(config.seeds)) != len(config.seeds):
        raise ValueError("seeds must be unique; repetitions are configured separately")
    if type(config.repetitions) is not int or config.repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    if not math.isfinite(config.ageing_interval_seconds) or config.ageing_interval_seconds <= 0:
        raise ValueError("ageing interval must be finite and positive")
    cases = []
    for ratio in ((1, 1), (10, 1)):
        for depth in (0, 3):
            for distribution in ("balanced", "a_only"):
                condition = f"ratio{ratio[0]}-{ratio[1]}_depth{depth}_{distribution}"
                for seed in config.seeds:
                    cases.append({
                        "case_id": f"{condition}_seed{seed}",
                        "condition": condition,
                        "seed": seed,
                        "config": WorkloadConfig(
                            run_id=config.run_id, seed=seed,
                            total_events=config.total_events, backlog_ratio=ratio,
                            dependency_depth=depth, urgent_fraction=config.urgent_fraction,
                            urgent_distribution=distribution, snapshot_at=config.snapshot_at,
                        ),
                    })
    return cases


def paired_rows(summaries: list[dict], case: dict) -> list[dict]:
    rows = []
    for site in sorted({summary["site_id"] for summary in summaries}):
        modes = {row["inheritance"]: row for row in summaries if row["site_id"] == site}
        off, on = modes["off"], modes["on"]
        row = {
            "case_id": case["case_id"], "condition": case["condition"],
            "seed": case["seed"], "site_id": site,
            "event_count": off["event_count"],
            "off_unresolved_events": off["unresolved_events"],
            "on_unresolved_events": on["unresolved_events"],
        }
        for label in ("urgent", "routine"):
            if off[f"{label}_workflow_count"] != on[f"{label}_workflow_count"]:
                raise ValueError("paired policies have different original workflow populations")
            population = off[f"{label}_workflow_count"]
            row[f"{label}_workflow_count"] = population
            row[f"{label}_off_unresolved"] = off[f"{label}_unresolved_workflows"]
            row[f"{label}_on_unresolved"] = on[f"{label}_unresolved_workflows"]
            off_value = off[f"{label}_completion_position_p95"]
            on_value = on[f"{label}_completion_position_p95"]
            row[f"{label}_off_p95_position"] = off_value
            row[f"{label}_on_p95_position"] = on_value
            # Do not compare partial populations and reward a policy that leaves work unfinished.
            complete_pair = (population > 0 and row[f"{label}_off_unresolved"] == 0
                             and row[f"{label}_on_unresolved"] == 0
                             and off_value is not None and on_value is not None)
            row[f"{label}_pair_eligible"] = complete_pair
            row[f"{label}_p95_position_reduction"] = off_value - on_value if complete_pair else None
            row[f"{label}_p95_position_reduction_pct"] = (
                100 * (off_value - on_value) / off_value if complete_pair and off_value > 0 else None
            )
        row["off_scheduler_cpu_ms"] = off["scheduler_call_cpu_ms_median"]
        row["on_scheduler_cpu_ms"] = on["scheduler_call_cpu_ms_median"]
        rows.append(row)
    return rows


def aggregate_pairs(pairs: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for pair in pairs:
        groups[(pair["condition"], pair["site_id"])].append(pair)
    output = []
    for (condition, site), group in sorted(groups.items()):
        if len({row["seed"] for row in group}) != len(group):
            raise ValueError("duplicate seed in a condition/site group")
        row = {
            "condition": condition, "site_id": site, "seed_count": len(group),
            "off_unresolved_events_total": sum(p["off_unresolved_events"] for p in group),
            "on_unresolved_events_total": sum(p["on_unresolved_events"] for p in group),
        }
        for label in ("urgent", "routine"):
            valid = [p for p in group if p[f"{label}_pair_eligible"]]
            deltas = [p[f"{label}_p95_position_reduction"] for p in valid]
            percentages = [p[f"{label}_p95_position_reduction_pct"] for p in valid]
            row.update({
                f"{label}_eligible_seed_pairs": len(valid),
                f"{label}_empty_population_pairs": sum(p[f"{label}_workflow_count"] == 0 for p in group),
                f"{label}_incomplete_pairs": sum(p[f"{label}_workflow_count"] > 0 and not p[f"{label}_pair_eligible"] for p in group),
                f"{label}_mean_p95_position_reduction": statistics.mean(deltas) if deltas else None,
                f"{label}_median_p95_position_reduction": statistics.median(deltas) if deltas else None,
                f"{label}_min_p95_position_reduction": min(deltas) if deltas else None,
                f"{label}_max_p95_position_reduction": max(deltas) if deltas else None,
                f"{label}_mean_p95_position_reduction_pct": statistics.mean(percentages) if percentages else None,
                f"{label}_improved_pairs": sum(d > 0 for d in deltas),
                f"{label}_unchanged_pairs": sum(d == 0 for d in deltas),
                f"{label}_worsened_pairs": sum(d < 0 for d in deltas),
            })
        output.append(row)
    return output


def write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def source_hashes() -> dict:
    root = Path(__file__).resolve().parents[2]
    files = [Path(__file__).resolve(),
             root / "evaluation/experiments/compare_inheritance.py",
             root / "evaluation/workloads/generate_workload.py"]
    files.extend(sorted((root / "edge/scheduler").glob("*.py")))
    return {str(path.relative_to(root)).replace("\\", "/"):
            hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def run_matrix(config: MatrixConfig, output_dir: Path, progress=print) -> dict:
    cases = build_cases(config)
    if output_dir.exists():
        raise ValueError("output directory already exists; use a new directory")
    # Validate every workload before creating result files, including extreme inputs.
    for case in cases:
        generate_workload(case["config"])
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1, "status": "running", "matrix_config": asdict(config),
        "case_count": len(cases), "condition_count": 8,
        "comparison": "paired_local_inheritance_off_vs_on",
        "source_file_sha256": source_hashes(),
        "environment": {"python": sys.version, "platform": platform.platform()},
        "limitations": [
            "Each site drains independently; backlog skew does not create cross-site contention.",
            "Positions count selections, not seconds; acknowledgement is simulated immediate success.",
            "Time is frozen at the snapshot; live traffic, grants and adaptive admission are absent.",
            "Seed variation changes urgency placement and routine priorities; topology stays in the chain family.",
            "Urgency is a fraction of workflows; depth changes workflow and urgent-terminal counts.",
            "Seed min/max is observed spread, not a confidence interval or significance test.",
            "Aggregates average paired per-seed p95 differences, not a pooled event p95.",
        ],
    }
    write_json(output_dir / "manifest.json", manifest)
    summaries, pairs = [], []
    try:
        for index, case in enumerate(cases, start=1):
            workload = generate_workload(case["config"])
            report, events, workflows = compare(workload, config.repetitions,
                                                config.ageing_interval_seconds)
            directory = output_dir / "runs" / case["case_id"]
            directory.mkdir(parents=True, exist_ok=False)
            with (directory / "workload.json").open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(serialize(workload))
            write_json(directory / "summary.json", report)
            write_csv(directory / "events.csv", events)
            write_csv(directory / "workflows.csv", workflows)
            write_csv(directory / "timing_samples.csv", report["timing_samples"])
            summaries.extend({"case_id": case["case_id"], "condition": case["condition"],
                              "seed": case["seed"], **row} for row in report["summary"])
            pairs.extend(paired_rows(report["summary"], case))
            progress(f"[{index}/{len(cases)}] {case['case_id']}")
        aggregates = aggregate_pairs(pairs)
        write_csv(output_dir / "per_seed_metrics.csv", summaries)
        write_csv(output_dir / "paired_differences.csv", pairs)
        write_csv(output_dir / "condition_summary.csv", aggregates)
        result = {
            "status": "completed", "case_count": len(cases), "seed_count": len(config.seeds),
            "condition_site_groups": aggregates,
            "off_unresolved_events_total": sum(p["off_unresolved_events"] for p in pairs),
            "on_unresolved_events_total": sum(p["on_unresolved_events"] for p in pairs),
        }
        write_json(output_dir / "matrix_summary.json", result)
    except Exception as error:
        write_json(output_dir / "failure.json", {
            "status": "failed", "error_type": type(error).__name__, "error": str(error),
            "completed_cases": len(pairs) // 2,
            "note": "Partial artifacts retained; use a new directory for another attempt.",
        })
        raise
    # Presence of matrix_summary.json is the successful completion marker.
    progress("Completed. See condition_summary.csv and matrix_summary.json.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seeds", type=int, nargs="+", default=[3104, 3105, 3106, 3107, 3108])
    parser.add_argument("--total-events", type=int, default=440)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--urgent-fraction", type=float, default=0.2)
    parser.add_argument("--snapshot-at", default="2026-10-21T00:00:00+00:00")
    parser.add_argument("--ageing-interval", type=float, default=600)
    args = parser.parse_args()
    try:
        config = MatrixConfig(args.run_id, tuple(args.seeds), args.total_events,
                              args.repetitions, args.urgent_fraction, args.snapshot_at,
                              args.ageing_interval)
        print("LOCAL SCHEDULER MATRIX: positions are counts, not recovery seconds.", flush=True)
        result = run_matrix(config, args.output_dir,
                            progress=lambda message: print(message, flush=True))
        print("Cases:", result["case_count"])
        print("Unresolved events OFF/ON:", result["off_unresolved_events_total"],
              result["on_unresolved_events_total"])
        print("Saved:", args.output_dir)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
