"""Focused old-controller -> AIMD -> old-controller PostgreSQL rehearsal.

Run from the repository root with local Docker Desktop and the project venv:
    .venv/Scripts/python.exe docs/evidence/cp7b-aws-aimd-rollback-compatibility-20261029.py

Pass --output with a new filename to retain a full JSON report. Existing reports
are never overwritten.

The test creates one uniquely named local PostgreSQL 16 container with tmpfs data
and a random loopback port. It extracts the deployed Git revision into a temporary
directory. No AWS connection, production database, or existing Docker volume is used.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
from uuid import uuid4

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[2]
OLD = "1482fe3827896f135577ee53b68ddd67b57f881f"
NEW = "f128ac0f2b6e551af3e03cb3e3d44262b451195a"


def run(args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{args[0]} command failed (exit {result.returncode}); output suppressed")
    return result


def phase(source: Path, db_url: str, code: str, **settings):
    env = os.environ.copy()
    env.update(DATABASE_URL=db_url, PYTHONPATH=str(source),
               CONTROLLER_ADMISSION_ENABLED="1")
    env.update({key: str(value) for key, value in settings.items()})
    result = run([sys.executable, "-c", code], cwd=source, env=env)
    return json.loads(result.stdout)


def snapshot(conn):
    tables = {
        "events": "SELECT * FROM events ORDER BY event_id",
        "grants": "SELECT * FROM controller_grants ORDER BY grant_id",
        "tickets": "SELECT * FROM controller_tickets ORDER BY hospital_id",
        "grant_audit": "SELECT * FROM controller_grant_audit ORDER BY id",
        "admission_base": """SELECT singleton,state,healthy_count,state_since,reason,
            observed_at,metrics,next_poll_at,poll_version,last_issue_at,
            next_issue_at,last_ack_at,next_ack_at,allowed,denied,last_decision
            FROM controller_admission""",
    }
    result = {}
    for name, query in tables.items():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query)
            rows = cur.fetchall()
        encoded = json.dumps(rows, sort_keys=True, default=str).encode()
        result[name] = {"rows": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    return result


def schema(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='controller_admission'
              AND column_name IN ('rate_rps','rate_healthy_count')
            ORDER BY column_name""")
        columns = [row[0] for row in cur.fetchall()]
        cur.execute("""SELECT indexname FROM pg_indexes
            WHERE schemaname='public' AND tablename='controller_grants'
            ORDER BY indexname""")
        indexes = [row[0] for row in cur.fetchall()]
        cur.execute("""SELECT conname FROM pg_constraint
            WHERE conrelid='controller_grants'::regclass AND contype='u'""")
        unique_constraints = [row[0] for row in cur.fetchall()]
    return {"aimd_columns": columns, "grant_indexes": indexes,
            "grant_unique_constraints": unique_constraints}


def seed(conn):
    now = datetime.now(timezone.utc)
    completed, acknowledged = str(uuid4()), str(uuid4())
    with conn.cursor() as cur:
        for event_id, hospital in (("SYNTH_A_APPLIED", "Hospital_A"),
                                   ("SYNTH_B_APPLIED", "Hospital_B")):
            cur.execute("""INSERT INTO events
                (event_id,site_id,event_type,base_priority,prerequisite_ids,
                 payload,status) VALUES (%s,%s,'patient_registration',1,%s,%s,'applied')""",
                (event_id, hospital, Jsonb([]), Jsonb({"patient_id": event_id})))
        cur.execute("""INSERT INTO controller_grants
            (grant_id,hospital_id,event_id,expires_at,status,priority,
             original_queue_position,original_bypass_count,event_digest)
            VALUES (%s,'Hospital_A','SYNTH_A_APPLIED',%s,'completed',3,1,0,%s),
                   (%s,'Hospital_B','SYNTH_B_PENDING',%s,'acknowledged',4,2,1,%s)""",
            (completed, now + timedelta(hours=1), "a" * 64,
             acknowledged, now + timedelta(hours=1), "b" * 64))
        cur.execute("""INSERT INTO controller_tickets
            (hospital_id,event_id,priority,bypass_count,event_digest,queue_position)
            VALUES ('Hospital_A','SYNTH_A_NEXT',3,1,%s,
                    nextval('controller_queue_position_seq'))""", ("c" * 64,))
        cur.execute("""INSERT INTO controller_grant_audit
            (grant_id,previous_status,new_status,action)
            VALUES (%s,'acknowledged','completed','synthetic_seed')""", (completed,))
        cur.execute("""UPDATE controller_admission SET state='Normal',
            healthy_count=1,state_since=%s,reason='healthy',observed_at=%s,
            metrics=%s,allowed=2,denied=1 WHERE singleton""",
            (now - timedelta(seconds=30), now, Jsonb({"synthetic": True})))
    conn.commit()
    return {"completed_grant_id": completed, "acknowledged_grant_id": acknowledged}


