"""Capture a secret-free Checkpoint 7B Hospital deployment snapshot.

This script is read-only. It records container/image/resource identities,
logical database hashes, controller state, telemetry counts and deployed source
hashes. It refuses to overwrite its output and never emits credential values.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
MAIN = {
    "postgres": "crisisguard-local-postgres-1",
    "controller": "crisisguard-local-controller-1",
    "patient": "crisisguard-local-patient-1",
    "emergency": "crisisguard-local-emergency-1",
    "admin": "crisisguard-local-admin-1",
    "hospital_a": "crisisguard-local-hospital-a-1",
    "hospital_b": "crisisguard-local-hospital-b-1",
    "dashboard": "crisisguard-local-dashboard-1",
}


def run(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=30).stdout


def docker(*args: str) -> str:
    return run(["docker", *args])


def inspect() -> dict:
    raw = json.loads(docker("inspect", *MAIN.values()))
    result = {}
    for item in raw:
        name = next(key for key, value in MAIN.items()
                    if value == item["Name"].lstrip("/"))
        health = (item["State"].get("Health") or {}).get("Status")
        result[name] = {
            "id": item["Id"], "image_id": item["Image"],
            "created": item["Created"], "started_at": item["State"]["StartedAt"],
            "state": item["State"]["Status"], "health": health,
            "mounts": [{"name": m.get("Name"), "destination": m["Destination"],
                        "type": m["Type"], "rw": m["RW"]} for m in item["Mounts"]],
            "compose_config_files": item["Config"].get("Labels", {}).get(
                "com.docker.compose.project.config_files"),
        }
    return result


def postgres() -> dict:
    sql = ("select event_id,site_id,event_type,base_priority,prerequisite_ids::text,"
           "payload::text,status,coalesce(applied_at::text,'') from events order by event_id")
    rows = docker("exec", MAIN["postgres"], "psql", "-U", "crisisguard", "-d",
                  "crisisguard", "-At", "-F", "|", "-c", sql)
    counts_sql = ("select json_build_object('events',(select count(*) from events),"
                  "'grants',(select count(*) from controller_grants),"
                  "'tickets',(select count(*) from controller_tickets));")
    counts = json.loads(docker("exec", MAIN["postgres"], "psql", "-U", "crisisguard",
                               "-d", "crisisguard", "-Atc", counts_sql))
    controller_dump = docker("exec", MAIN["postgres"], "pg_dump", "-U", "crisisguard",
                             "-d", "crisisguard", "--data-only", "--table=controller_*")
    return {
        **counts,
        "events_logical_sha256": hashlib.sha256(rows.encode()).hexdigest(),
        "controller_data_sha256": hashlib.sha256(controller_dump.encode()).hexdigest(),
    }


def sqlite(container: str) -> dict:
    code = """
import hashlib,json,sqlite3
from edge.database.db import DB_PATH
with sqlite3.connect(DB_PATH) as c:
 columns=[r[1] for r in c.execute('pragma table_info(events)')]
 rows=c.execute('select * from events order by event_id').fetchall()
 attempts=c.execute('select count(*) from metaguard_attempts').fetchone()[0]
 integrity=c.execute('pragma integrity_check').fetchone()[0]
raw=json.dumps({'columns':columns,'rows':rows},sort_keys=True,default=str,separators=(',',':'))
print(json.dumps({'events':len(rows),'attempts':attempts,'integrity':integrity,'events_logical_sha256':hashlib.sha256(raw.encode()).hexdigest()}))
"""
    return json.loads(docker("exec", container, "python", "-c", code))


def metrics() -> dict:
    result = {}
    for name, port in (("patient", 8001), ("emergency", 8003), ("admin", 8004)):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            body = json.load(response)
        result[name] = {key: body[key] for key in (
            "live_latency_sample_count", "replay_latency_sample_count",
            "unclassified_latency_sample_count", "latency_sample_count",
            "total_requests", "database_healthy", "sampled_at")}
    return result


def controller() -> dict:
    code = """
import json,os,urllib.request
r=urllib.request.Request('http://127.0.0.1:8000/admission/status',headers={'X-Cloud-Token':os.environ['CONTROLLER_CLOUD_TOKEN']})
with urllib.request.urlopen(r,timeout=5) as response: b=json.load(response)
print(json.dumps({k:b[k] for k in ('enabled','state','reason','rate_rps','policy','telemetry_fresh','observed_at')}))
"""
    return json.loads(docker("exec", MAIN["controller"], "python", "-c", code))


def deployed_hospital(container: str) -> dict:
    code = """
import hashlib,json,os,pathlib
p=pathlib.Path('/app/edge/capture_api/cloud_client.py').read_bytes()
t=os.getenv('CLOUD_REPLAY_TELEMETRY_TOKEN','')
print(json.dumps({'cloud_client_sha256':hashlib.sha256(p).hexdigest(),'replay_credential_configured':len(t)>=32,'replay_credential_length':len(t)}))
"""
    return json.loads(docker("exec", container, "python", "-c", code))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"Refusing to overwrite {args.output}")
    all_containers = json.loads("[" + ",".join(
        docker("ps", "-a", "--no-trunc", "--format", "{{json .}}").splitlines()) + "]")
    source = (ROOT / "edge/capture_api/cloud_client.py").read_bytes()
    evidence = {
        "checkpoint": "7B", "phase": args.phase,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "workspace_cloud_client_sha256": hashlib.sha256(source).hexdigest(),
        "main_containers": inspect(),
        "all_containers": {item["Names"]: {"id": item["ID"], "image": item["Image"],
                                             "status": item["Status"]}
                           for item in all_containers},
        "volumes": sorted(docker("volume", "ls", "-q").splitlines()),
        "postgresql": postgres(),
        "hospital_a_sqlite": sqlite(MAIN["hospital_a"]),
        "hospital_b_sqlite": sqlite(MAIN["hospital_b"]),
        "controller": controller(),
        "cloud_metrics": metrics(),
        "hospital_a_deployed": deployed_hospital(MAIN["hospital_a"]),
        "hospital_b_deployed": deployed_hospital(MAIN["hospital_b"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "phase": args.phase,
                      "postgresql": evidence["postgresql"],
                      "hospital_a": evidence["hospital_a_sqlite"],
                      "hospital_b": evidence["hospital_b_sqlite"]}, indent=2))


if __name__ == "__main__":
    main()
