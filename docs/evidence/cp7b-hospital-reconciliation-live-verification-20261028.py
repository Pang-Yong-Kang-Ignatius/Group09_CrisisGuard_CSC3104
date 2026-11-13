"""Verify deployed Hospital reconciliation GET provenance in a clean window.

Invokes the real deployed get_cloud_event_status function for existing applied
events only. No event submission, grant mutation or database write occurs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "docs/evidence/cp7b-hospital-reconciliation-live-20261028.json"
SERVICES = {
    "patient": "http://127.0.0.1:8001",
    "emergency": "http://127.0.0.1:8003",
    "admin": "http://127.0.0.1:8004",
}
PROBES = (
    ("hospital_a_patient", "crisisguard-local-hospital-a-1", "patient_registration",
     "CP7B_RESUME_20261028T123204Z_Hospital_A_stale"),
    ("hospital_b_patient", "crisisguard-local-hospital-b-1", "patient_registration",
     "CP7B_MAIN_20261027T144951_Hospital_B_2"),
    ("hospital_a_emergency", "crisisguard-local-hospital-a-1", "emergency_record",
     "DASH_0c768eef92d1413b866cbbeb45bffa25"),
    ("hospital_a_admin", "crisisguard-local-hospital-a-1", "patient_discharge",
     "DASH_e5d593df3dd444ffbc3d37e513d628df"),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def metrics() -> dict:
    result = {}
    for service, base in SERVICES.items():
        with urllib.request.urlopen(base + "/metrics", timeout=5) as response:
            result[service] = json.load(response)
    return result


def reconcile(container: str, event_type: str, event_id: str) -> dict:
    event = json.dumps({"event_type": event_type, "event_id": event_id})
    code = ("import json; from edge.capture_api.cloud_client import get_cloud_event_status as f; "
            f"print(json.dumps(f(json.loads({event!r}))))")
    completed = subprocess.run(
        ["docker", "exec", container, "python", "-c", code],
        check=True, capture_output=True, text=True, timeout=15,
    )
    return json.loads(completed.stdout)


def counts(samples: dict) -> dict:
    fields = ("live_latency_sample_count", "replay_latency_sample_count",
              "unclassified_latency_sample_count", "latency_sample_count")
    return {service: {field: body[field] for field in fields}
            for service, body in samples.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite {args.output}")
    before = metrics()
    assert all(body["latency_sample_count"] == 0 for body in before.values()), \
        "Telemetry windows must be empty"
    started = now()
    results = {name: reconcile(container, event_type, event_id)
               for name, container, event_type, event_id in PROBES}
    after = metrics()
    observed = counts(after)
    expected = {
        "patient": {"live_latency_sample_count": 0, "replay_latency_sample_count": 2,
                    "unclassified_latency_sample_count": 0, "latency_sample_count": 2},
        "emergency": {"live_latency_sample_count": 0, "replay_latency_sample_count": 1,
                      "unclassified_latency_sample_count": 0, "latency_sample_count": 1},
        "admin": {"live_latency_sample_count": 0, "replay_latency_sample_count": 1,
                  "unclassified_latency_sample_count": 0, "latency_sample_count": 1},
    }
    assertions = {
        "all_existing_events_return_applied": all(
            item.get("state") == "applied" for item in results.values()),
        "exact_replay_classification_no_live_or_unclassified": observed == expected,
    }
    evidence = {
        "checkpoint": "7B", "scope": "Deployed Hospital A/B reconciliation GET provenance",
        "started_at": started, "finished_at": now(),
        "method": "Real deployed get_cloud_event_status calls on existing applied IDs; read-only",
        "baseline_metrics": before, "reconciliation_results": results,
        "final_metrics": after, "observed_counts": observed, "expected_counts": expected,
        "assertions": assertions,
        "result": "passed" if all(assertions.values()) else "failed",
    }
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "result": evidence["result"],
                      "results": results, "counts": observed}, indent=2))
    if evidence["result"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
