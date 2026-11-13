"""Bounded deployed-caller provenance verification for Checkpoint 7B.

The experiment uses existing-event GETs and deliberately invalid-grant synthetic
POSTs. The POSTs must be rejected before persistence. It invokes the deployed
Hospital cloud client and the deployed Dashboard telemetry helper inside their
containers, never prints credentials, and refuses to overwrite evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "docs/evidence/cp7b-caller-provenance-verification-20261028.json"
CONTAINERS = {
    "hospital_a": "crisisguard-local-hospital-a-1",
    "hospital_b": "crisisguard-local-hospital-b-1",
    "dashboard": "crisisguard-local-dashboard-1",
    "postgres": "crisisguard-local-postgres-1",
}
SERVICES = {
    "patient": ("http://127.0.0.1:8001", "CP7B_RESUME_20261028T123204Z_Hospital_A_stale"),
    "emergency": ("http://127.0.0.1:8003", "DASH_0c768eef92d1413b866cbbeb45bffa25"),
    "admin": ("http://127.0.0.1:8004", "DASH_e5d593df3dd444ffbc3d37e513d628df"),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def docker_python(container: str, code: str) -> dict:
    result = subprocess.run(
        ["docker", "exec", container, "python", "-c", code],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return json.loads(result.stdout)


def docker_text(*args: str) -> str:
    result = subprocess.run(
        ["docker", "exec", *args], check=True, capture_output=True,
        text=True, timeout=30,
    )
    return result.stdout


def request_json(url: str, *, method: str = "GET", body: dict | None = None,
                 headers: dict[str, str] | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    started = now()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            raw = response.read().decode()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read().decode()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"text": raw[:500]}
    return {"captured_at": started, "status_code": status, "body": parsed}


def metrics() -> dict:
    return {name: request_json(base + "/metrics")["body"]
            for name, (base, _) in SERVICES.items()}


def postgres_snapshot() -> dict:
    sql = ("select event_id,site_id,event_type,base_priority,prerequisite_ids::text,"
           "payload::text,status,coalesce(applied_at::text,'') from events order by event_id")
    raw = docker_text(CONTAINERS["postgres"], "psql", "-U", "crisisguard", "-d",
                      "crisisguard", "-At", "-F", "|", "-c", sql)
    return {"count": len(raw.splitlines()),
            "logical_sha256": hashlib.sha256(raw.encode()).hexdigest()}


def sqlite_snapshot(container: str) -> dict:
    code = """
import hashlib, json, sqlite3
from edge.database.db import DB_PATH
with sqlite3.connect(DB_PATH) as conn:
    columns = [row[1] for row in conn.execute('pragma table_info(events)')]
    rows = conn.execute('select * from events order by event_id').fetchall()
