"""Compare local scheduler inheritance ON/OFF using a fixed workload snapshot."""

from __future__ import annotations

import argparse
from copy import deepcopy
import csv
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys
from time import perf_counter_ns, process_time_ns

from edge.scheduler.scheduler import select_next_event
from evaluation.workloads.generate_workload import parse_time, validate_workload


def percentile(values: list[int], fraction: float = 0.95) -> int | None:
    """Nearest-rank percentile; empty populations are represented as null."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def canonical_hash(workload: dict) -> str:
    """Hash JSON values, independent of indentation and LF/CRLF conversion."""
    encoded = json.dumps(workload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def simulate_site(workload: dict, site: str, inheritance: bool,
                  ageing_interval_seconds: float = 600) -> tuple[dict, list[dict], list[dict]]:
    """Acknowledge each selection immediately; positions start at 1 per hospital."""
    if not math.isfinite(ageing_interval_seconds) or ageing_interval_seconds <= 0:
        raise ValueError("ageing_interval_seconds must be finite and positive")
    events = deepcopy([event for event in workload["events"] if event["site_id"] == site])
    workflows = [workflow for workflow in workload["workflows"] if workflow["site_id"] == site]
    now = parse_time(workload["config"]["snapshot_at"])
    by_id = {event["event_id"]: event for event in events}
    applied, positions = set(), {}
    cpu_ns = wall_ns = calls = 0
    while len(applied) < len(events):
        cpu_start, wall_start = process_time_ns(), perf_counter_ns()
        selected = select_next_event(
            events, applied_ids=applied, now=now,
            ageing_interval_seconds=ageing_interval_seconds,
            enable_inheritance=inheritance,
        )
        wall_ns += perf_counter_ns() - wall_start
        cpu_ns += process_time_ns() - cpu_start
        calls += 1
        if selected is None:
            break
        event_id = selected["event_id"]
        if event_id not in by_id or event_id in applied:
            raise ValueError("scheduler returned an unknown or already applied event")
        if not set(by_id[event_id]["prerequisite_ids"]) <= applied:
            raise ValueError("scheduler violated prerequisite ordering")
        positions[event_id] = len(applied) + 1
        applied.add(event_id)

    event_rows = []
    for event in events:
        position = positions.get(event["event_id"])
        event_rows.append({
            "site_id": site, "inheritance": "on" if inheritance else "off",
            "event_id": event["event_id"], "workflow_id": event["payload"]["workflow_id"],
            "base_priority": event["base_priority"], "completed": position is not None,
            "completion_position": position,
            "preceding_selections": position - 1 if position is not None else None,
        })
    workflow_rows = []
    for workflow in workflows:
        ids = workflow["required_event_ids"]
        completed = all(event_id in applied for event_id in ids)
        workflow_rows.append({
            "site_id": site, "inheritance": "on" if inheritance else "off",
            "workflow_id": workflow["workflow_id"], "urgent": workflow["urgent"],
            "completed": completed,
            "completion_position": max(positions[i] for i in ids) if completed else None,
            "completion_event_id": workflow["completion_event_id"],
        })
    summary = {
        "site_id": site, "inheritance": "on" if inheritance else "off",
        "event_count": len(events), "completed_events": len(applied),
        "unresolved_events": len(events) - len(applied),
        "workflow_count": len(workflows),
        "scheduler_calls": calls,
        "scheduler_call_cpu_ms": cpu_ns / 1_000_000,
        "scheduler_call_wall_ms": wall_ns / 1_000_000,
    }
    for urgent, label in ((True, "urgent"), (False, "routine")):
        population = [row for row in workflow_rows if row["urgent"] == urgent]
        completed_positions = [row["completion_position"] for row in population if row["completed"]]
        summary.update({
            f"{label}_workflow_count": len(population),
            f"{label}_completed_workflows": len(completed_positions),
            f"{label}_unresolved_workflows": len(population) - len(completed_positions),
            f"{label}_completion_position_p50": statistics.median(completed_positions) if completed_positions else None,
            f"{label}_completion_position_p95": percentile(completed_positions),
        })
    # This includes routine prerequisites of urgent workflows; label explicitly.
    routine_base_waits = [row["preceding_selections"] for row in event_rows
                          if row["base_priority"] < 5 and row["completed"]]
    summary["routine_base_event_preceding_selections_p95"] = percentile(routine_base_waits)
    return summary, event_rows, workflow_rows


def compare(workload: dict, repetitions: int = 5,
            ageing_interval_seconds: float = 600) -> tuple[dict, list[dict], list[dict]]:
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    validate_workload(workload)
    sites = sorted({event["site_id"] for event in workload["events"]})
    samples, event_rows, workflow_rows, reference_orders = [], [], [], {}
    for repetition in range(repetitions):
        # Alternate execution order to reduce consistent first/second timing bias.
        modes = (False, True) if repetition % 2 == 0 else (True, False)
        for site in sites:
            for inheritance in modes:
                summary, rows, workflows = simulate_site(workload, site, inheritance,
                                                         ageing_interval_seconds)
                samples.append({"repetition": repetition + 1, **summary})
                key = (site, inheritance)
                order = tuple((row["event_id"], row["completion_position"]) for row in rows)
                if key in reference_orders and reference_orders[key] != order:
                    raise ValueError("selection order changed across identical repetitions")
                reference_orders[key] = order
                if repetition == 0:
                    event_rows.extend(rows)
                    workflow_rows.extend(workflows)
    aggregates = []
    for site in sites:
        for mode in ("off", "on"):
            group = [sample for sample in samples if sample["site_id"] == site and sample["inheritance"] == mode]
            row = {key: value for key, value in group[0].items()
                   if key not in ("repetition", "scheduler_call_cpu_ms", "scheduler_call_wall_ms")}
            row["scheduler_call_cpu_ms_median"] = statistics.median(s["scheduler_call_cpu_ms"] for s in group)
            row["scheduler_call_wall_ms_median"] = statistics.median(s["scheduler_call_wall_ms"] for s in group)
            aggregates.append(row)
    report = {
        "schema_version": 1,
        "experiment": "local_inheritance_snapshot_comparison",
        "workload_canonical_sha256": canonical_hash(workload),
        "workload_config": workload["config"],
        "environment": {"python": sys.version, "platform": platform.platform()},
        "settings": {
            "repetitions": repetitions,
            "ageing_interval_seconds": ageing_interval_seconds,
            "snapshot_at": workload["config"]["snapshot_at"],
            "acknowledgement": "immediate_simulated_success",
            "site_coordination": "none_each_site_drained_independently",
            "percentile": "nearest_rank_p95_median_p50",
        },
        "limitations": [
            "Positions are per-site selection counts, not elapsed recovery time.",
            "Ageing time is fixed at the snapshot throughout each drain.",
            "No HTTP, databases, controller, live traffic, failures or event costs are simulated.",
            "Timing measures scheduler calls only and varies by environment.",
            "Repetitions reuse one workload; they are not independent workload seeds.",
            "Percentiles use completed work only; unresolved counts must be reported alongside them.",
        ],
        "summary": aggregates,
        "timing_samples": samples,
    }
    return report, event_rows, workflow_rows


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise ValueError("cannot write empty result table")
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="Must be a new directory")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--ageing-interval", type=float, default=600)
    args = parser.parse_args()
    try:
        workload = json.loads(args.workload.read_text(encoding="utf-8"))
        if args.output_dir.exists():
            raise ValueError("output directory already exists; choose a new result directory")
        report, events, workflows = compare(workload, args.repetitions, args.ageing_interval)
        args.output_dir.mkdir(parents=True, exist_ok=False)
        with (args.output_dir / "summary.json").open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        write_csv(args.output_dir / "summary.csv", report["summary"])
        write_csv(args.output_dir / "events.csv", events)
        write_csv(args.output_dir / "workflows.csv", workflows)
        write_csv(args.output_dir / "timing_samples.csv", report["timing_samples"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        parser.error(str(error))
    print("LOCAL SCHEDULER SIMULATION: positions are counts, not seconds.")
    for row in report["summary"]:
        urgent = row["urgent_completion_position_p95"]
        routine = row["routine_completion_position_p95"]
        print(f"{row['site_id']} inheritance={row['inheritance']}: "
              f"urgent p95 position={urgent}; routine p95 position={routine}; "
              f"unresolved events={row['unresolved_events']}")
    print("Saved:", args.output_dir)


if __name__ == "__main__":
    main()