OLD_INIT = """import json
from cloud.controller.storage import ControllerStorage
from cloud.database.db import init_db
init_db(); ControllerStorage().init_db()
print(json.dumps({'initialized': True}))
"""

NEW_INIT = """import json
from cloud.controller.storage import ControllerStorage
storage=ControllerStorage(); storage.init_db(); storage.init_db()
print(json.dumps({'migrated': True, 'idempotent_second_init': True}))
"""

NEW_AIMD = """import json,time
from datetime import datetime,timezone
from cloud.controller.storage import ControllerStorage
from cloud.database.db import get_connection
from cloud.controller import admission
storage=ControllerStorage()
config=admission.settings()
rates=[]
for index in range(2):
    if index: time.sleep(0.15)
    version=storage.claim_admission_poll(config)
    assert version is not None
    stamp=datetime.now(timezone.utc).isoformat()
    samples={name:{'sampled_at':stamp,'service':name,'database_healthy':True,
                   'average_latency_ms':1.0,'throughput_rps':1.0,'error_rate':0.0,
                   'uptime_seconds':10.0,'window_seconds':60.0,
                   'total_requests':10,'failed_requests':0,'window_requests':10}
             for name in admission.SERVICES}
    storage.update_admission_metrics(samples,version,config)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT state,rate_rps,rate_healthy_count FROM controller_admission')
        rates.append(cur.fetchone())
print(json.dumps({'policy':admission.policy(),'states_and_rates':rates,
                  'candidate_api_available':hasattr(storage,'set_candidate')}))
"""

NEW_FIXED_STATUS = """import json
from cloud.controller.storage import ControllerStorage
from cloud.controller import admission
storage=ControllerStorage()
status=storage.admission_status()
print(json.dumps({'policy':admission.policy(),'state':status['state'],
                  'reported_rate_rps':status['rate_rps'],
                  'telemetry_fresh':status['telemetry_fresh'],
                  'candidate_api_available':hasattr(storage,'set_candidate')}))
"""

OLD_AFTER_MIGRATION = """import json,os
from cloud.controller.storage import ControllerStorage
from cloud.database.db import get_connection
storage=ControllerStorage(); storage.init_db()
status=storage.admission_status()
waiting=storage.list_tickets()
completed=storage.get_grant_status(os.environ['PHASE_COMPLETED_GRANT_ID'])
acknowledged=storage.get_grant_status(os.environ['PHASE_ACKNOWLEDGED_GRANT_ID'])
issued=storage.issue_grant()
with get_connection() as conn, conn.cursor() as cur:
    cur.execute('SELECT last_decision FROM controller_admission')
    decision=cur.fetchone()[0]
print(json.dumps({'state':status['state'],'reported_rate_rps':status['rate_rps'],
                  'waiting_before_issue':len(waiting),
                  'grant_identity_preserved':bool(completed and acknowledged and
                      completed['hospital_id']=='Hospital_A' and
                      completed['event_id']=='SYNTH_A_APPLIED' and
                      completed['status']=='completed' and
                      acknowledged['hospital_id']=='Hospital_B' and
                      acknowledged['event_id']=='SYNTH_B_PENDING' and
                      acknowledged['status']=='acknowledged'),
                  'issued':issued is not None,
                  'issued_rate_rps':decision.get('rate_rps'),
                  'candidate_api_available':hasattr(storage,'set_candidate')}))
"""

