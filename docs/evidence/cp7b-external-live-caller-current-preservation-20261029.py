"""Recheck durable original-stack preservation while the application is stopped.

The existing PostgreSQL container is started temporarily for read-only queries
and returned to its initial state. Hospital SQLite volumes are mounted read-only
in network-isolated ephemeral helpers. No application service or volume is
recreated and no replay path can run.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "docs/evidence/cp7b-external-live-caller-current-preservation-20261029.json"
POSTGRES = "crisisguard-local-postgres-1"
MAIN = (
    POSTGRES, "crisisguard-local-controller-1", "crisisguard-local-patient-1",
    "crisisguard-local-emergency-1", "crisisguard-local-admin-1",
    "crisisguard-local-hospital-a-1", "crisisguard-local-hospital-b-1",
    "crisisguard-local-dashboard-1",
)


def run(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=45).stdout


def docker(*args: str) -> str:
    return run(["docker", *args])


def inspect_main() -> dict:
    items = json.loads(docker("inspect", *MAIN))
    return {item["Name"].lstrip("/"): {
        "id": item["Id"], "image": item["Image"],
        "state": item["State"]["Status"],
        "mounts": [{"name": m.get("Name"), "destination": m["Destination"],
                    "type": m["Type"], "rw": m["RW"]} for m in item["Mounts"]],
    } for item in items}


def psql(sql: str) -> str:
    return docker("exec", POSTGRES, "psql", "-U", "crisisguard", "-d",
                  "crisisguard", "-Atc", sql)


def postgres_snapshot() -> dict:
    events = psql(
        "select event_id,site_id,event_type,base_priority,prerequisite_ids::text,"
        "payload::text,status,coalesce(applied_at::text,'') from events order by event_id;")
    grants = psql(
        "select row_to_json(t)::text from "
        "(select * from controller_grants order by grant_id) t;")
    tickets = psql(
        "select row_to_json(t)::text from "
        "(select * from controller_tickets order by 1,2) t;")
    counts = psql(
        "select (select count(*) from events),"
        "(select count(*) from controller_grants),"
        "(select count(*) from controller_tickets),"
        "(select count(*) from events where event_id like 'CP7B_LIVE_CALLER_REJECT_%');"
    ).strip().split("|")
    return {
        "events": int(counts[0]), "grants": int(counts[1]),
        "tickets": int(counts[2]), "live_probe_events": int(counts[3]),
        "events_logical_sha256": hashlib.sha256(events.encode()).hexdigest(),
        "grants_data_sha256": hashlib.sha256(grants.encode()).hexdigest(),
        "tickets_data_sha256": hashlib.sha256(tickets.encode()).hexdigest(),
    }


def sqlite_snapshot(volume: str) -> dict:
    code = """
import hashlib,json,sqlite3
uri='file:/data/hospital.db?mode=ro'
with sqlite3.connect(uri,uri=True) as c:
 c.execute('pragma query_only=on')
 columns=[r[1] for r in c.execute('pragma table_info(events)')]
 rows=c.execute('select * from events order by event_id').fetchall()
 attempts=c.execute('select count(*) from metaguard_attempts').fetchone()[0]
 integrity=c.execute('pragma integrity_check').fetchone()[0]
raw=json.dumps({'columns':columns,'rows':rows},sort_keys=True,default=str,separators=(',',':'))
print(json.dumps({'events':len(rows),'attempts':attempts,'integrity':integrity,'events_logical_sha256':hashlib.sha256(raw.encode()).hexdigest()}))
"""
    return json.loads(docker(
        "run", "--rm", "--network", "none", "--read-only",
        "-v", f"{volume}:/data:ro", "--entrypoint", "python",
        "crisisguard-hospital:cp7b-provenance-20261028", "-c", code))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite {args.output}")

    expected_audit = json.loads((ROOT / "docs/evidence/cp7b-external-live-caller-audit-20261028.json").read_text(encoding="utf-8"))
    expected_hospitals = json.loads((ROOT / "docs/evidence/cp7b-hospital-provenance-after-20261028.json").read_text(encoding="utf-8"))
    before = inspect_main()
    volumes_before = sorted(docker("volume", "ls", "-q").splitlines())
    containers_before = len(docker("ps", "-a", "-q").splitlines())
    postgres_initially_running = before[POSTGRES]["state"] == "running"
    try:
        if not postgres_initially_running:
            docker("start", POSTGRES)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                state = json.loads(docker("inspect", POSTGRES))[0]["State"]
                if (state.get("Health") or {}).get("Status") == "healthy":
                    break
                time.sleep(1)
            else:
                raise RuntimeError("PostgreSQL did not become healthy")
        postgres = postgres_snapshot()
    finally:
        if not postgres_initially_running:
            docker("stop", POSTGRES)

    hospital_a = sqlite_snapshot("crisisguard-local_hospital-a-data")
    hospital_b = sqlite_snapshot("crisisguard-local_hospital-b-data")
    after = inspect_main()
    volumes_after = sorted(docker("volume", "ls", "-q").splitlines())
    containers_after = len(docker("ps", "-a", "-q").splitlines())
    expected_db = expected_audit["database_after"]
    expected_ids = expected_audit["runtime_after"]["containers"]
    assertions = {
        "postgresql_events_preserved": postgres["events"] == expected_db["events"] == 191 and postgres["events_logical_sha256"] == expected_db["events_logical_sha256"],
        "controller_grants_preserved": postgres["grants"] == expected_db["grants"] == 8 and postgres["grants_data_sha256"] == expected_db["grants_data_sha256"],
        "controller_tickets_preserved": postgres["tickets"] == expected_db["tickets"] == 0 and postgres["tickets_data_sha256"] == expected_db["tickets_data_sha256"],
        "no_live_probe_events": postgres["live_probe_events"] == 0,
        "hospital_a_preserved": hospital_a == expected_hospitals["hospital_a_sqlite"],
        "hospital_b_preserved": hospital_b == expected_hospitals["hospital_b_sqlite"],
        "main_container_ids_and_images_preserved": all(
            after[name]["id"] == expected_ids[name]["id"] and
            after[name]["image"] == expected_ids[name]["image"]
            for name in MAIN),
        "main_container_mounts_unchanged": all(before[name]["mounts"] == after[name]["mounts"] for name in MAIN),
        "initial_container_states_restored": all(before[name]["state"] == after[name]["state"] for name in MAIN),
        "volume_set_unchanged": volumes_before == volumes_after,
        "container_count_unchanged": containers_before == containers_after,
    }
    evidence = {
        "checkpoint": "7B", "scope": "Post-interruption durable preservation audit",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "method": {
            "postgresql": "Existing container temporarily started for read-only ordered-row queries and restored to its initial stopped state",
            "sqlite": "Network-isolated read-only ephemeral helpers; original Hospital containers remained stopped",
        },
        "postgresql": postgres, "hospital_a_sqlite": hospital_a,
        "hospital_b_sqlite": hospital_b,
        "containers_before": before, "containers_after": after,
        "volume_count_before": len(volumes_before), "volume_count_after": len(volumes_after),
        "container_count_before": containers_before, "container_count_after": containers_after,
        "assertions": assertions,
        "result": "passed" if all(assertions.values()) else "failed",
    }
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "postgresql": postgres,
                      "hospital_a": hospital_a, "hospital_b": hospital_b,
                      "assertions": assertions, "result": evidence["result"]}, indent=2))
    if evidence["result"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