raw = json.dumps({'columns': columns, 'rows': rows}, sort_keys=True, default=str, separators=(',', ':'))
print(json.dumps({'count': len(rows), 'logical_sha256': hashlib.sha256(raw.encode()).hexdigest()}))
"""
    return docker_python(container, code)


def database_snapshot() -> dict:
    return {
        "postgresql": postgres_snapshot(),
        "hospital_a_sqlite": sqlite_snapshot(CONTAINERS["hospital_a"]),
        "hospital_b_sqlite": sqlite_snapshot(CONTAINERS["hospital_b"]),
    }


def hospital_status(container: str, event_type: str, event_id: str) -> dict:
    event = json.dumps({"event_type": event_type, "event_id": event_id})
    code = ("import json; from edge.capture_api.cloud_client import get_cloud_event_status as f; "
            f"print(json.dumps(f(json.loads({event!r}))))")
    return docker_python(container, code)


def hospital_invalid_grant_send(container: str, site_id: str, event_id: str) -> dict:
    event = {
        "event_id": event_id, "site_id": site_id,
        "event_type": "patient_registration", "base_priority": 3,
        "prerequisite_ids": [], "payload": {"purpose": "bounded provenance rejection probe"},
    }
    encoded = json.dumps(event)
    code = ("import json; from edge.capture_api.cloud_client import send_to_cloud as f; "
            f"print(json.dumps(f(json.loads({encoded!r}), grant_id='00000000-0000-0000-0000-000000000000')))")
    return docker_python(container, code)


def dashboard_live_probe(service: str, *, contradictory: bool = False) -> dict:
    internal = {"patient": "patient", "emergency": "emergency", "admin": "admin"}[service]
    event_id = SERVICES[service][1]
    grant = "h['X-MetaGuard-Grant']='00000000-0000-0000-0000-000000000000';" if contradictory else ""
    code = (
        "import json,httpx; from cloud.traffic import traffic_headers; "
        "h=traffic_headers('live'); " + grant +
        f"r=httpx.get('http://{internal}:8000/events/{event_id}',headers=h,timeout=5); "
        "print(json.dumps({'status_code':r.status_code,'body_status':r.json().get('status'),"
        "'telemetry_header_present':bool(h),'token_length_policy_met':len(h.get('X-CrisisGuard-Telemetry-Token',''))>=32}))"
    )
    return docker_python(CONTAINERS["dashboard"], code)


def expected_counts(final: dict) -> dict:
    fields = ("live_latency_sample_count", "replay_latency_sample_count",
              "unclassified_latency_sample_count", "latency_sample_count")
    return {service: {field: final[service][field] for field in fields}
            for service in SERVICES}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite {args.output}")

    baseline = metrics()
    for service, sample in baseline.items():
        assert sample["latency_sample_count"] == 0, f"{service} telemetry window is not empty"
    db_before = database_snapshot()
    started = now()

    reconciliation = {
        "hospital_a_patient": hospital_status(CONTAINERS["hospital_a"], "patient_registration", SERVICES["patient"][1]),
        "hospital_a_emergency": hospital_status(CONTAINERS["hospital_a"], "emergency_record", SERVICES["emergency"][1]),
        "hospital_a_admin": hospital_status(CONTAINERS["hospital_a"], "patient_discharge", SERVICES["admin"][1]),
        "hospital_b_patient": hospital_status(CONTAINERS["hospital_b"], "patient_registration", "CP7B_MAIN_20261027T144951_Hospital_B_2"),
    }
    rejected_ids = {
        "hospital_a": "CP7B_PROVENANCE_REJECT_HOSPITAL_A_20261028",
        "hospital_b": "CP7B_PROVENANCE_REJECT_HOSPITAL_B_20261028",
    }
    invalid_grant = {
        "hospital_a": hospital_invalid_grant_send(CONTAINERS["hospital_a"], "Hospital_A", rejected_ids["hospital_a"]),
        "hospital_b": hospital_invalid_grant_send(CONTAINERS["hospital_b"], "Hospital_B", rejected_ids["hospital_b"]),
    }
    dashboard_live = {service: dashboard_live_probe(service) for service in SERVICES}
    external_unmarked = request_json(SERVICES["patient"][0] + "/events/" + SERVICES["patient"][1])
    external_invalid_mark = request_json(
        SERVICES["patient"][0] + "/events/" + SERVICES["patient"][1],
        headers={"X-CrisisGuard-Traffic": "live", "X-CrisisGuard-Telemetry-Token": "invalid"},
    )
    contradictory = dashboard_live_probe("patient", contradictory=True)
    dashboard_guard = request_json(
        "http://127.0.0.1:8080/api/events/patient", method="POST",
        body={"event_id": "CP7B_DASHBOARD_GUARD_20261028", "site_id": "Hospital_A",
              "event_type": "patient_registration", "base_priority": 3,
              "prerequisite_ids": [], "payload": {"purpose": "guard probe"}},
    )

    final = metrics()
    db_after = database_snapshot()
    counts = expected_counts(final)
    expected = {
        "patient": {"live_latency_sample_count": 1, "replay_latency_sample_count": 2,
                    "unclassified_latency_sample_count": 5, "latency_sample_count": 8},
        "emergency": {"live_latency_sample_count": 1, "replay_latency_sample_count": 0,
                      "unclassified_latency_sample_count": 1, "latency_sample_count": 2},
        "admin": {"live_latency_sample_count": 1, "replay_latency_sample_count": 0,
                  "unclassified_latency_sample_count": 1, "latency_sample_count": 2},
    }
    assertions = {
        "classification_counts_exact": counts == expected,
        "all_reconciliation_applied": all(item.get("state") == "applied" for item in reconciliation.values()),
        "authenticated_invalid_grant_posts_denied_409": all(item.get("status_code") == 409 for item in invalid_grant.values()),
        "dashboard_direct_submission_denied_403": dashboard_guard["status_code"] == 403,
        "dashboard_live_helper_authenticated": all(item["status_code"] == 200 and item["telemetry_header_present"] and item["token_length_policy_met"] for item in dashboard_live.values()),
        "unmarked_and_invalid_requests_succeeded_but_unclassified": external_unmarked["status_code"] == 200 and external_invalid_mark["status_code"] == 200,
        "contradictory_live_with_grant_succeeded_but_unclassified": contradictory["status_code"] == 200,
        "database_logical_snapshots_unchanged": db_before == db_after,
    }
    evidence = {
        "checkpoint": "7B", "scope": "Original desktop deployed-caller provenance verification",
        "started_at": started, "finished_at": now(),
        "method": {
            "clean_window_required": True,
            "hospital_calls": "Deployed edge.capture_api.cloud_client functions invoked in each running Hospital container",
            "dashboard_live_calls": "Deployed cloud.traffic helper invoked in Dashboard container; not an active Dashboard route while direct submissions are disabled",
            "write_safety": "Synthetic Hospital POSTs use an authenticated but nonexistent grant UUID and must be rejected before persistence",
        },
        "baseline_metrics": baseline,
        "reconciliation_get_results": reconciliation,
        "invalid_grant_replay_post_results": invalid_grant,
        "dashboard_live_credential_helper_results": dashboard_live,
        "dashboard_direct_submission_guard": dashboard_guard,
        "external_unmarked_result": external_unmarked,
        "external_invalid_mark_result": external_invalid_mark,
        "contradictory_live_with_grant_result": contradictory,
        "final_metrics": final,
        "observed_classification_counts": counts,
        "expected_classification_counts": expected,
        "rejected_event_ids": rejected_ids,
        "database_before": db_before, "database_after": db_after,
        "assertions": assertions,
        "result": "passed" if all(assertions.values()) else "failed",
        "limitations": [
            "Hospital reconciliation GETs are unmarked by deployed code and therefore unclassified.",
            "Dashboard direct Cloud POST forwarding is intentionally unreachable while METAGUARD_REQUIRED=1; helper probes validate its configured live credential, not an active submission route.",
            "External callers require explicit live provenance credentials; missing, invalid, or contradictory markings fail closed to unclassified.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "result": evidence["result"],
                      "counts": counts, "assertions": assertions}, indent=2))
    if evidence["result"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