STALE_RESERVE = """import json
from psycopg.rows import dict_row
from cloud.controller import admission
from cloud.database.db import get_connection
with get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
    allowed=admission.reserve(cur,'issue')
    cur.execute('SELECT state,last_decision FROM controller_admission')
    result=cur.fetchone()
print(json.dumps({'allowed':allowed,'state':result['state'],
                  'reason':result['last_decision'].get('reason')}))
"""

ADD_SUPERSEDED = """import json
from cloud.controller.storage import ControllerStorage
storage=ControllerStorage()
added=storage.add_ticket('Hospital_B','SYNTH_B_SUPERSEDED',2,'d'*64)
print(json.dumps({'added_ticket_after_superseded_grant':added}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    context = run(["docker", "context", "inspect", "--format",
                   "{{.Endpoints.docker.Host}}" ]).stdout.strip()
    if not context.startswith("npipe:"):
        raise RuntimeError("Refusing non-local Docker endpoint")
    if run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip() != NEW:
        raise RuntimeError("Current source revision differs from the reviewed AIMD revision")
    report = {"date_sgt": "2026-10-29", "old_revision": OLD,
              "new_revision": NEW, "docker_endpoint_kind": "local_npipe",
              "storage": "one disposable PostgreSQL 16 tmpfs container; random loopback port"}
    name = "cg-cp7b-rollback-" + uuid4().hex[:12]
    container = None
    with tempfile.TemporaryDirectory(prefix="cg-cp7b-old-source-") as old_dir:
        old_source = Path(old_dir)
        archive = subprocess.check_output(
            ["git", "archive", "--format=tar", OLD, "cloud"], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or path.parts[0] != "cloud":
                    raise RuntimeError("Unsafe archive member")
            tar.extractall(old_source, filter="data")
        try:
            password = secrets.token_hex(16)
            container = run(["docker", "run", "-d", "--name", name,
                             "--label", "crisisguard.cp7b.rollback-test=" + name,
                             "--publish", "127.0.0.1::5432",
                             "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
                             "-e", "POSTGRES_PASSWORD=" + password,
                             "-e", "POSTGRES_DB=rollback_test",
                             "postgres:16", "-c", "shared_buffers=32MB",
                             "-c", "max_connections=10"]).stdout.strip()
            published = run(["docker", "port", container, "5432/tcp"]).stdout.strip()
            match = re.fullmatch(r"127\.0\.0\.1:(\d+)", published)
            if not match:
                raise RuntimeError("PostgreSQL was not bound solely to loopback")
            db_url = f"postgresql://postgres:{password}@127.0.0.1:{match[1]}/rollback_test"
            deadline = time.monotonic() + 60
            while True:
                try:
                    with psycopg.connect(db_url, connect_timeout=2) as conn:
                        conn.execute("SELECT 1")
                    break
                except psycopg.OperationalError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Disposable PostgreSQL did not become ready") from None
                    time.sleep(0.5)

            phase(old_source, db_url, OLD_INIT)
            with psycopg.connect(db_url) as conn:
                old_schema = schema(conn)
                identities = seed(conn)
                before = snapshot(conn)
            phase(ROOT, db_url, NEW_INIT)
            with psycopg.connect(db_url) as conn:
                new_schema = schema(conn)
                migrated = snapshot(conn)
                rate_rps, rate_healthy_count = conn.execute(
                    "SELECT rate_rps,rate_healthy_count FROM controller_admission"
                ).fetchone()
            if before != migrated:
                raise RuntimeError("Migration changed existing controller or event rows")
            new_result = phase(
                ROOT, db_url, NEW_AIMD,
                CONTROLLER_ADMISSION_POLICY="aimd",
                CONTROLLER_ADMISSION_POLL_SECONDS="0.1",
                CONTROLLER_ADMISSION_MAX_AGE_SECONDS="5",
                CONTROLLER_ADMISSION_TIMEOUT_SECONDS="0.05",
                CONTROLLER_ADMISSION_DWELL_SECONDS="0.1",
                CONTROLLER_ADMISSION_HEALTHY_SAMPLES="2")
            with psycopg.connect(db_url) as conn:
                after_aimd = snapshot(conn)
            if any(before[k] != after_aimd[k] for k in
                   ("events", "grants", "tickets", "grant_audit")):
                raise RuntimeError("AIMD telemetry polling changed event/grant/ticket rows")
            if before["admission_base"] == after_aimd["admission_base"]:
                raise RuntimeError("AIMD telemetry polling did not update admission state")
            new_fixed = phase(ROOT, db_url, NEW_FIXED_STATUS,
                              CONTROLLER_ADMISSION_POLICY="fixed")

            old_result = phase(old_source, db_url, OLD_AFTER_MIGRATION,
                               PHASE_COMPLETED_GRANT_ID=identities["completed_grant_id"],
                               PHASE_ACKNOWLEDGED_GRANT_ID=identities["acknowledged_grant_id"])
            with psycopg.connect(db_url) as conn:
                after_old_issue = snapshot(conn)
            if before["events"] != after_old_issue["events"]:
                raise RuntimeError("Old controller changed cloud event identities")

            # Staleness and pacing are tested on this disposable database only.
            def make_stale():
                with psycopg.connect(db_url) as conn:
                    conn.execute("""UPDATE controller_admission SET state='Normal',
                        observed_at=clock_timestamp()-interval '1 hour',
                        last_issue_at=NULL,next_issue_at=NULL WHERE singleton""")

            caps = {"CONTROLLER_ADMISSION_NORMAL_INFLIGHT": "4",
                    "CONTROLLER_ADMISSION_CAUTION_INFLIGHT": "4",
                    "CONTROLLER_ADMISSION_RECOVERY_INFLIGHT": "4"}
            make_stale()
            old_stale = phase(old_source, db_url, STALE_RESERVE, **caps)
            make_stale()
            new_stale = phase(ROOT, db_url, STALE_RESERVE,
                              CONTROLLER_ADMISSION_POLICY="aimd", **caps)
            make_stale()
            new_fixed_stale = phase(ROOT, db_url, STALE_RESERVE,
                                    CONTROLLER_ADMISSION_POLICY="fixed", **caps)

            with psycopg.connect(db_url) as conn:
                conn.execute("""INSERT INTO controller_grants
                    (grant_id,hospital_id,event_id,expires_at,status,event_digest)
                    VALUES (%s,'Hospital_B','SYNTH_B_SUPERSEDED',
                            clock_timestamp()+interval '1 hour','superseded',%s)""",
                    (str(uuid4()), "d" * 64))
            old_superseded = phase(old_source, db_url, ADD_SUPERSEDED)
            new_superseded = phase(ROOT, db_url, ADD_SUPERSEDED,
                                   CONTROLLER_ADMISSION_POLICY="aimd")

            # The active-grant uniqueness rule must survive both init_db calls.
            active_duplicate_rejected = False
            try:
                with psycopg.connect(db_url) as conn:
                    conn.execute("""INSERT INTO controller_grants
                        (grant_id,hospital_id,event_id,expires_at,status)
                        VALUES (%s,'Hospital_B','SYNTH_B_PENDING',
                                clock_timestamp()+interval '1 hour','issued')""",
                        (str(uuid4()),))
            except errors.UniqueViolation:
                active_duplicate_rejected = True
            with psycopg.connect(db_url) as conn:
                conn.execute("""INSERT INTO controller_grants
                    (grant_id,hospital_id,event_id,expires_at,status)
                    VALUES (%s,'Hospital_B','SYNTH_B_PENDING',
                            clock_timestamp()+interval '1 hour','completed')""",
                    (str(uuid4()),))
                conn.rollback()  # Demonstrate terminal history without retaining it.

            # A drained grant set exercises the deployed default Recovery cap of 1.
            # This is a separate disposable scenario after preservation checks.
            with psycopg.connect(db_url) as conn:
                conn.execute("""UPDATE controller_grants SET status='completed',
                    completed_at=clock_timestamp()
                    WHERE status IN ('issued','acknowledged')""")
            make_stale()
            old_stale_default = phase(old_source, db_url, STALE_RESERVE)
            make_stale()
            new_fixed_stale_default = phase(ROOT, db_url, STALE_RESERVE,
                                            CONTROLLER_ADMISSION_POLICY="fixed")

            report.update({
                "original_schema": old_schema,
                "migrated_schema": new_schema,
                "pre_migration_rows": before,
                "post_migration_rows_before_poll": migrated,
                "post_aimd_rows": after_aimd,
                "rows_preserved_during_migration": before == migrated,
                "new_columns_before_poll": {
                    "rate_rps": rate_rps,
                    "rate_healthy_count": rate_healthy_count,
                },
                "aimd": new_result,
                "new_controller_fixed_policy_backout": new_fixed,
                "old_controller_after_migration": old_result,
                "events_preserved_after_old_issue": before["events"] == after_old_issue["events"],
                "old_stale_telemetry": old_stale,
                "new_stale_telemetry": new_stale,
                "new_fixed_stale_telemetry": new_fixed_stale,
                "old_stale_with_default_caps_after_drain": old_stale_default,
                "new_fixed_stale_with_default_caps_after_drain": new_fixed_stale_default,
                "old_superseded_ticket": old_superseded,
                "new_superseded_ticket": new_superseded,
                "active_duplicate_grant_rejected": active_duplicate_rejected,
                "terminal_grant_history_allowed": True,
                "decision": "direct_old_controller_rollback_unsafe",
            })
            assert old_schema["aimd_columns"] == []
            assert new_schema["aimd_columns"] == ["rate_healthy_count", "rate_rps"]
            assert old_schema["grant_indexes"] == new_schema["grant_indexes"]
            assert rate_rps is None and rate_healthy_count == 0
            assert new_result["policy"] == "aimd"
            assert new_result["states_and_rates"][0][1:] == [0.2, 1]
            assert new_result["states_and_rates"][1][1:] == [0.4, 0]
            assert new_fixed["policy"] == "fixed" and new_fixed["reported_rate_rps"] == 2.0
            assert new_fixed["telemetry_fresh"] and new_fixed["candidate_api_available"]
            assert old_result["issued"] and old_result["reported_rate_rps"] == 2.0
            assert old_result["grant_identity_preserved"]
            assert old_result["issued_rate_rps"] == 2.0
            assert old_stale["allowed"] and not new_stale["allowed"]
            assert not new_fixed_stale["allowed"]
            assert old_stale_default["allowed"] and not new_fixed_stale_default["allowed"]
            assert old_superseded["added_ticket_after_superseded_grant"] is False
            assert new_superseded["added_ticket_after_superseded_grant"] is True
            assert active_duplicate_rejected
        finally:
            if container:
                removed = subprocess.run(["docker", "rm", "-f", container],
                                         text=True, capture_output=True)
                report["disposable_container_removed"] = removed.returncode == 0
    if not report.get("disposable_container_removed"):
        raise RuntimeError("Disposable PostgreSQL container cleanup failed")
    if args.output:
        target = args.output.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
    print(json.dumps({"decision": report["decision"],
                      "rows_preserved": report["rows_preserved_during_migration"],
                      "old_rate": report["old_controller_after_migration"]["reported_rate_rps"],
                      "aimd_rate": report["aimd"]["states_and_rates"][-1][1],
                      "old_stale_allowed": report["old_stale_telemetry"]["allowed"],
                      "new_stale_allowed": report["new_stale_telemetry"]["allowed"],
                      "new_fixed_stale_allowed": report["new_fixed_stale_telemetry"]["allowed"],
                      "old_stale_default_caps_allowed": report["old_stale_with_default_caps_after_drain"]["allowed"],
                      "new_fixed_stale_default_caps_allowed": report["new_fixed_stale_with_default_caps_after_drain"]["allowed"],
                      "container_removed": report["disposable_container_removed"]}))


if __name__ == "__main__":
    main()
