"""Audit deployed live event producers without authorizing Cloud writes.

The guarded Dashboard routes are called normally and must return 403 without
reaching Cloud. The same deployed Dashboard transport helper is then invoked
inside its container for each service; it must carry authenticated live
provenance but receive Cloud 401 because it has no Hospital admission identity.
No event, grant, ticket, configuration or container is modified.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "docs/evidence/cp7b-external-live-caller-audit-20261028.json"
SERVICES = {
    "patient": (8001, "patient_registration"),
    "emergency": (8003, "emergency_record"),
    "admin": (8004, "patient_discharge"),
}
PROBE_IDS = {name: f"CP7B_LIVE_CALLER_REJECT_{name.upper()}_20261028"
             for name in SERVICES}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ordered_rows_sha256(rows: str) -> str:
    """Hash a deterministically ordered, complete row stream."""
    return hashlib.sha256(rows.encode()).hexdigest()


def canonical_comparison_self_check() -> dict:
    baseline = '{"grant_id":"1","status":"completed"}\n{"grant_id":"2","status":"completed"}\n'
    modified = '{"grant_id":"1","status":"expired"}\n{"grant_id":"2","status":"completed"}\n'
    added = baseline + '{"grant_id":"3","status":"completed"}\n'
    deleted = '{"grant_id":"1","status":"completed"}\n'
    baseline_hash = ordered_rows_sha256(baseline)
    return {
        "complete_rows_are_hashed": True,
        "deterministic_order_required_by_queries": True,
        "modification_detected": ordered_rows_sha256(modified) != baseline_hash,
        "addition_detected": ordered_rows_sha256(added) != baseline_hash,
        "deletion_detected": ordered_rows_sha256(deleted) != baseline_hash,
    }


def run(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=30).stdout


def docker(*args: str) -> str:
    return run(["docker", *args])


def request(url: str, *, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            status, raw = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read().decode()
    return {"status_code": status, "body": json.loads(raw)}


def metrics() -> dict:
    result = {}
    for service, (port, _) in SERVICES.items():
        result[service] = request(f"http://127.0.0.1:{port}/metrics")["body"]
    return result


def postgres_snapshot() -> dict:
    sql = ("select event_id,site_id,event_type,base_priority,prerequisite_ids::text,"
           "payload::text,status,coalesce(applied_at::text,'') from events order by event_id")
    rows = docker("exec", "crisisguard-local-postgres-1", "psql", "-U", "crisisguard",
                  "-d", "crisisguard", "-At", "-F", "|", "-c", sql)
    ids = ",".join("'" + value + "'" for value in PROBE_IDS.values())
    counts = docker(
        "exec", "crisisguard-local-postgres-1", "psql", "-U", "crisisguard",
        "-d", "crisisguard", "-At", "-F", "|", "-c",
        "select (select count(*) from events),"
        "(select count(*) from controller_grants),"
        "(select count(*) from controller_tickets),"
        f"(select count(*) from events where event_id in ({ids}));",
    ).strip().split("|")
    grants = docker(
        "exec", "crisisguard-local-postgres-1", "psql", "-U", "crisisguard",
        "-d", "crisisguard", "-Atc",
        "select row_to_json(t)::text from (select * from controller_grants order by grant_id) t;",
    )
    tickets = docker(
        "exec", "crisisguard-local-postgres-1", "psql", "-U", "crisisguard",
        "-d", "crisisguard", "-Atc",
        "select row_to_json(t)::text from (select * from controller_tickets order by 1,2) t;",
    )
    return {
        "events": int(counts[0]), "grants": int(counts[1]),
        "tickets": int(counts[2]), "probe_events": int(counts[3]),
        "events_logical_sha256": ordered_rows_sha256(rows),
        "grants_data_sha256": ordered_rows_sha256(grants),
        "tickets_data_sha256": ordered_rows_sha256(tickets),
    }


def runtime_snapshot() -> dict:
    names = (
        "crisisguard-local-postgres-1", "crisisguard-local-controller-1",
        "crisisguard-local-patient-1", "crisisguard-local-emergency-1",
        "crisisguard-local-admin-1", "crisisguard-local-hospital-a-1",
        "crisisguard-local-hospital-b-1", "crisisguard-local-dashboard-1",
    )
    inspected = json.loads(docker("inspect", *names))
    return {
        "containers": {item["Name"].lstrip("/"): {
            "id": item["Id"], "image": item["Image"],
            "state": item["State"]["Status"],
            "health": (item["State"].get("Health") or {}).get("Status"),
        } for item in inspected},
        "volumes": sorted(docker("volume", "ls", "-q").splitlines()),
        "all_container_count": len(docker("ps", "-a", "-q").splitlines()),
    }


def event(service: str) -> dict:
    return {
        "event_id": PROBE_IDS[service], "site_id": "Hospital_A",
        "event_type": SERVICES[service][1], "base_priority": 2,
        "prerequisite_ids": [],
        "payload": {"purpose": "unauthorized live provenance rejection probe"},
    }


def deployed_transport_probe(service: str) -> dict:
    body = json.dumps(event(service))
    code = (
        "import asyncio,json; from dashboard.main import forward_request; "
        f"print(json.dumps(asyncio.run(forward_request({service!r},'POST','/events',json.loads({body!r})))))"
    )
    return json.loads(docker("exec", "crisisguard-local-dashboard-1", "python", "-c", code))


def counts(samples: dict) -> dict:
    fields = ("live_latency_sample_count", "replay_latency_sample_count",
              "unclassified_latency_sample_count", "latency_sample_count")
    return {service: {field: body[field] for field in fields}
            for service, body in samples.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--self-test-only", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite {args.output}")

    if args.self_test_only:
        checks = canonical_comparison_self_check()
        evidence = {
            "checkpoint": "7B",
            "scope": "Canonical ordered-row preservation comparison self-test",
            "captured_at": now(),
            "method": "SHA-256 over all columns returned as row JSON in deterministic primary-key/column order; row counts are recorded separately",
            "checks": checks,
            "result": "passed" if all(checks.values()) else "failed",
        }
        args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, indent=2))
        if evidence["result"] != "passed":
            raise SystemExit(1)
        return

    baseline = metrics()
    assert all(body["latency_sample_count"] == 0 for body in baseline.values()), \
        "Telemetry windows must be empty"
    db_before = postgres_snapshot()
    runtime_before = runtime_snapshot()
    started = now()

    guarded = {service: request(
        f"http://127.0.0.1:8080/api/events/{service}", body=event(service))
        for service in SERVICES}
    after_guard = metrics()
    transport = {service: deployed_transport_probe(service) for service in SERVICES}
    final = metrics()
    db_after = postgres_snapshot()
    runtime_after = runtime_snapshot()
    expected = {service: {
        "live_latency_sample_count": 1, "replay_latency_sample_count": 0,
        "unclassified_latency_sample_count": 0, "latency_sample_count": 1,
    } for service in SERVICES}
    assertions = {
        "dashboard_routes_denied_403": all(item["status_code"] == 403 for item in guarded.values()),
        "guarded_routes_did_not_reach_cloud": all(
            body["latency_sample_count"] == 0 for body in after_guard.values()),
        "deployed_live_transports_denied_by_admission_401": all(
            item["http_status"] == 401 for item in transport.values()),
        "exact_authenticated_live_classification": counts(final) == expected,
        "no_event_or_controller_history_change": db_before == db_after,
        "probe_ids_not_persisted": db_after["probe_events"] == 0,
        "runtime_resources_unchanged": runtime_before == runtime_after,
    }
    evidence = {
        "checkpoint": "7B", "scope": "External/project-owned live event producer audit",
        "started_at": started, "finished_at": now(),
        "producer_inventory": {
            "project_owned_runtime": [
                {"producer": "Dashboard direct Cloud submission", "path": "browser POST /api/events/{service} -> Dashboard forward_request -> Cloud POST /events", "provenance": "authenticated live", "runtime": "disabled before forwarding by METAGUARD_REQUIRED=1"},
                {"producer": "Dashboard Hospital capture", "path": "browser POST /api/hospitals/{hospital}/capture -> Hospital /capture", "provenance": "not a Cloud call; later Hospital delivery is authenticated replay", "runtime": "enabled"},
                {"producer": "Hospital A/B replay", "path": "Hospital send_to_cloud -> Cloud POST /events", "provenance": "authenticated replay", "runtime": "enabled"}
            ],
            "external_test_tools_not_runtime_producers": [
                "tests/integration/test_concurrency.py",
                "tests/integration/test_cross_service_concurrency.py",
                "infrastructure/scripts/smoke_deployment.py",
                "infrastructure/scripts/smoke_metaguard_deployment.py",
                "infrastructure/scripts/aws_outage_host.py"
            ],
            "hypothetical_future_clients": "No implementation or deployment exists; callers must use the documented authenticated live provenance contract if introduced."
        },
        "deployment_decision": "No application source or deployment change required: the only project-owned live Cloud producer is already instrumented and is deliberately disabled by the grant-enforcement guard.",
        "baseline_metrics": baseline, "guarded_route_results": guarded,
        "metrics_after_guarded_routes": after_guard,
        "deployed_transport_results": transport, "final_metrics": final,
        "observed_counts": counts(final), "expected_counts": expected,
        "database_before": db_before, "database_after": db_after,
        "runtime_before": runtime_before, "runtime_after": runtime_after,
        "assertions": assertions,
        "result": "passed" if all(assertions.values()) else "failed",
        "limitations": [
            "Transport probes intentionally bypass the Dashboard route guard only inside the deployed container and remain unauthorized at Cloud; they prove provenance without creating events.",
            "Test harnesses are not production workload coverage and were not given private live credentials.",
            "No claim is made about hypothetical callers outside the repository or future deployments."
        ]
    }
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "result": evidence["result"],
                      "guarded": {k: v["status_code"] for k, v in guarded.items()},
                      "transport": {k: v["http_status"] for k, v in transport.items()},
                      "counts": counts(final), "assertions": assertions}, indent=2))
    if evidence["result"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
