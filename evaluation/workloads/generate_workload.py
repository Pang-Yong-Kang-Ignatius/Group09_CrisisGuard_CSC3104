"""Generate offline synthetic event backlogs without starting services or touching DBs."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import uuid


SITES = ("Hospital_A", "Hospital_B")
CAPTURE_FIELDS = (
    "event_id", "site_id", "event_type", "base_priority", "prerequisite_ids", "payload"
)


@dataclass(frozen=True)
class WorkloadConfig:
    run_id: str
    seed: int = 3104
    total_events: int = 1000
    backlog_ratio: tuple[int, int] = (1, 1)
    dependency_depth: int = 3
    urgent_fraction: float = 0.2
    urgent_distribution: str = "balanced"
    snapshot_at: str = "2026-10-21T00:00:00+00:00"
    capture_interval_seconds: float = 1.0


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("snapshot_at must include a timezone, e.g. +00:00")
    return parsed.astimezone(timezone.utc)


def capture_request(event: dict) -> dict:
    """Project a scheduler record onto the current six-field capture/cloud contract."""
    return {key: event[key] for key in CAPTURE_FIELDS}


def apportion(total: int, weights: tuple[int, int]) -> tuple[int, int]:
    """Largest remainder allocation; Hospital A wins exact rounding ties."""
    denominator = sum(weights)
    counts = [total * weight // denominator for weight in weights]
    order = sorted(range(2), key=lambda i: (-(total * weights[i] % denominator), i))
    for index in order[: total - sum(counts)]:
        counts[index] += 1
    return tuple(counts)


def generate_workload(config: WorkloadConfig) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config.run_id):
        raise ValueError("run_id must contain 1-64 letters, digits, underscores or hyphens")
    if config.total_events < 2:
        raise ValueError("total_events must be at least 2")
    if len(config.backlog_ratio) != 2 or any(w <= 0 for w in config.backlog_ratio):
        raise ValueError("backlog_ratio requires two positive integer weights")
    if config.dependency_depth < 0:
        raise ValueError("dependency_depth must be nonnegative (number of edges)")
    if not math.isfinite(config.urgent_fraction) or not 0 <= config.urgent_fraction <= 1:
        raise ValueError("urgent_fraction must be between 0 and 1")
    if config.urgent_distribution not in ("balanced", "a_only"):
        raise ValueError("urgent_distribution must be balanced or a_only")
    if not math.isfinite(config.capture_interval_seconds) or config.capture_interval_seconds < 0:
        raise ValueError("capture_interval_seconds must be finite and nonnegative")
    snapshot = parse_time(config.snapshot_at)
    counts = apportion(config.total_events, config.backlog_ratio)
    if min(counts) == 0:
        raise ValueError("total_events is too small to give both hospitals a backlog")
    length = config.dependency_depth + 1
    workflow_counts = tuple((count + length - 1) // length for count in counts)
    urgent_total = math.floor(sum(workflow_counts) * config.urgent_fraction + 0.5)
    if config.urgent_distribution == "a_only":
        if urgent_total > workflow_counts[0]:
            raise ValueError("Hospital A has too few workflows for this urgent_fraction")
        urgent_counts = (urgent_total, 0)
    else:
        urgent_counts = apportion(urgent_total, workflow_counts)

    normalized = asdict(config)
    normalized["snapshot_at"] = snapshot.isoformat()
    # Include every workload parameter, but no policy, machine clock or output path.
    identity = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, "crisisguard-workload-v1:" + identity)
    rng = random.Random(config.seed)
    events, workflows = [], []
    for site_index, site in enumerate(SITES):
        urgent_indices = set(rng.sample(range(workflow_counts[site_index]), urgent_counts[site_index]))
        captured = 0
        for workflow_index in range(workflow_counts[site_index]):
            workflow_id = str(uuid.uuid5(namespace, f"{site}:workflow:{workflow_index}"))
            urgent = workflow_index in urgent_indices
            size = min(length, counts[site_index] - captured)
            ids = [str(uuid.uuid5(namespace, f"{workflow_id}:event:{i}")) for i in range(size)]
            for step, event_id in enumerate(ids):
                terminal = step == size - 1
                if terminal and urgent:
                    event_type = "emergency_record"
                elif step == 0:
                    event_type = "patient_registration"
                elif terminal:
                    event_type = "patient_discharge"
                else:
                    event_type = "encounter_creation"
                # Urgent prerequisites stay routine; inheritance must supply their boost.
                priority = 5 if terminal and urgent else rng.randint(1, 3)
                created_at = snapshot - timedelta(
                    seconds=(counts[site_index] - captured) * config.capture_interval_seconds
                )
                events.append({
                    "event_id": event_id,
                    "site_id": site,
                    "event_type": event_type,
                    "base_priority": priority,
                    "prerequisite_ids": [ids[step - 1]] if step else [],
                    "payload": {
                        "synthetic": True,
                        "patient_id": "SYNTHETIC_" + workflow_id,
                        "workflow_id": workflow_id,
                        "step": step,
                    },
                    "status": "pending",
                    "created_at": created_at.isoformat(),
                })
                captured += 1
            workflows.append({
                "workflow_id": workflow_id,
                "site_id": site,
                "urgent": urgent,
                "dependency_depth": size - 1,
                "required_event_ids": ids,
                "completion_event_id": ids[-1],
            })
    summary = {
        "event_count": len(events),
        "workflow_count": len(workflows),
        "urgent_workflow_count": urgent_total,
        "events_by_site": dict(zip(SITES, counts)),
        "workflows_by_site": dict(zip(SITES, workflow_counts)),
        "urgent_workflows_by_site": dict(zip(SITES, urgent_counts)),
        "workflow_depth_counts": dict(sorted(Counter(str(w["dependency_depth"]) for w in workflows).items())),
    }
    bundle = {
        "schema_version": 1,
        "generator_version": "1.0.0",
        "config": normalized,
        "summary": summary,
        "events": events,
        "workflows": workflows,
    }
    validate_workload(bundle)
    return bundle


def validate_workload(bundle: dict) -> None:
    """Check event/graph contracts independently of the generator's selection logic."""
    events = bundle["events"]
    by_id = {e["event_id"]: e for e in events}
    if len(by_id) != len(events):
        raise ValueError("duplicate event IDs")
    if len(events) != bundle["config"]["total_events"]:
        raise ValueError("event count does not match configuration")
    snapshot = parse_time(bundle["config"]["snapshot_at"])
    children = {event_id: [] for event_id in by_id}
    incoming = {}
    for event in events:
        if event["site_id"] not in SITES or event["status"] != "pending":
            raise ValueError("invalid hospital or initial status")
        priority = event["base_priority"]
        if type(priority) is not int or not 1 <= priority <= 5:
            raise ValueError("priority must be an integer in 1-5")
        if parse_time(event["created_at"]) > snapshot:
            raise ValueError("event captured after snapshot")
        allowed = {"patient_registration", "encounter_creation", "emergency_record", "patient_discharge"}
        if event["event_type"] not in allowed:
            raise ValueError("unsupported event type")
        incoming[event["event_id"]] = len(event["prerequisite_ids"])
        for prerequisite in event["prerequisite_ids"]:
            if prerequisite not in by_id:
                raise ValueError("missing prerequisite")
            parent = by_id[prerequisite]
            if parent["site_id"] != event["site_id"]:
                raise ValueError("cross-site prerequisite")
            if parse_time(parent["created_at"]) > parse_time(event["created_at"]):
                raise ValueError("prerequisite captured after dependent event")
            children[prerequisite].append(event["event_id"])
    ready = [key for key, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        event_id = ready.pop()
        visited += 1
        for child in children[event_id]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    if visited != len(events):
        raise ValueError("dependency cycle")
    covered, workflow_ids = [], set()
    for workflow in bundle["workflows"]:
        ids = workflow["required_event_ids"]
        if workflow["workflow_id"] in workflow_ids or not ids:
            raise ValueError("duplicate or empty workflow")
        workflow_ids.add(workflow["workflow_id"])
        if workflow["completion_event_id"] != ids[-1] or workflow["dependency_depth"] != len(ids) - 1:
            raise ValueError("invalid workflow completion or depth")
        for step, event_id in enumerate(ids):
            if event_id not in by_id:
                raise ValueError("workflow references unknown event")
            event = by_id[event_id]
            if event["site_id"] != workflow["site_id"] or event["payload"]["workflow_id"] != workflow["workflow_id"]:
                raise ValueError("workflow ownership mismatch")
            expected = [ids[step - 1]] if step else []
            if event["prerequisite_ids"] != expected:
                raise ValueError("workflow is not the declared chain")
        if workflow["urgent"] and by_id[ids[-1]]["base_priority"] != 5:
            raise ValueError("urgent terminal must have priority 5")
        covered.extend(ids)
    if len(covered) != len(set(covered)) or set(covered) != set(by_id):
        raise ValueError("workflows must partition the original backlog")


def serialize(bundle: dict) -> str:
    return json.dumps(bundle, indent=2, sort_keys=True, allow_nan=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="Reuse across paired policies; change for independent captures")
    parser.add_argument("--seed", type=int, default=3104)
    parser.add_argument("--total-events", type=int, default=1000)
    parser.add_argument("--ratio", type=int, nargs=2, default=(1, 1), metavar=("A", "B"))
    parser.add_argument("--depth", type=int, default=3, help="Maximum edges per workflow; short remainder chains are reported")
    parser.add_argument("--urgent-fraction", type=float, default=0.2, help="Fraction of workflows, rounded half up")
    parser.add_argument("--urgent-distribution", choices=("balanced", "a_only"), default="balanced")
    parser.add_argument("--snapshot-at", default="2026-10-21T00:00:00+00:00", help="Fixed simulation reference time with timezone")
    parser.add_argument("--capture-interval", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        bundle = generate_workload(WorkloadConfig(
            run_id=args.run_id, seed=args.seed, total_events=args.total_events,
            backlog_ratio=tuple(args.ratio), dependency_depth=args.depth,
            urgent_fraction=args.urgent_fraction, urgent_distribution=args.urgent_distribution,
            snapshot_at=args.snapshot_at, capture_interval_seconds=args.capture_interval,
        ))
        content = serialize(bundle)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Never silently replace a workload used by a previous experiment.
        with args.output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except (ValueError, OSError, OverflowError) as error:
        parser.error(str(error))
    print(json.dumps(bundle["summary"], indent=2))
    print("SHA256:", hashlib.sha256(content.encode("utf-8")).hexdigest())
    print("Saved:", args.output)


if __name__ == "__main__":
    main()
