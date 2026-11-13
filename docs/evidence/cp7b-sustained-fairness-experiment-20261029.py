"""Finite sustained fairness experiment using the existing policy harness.

Run three fresh projects per policy concurrently on one measured-capacity host.
Each uses the unchanged real Hospital automatic replay and controller scheduler.
Only test data, arrivals and test-local admission configuration are supplied.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

import httpx
import psycopg
from psycopg.rows import dict_row


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("policy_harness", HERE / "cp7b-fixed-vs-aimd-experiment-20261029.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
SITES = base.SITES
STAMP = "20261029"


def source_hashes():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("cloud", "edge", "dashboard") for p in (ROOT / folder).rglob("*.py")
            if "__pycache__" not in p.parts}


def settings_for(policy):
    settings = dict(zip(("POSTGRES_PORT", "CONTROLLER_PORT", "PATIENT_PORT", "EMERGENCY_PORT",
                         "ADMIN_PORT", "HOSPITAL_A_PORT", "HOSPITAL_B_PORT", "DASHBOARD_PORT"),
                        map(str, base.free_ports(8))))
    settings.update(POSTGRES_USER="fairness_eval", POSTGRES_DB="fairness_eval",
        POSTGRES_PASSWORD=secrets.token_hex(24), CONTROLLER_CLOUD_TOKEN=secrets.token_hex(32),
        HOSPITAL_A_CONTROLLER_TOKEN=secrets.token_hex(32), HOSPITAL_B_CONTROLLER_TOKEN=secrets.token_hex(32),
        CLOUD_LIVE_TELEMETRY_TOKEN=secrets.token_hex(32), CLOUD_REPLAY_TELEMETRY_TOKEN=secrets.token_hex(32),
        AUTO_REPLAY="0", AUTO_REPLAY_INTERVAL_SECONDS="0.5", ENABLE_MANUAL_REPLAY="0", MANUAL_REPLAY_TOKEN="",
        CONTROLLER_ADMISSION_POLICY=policy, CONTROLLER_ADMISSION_ENABLED="1",
        CONTROLLER_ADMISSION_POLL_SECONDS="1", CONTROLLER_ADMISSION_MAX_AGE_SECONDS="5",
        CONTROLLER_ADMISSION_TIMEOUT_SECONDS="1", CONTROLLER_ADMISSION_DWELL_SECONDS="3",
        CONTROLLER_ADMISSION_HEALTHY_SAMPLES="2", CONTROLLER_ADMISSION_ADDITIVE_RPS="0.2",
        CONTROLLER_ADMISSION_NORMAL_RPS="2", CONTROLLER_ADMISSION_CAUTION_RPS="0.5",
        CONTROLLER_ADMISSION_RECOVERY_RPS="0.2", CONTROLLER_ADMISSION_NORMAL_INFLIGHT="4",
        CONTROLLER_ADMISSION_CAUTION_INFLIGHT="2", CONTROLLER_ADMISSION_RECOVERY_INFLIGHT="1",
        CONTROLLER_ADMISSION_CAUTION_LATENCY_MS="80", CONTROLLER_ADMISSION_RECOVERY_LATENCY_MS="1000",
        CONTROLLER_ADMISSION_HEALTHY_LATENCY_MS="50", CONTROLLER_ADMISSION_RECOVERY_ERROR_RATE="0.15")
    return settings


def batch(prefix, site, sequence, count, phase):
    """Two-event chains; alternate Patient/Emergency and Patient/Admin routes."""
    result = []
    high_site = SITES[0] if phase == 0 else SITES[1]
    for offset in range(0, count, 2):
        n = sequence + offset
        root_id = f"{prefix}-{site}-{n:05d}"
        high = site == high_site if phase < 2 else (n // 2) % 2 == 0
        child_priority = 5 if high else 1
        for index in range(2):
            # An urgent child lends its priority to its base-priority-1 parent.
            event_id = root_id if index == 0 else f"{prefix}-{site}-{n+1:05d}"
            kind = "patient_registration" if index == 0 else ("emergency_record" if (n // 2) % 2 == 0 else "patient_discharge")
            result.append({"event_id": event_id, "site_id": site, "event_type": kind,
                "base_priority": 1 if index == 0 else child_priority,
                "prerequisite_ids": [] if index == 0 else [root_id],
                "payload": {"synthetic": True, "sequence": n+index, "arrival_phase": phase}})
    return result


class FairnessTrial(base.Runner):
    def __init__(self, work, image, policy, ordinal, seconds, drain_seconds, original_volumes):
        self.policy, self.ordinal = policy, ordinal
        self.seconds, self.drain_seconds = seconds, drain_seconds
        self.project = f"cg-fair-{policy}-{ordinal}-{uuid.uuid4().hex[:8]}"
        trial_dir = work / self.project
        trial_dir.mkdir()
        super().__init__(trial_dir, image, trial_dir / "raw.json", 1)
        self.original_volumes = original_volumes
        self.output = {"policy": policy, "trial": ordinal, "project": self.project,
                       "started_at": base.utcnow(), "samples": [], "arrivals": [], "result": "running"}

    def save(self):
        self.raw_path.write_text(json.dumps(self.output, indent=2, default=str), encoding="utf-8")

    def execute_trial(self):
        settings = settings_for(self.policy)
        config = None
        client = httpx.Client(timeout=10, trust_env=False)
        expected = {}
        sequence = dict.fromkeys(SITES, 0)
        hospitals = dict(zip(SITES, [f"http://127.0.0.1:{settings[k]}" for k in ("HOSPITAL_A_PORT", "HOSPITAL_B_PORT")]))
        controller = f"http://127.0.0.1:{settings['CONTROLLER_PORT']}"
        cloud = {name: f"http://127.0.0.1:{settings[name.upper()+'_PORT']}" for name in base.SERVICES}
        operator = {"X-Cloud-Token": settings["CONTROLLER_CLOUD_TOKEN"]}
        url = f"postgresql://{settings['POSTGRES_USER']}:{settings['POSTGRES_PASSWORD']}@127.0.0.1:{settings['POSTGRES_PORT']}/{settings['POSTGRES_DB']}"

        def request(method, url, *, body=None, headers=None, allowed=(200,)):
            response = client.request(method, url, json=body, headers=headers)
            assert response.status_code in allowed, (response.status_code, response.text[:200])
            return response.json()

        def capture(site, count, phase, scheduled, start):
            events = batch(f"FAIR-{self.ordinal}", site, sequence[site], count, phase)
            sequence[site] += count
            for event in events:
                at = time.monotonic()
                request("POST", hospitals[site]+"/capture", body=event, allowed=(201,))
                expected[event["event_id"]] = event
                self.output["arrivals"].append({"event_id": event["event_id"], "site_id": site,
                    "scheduled_elapsed_seconds": scheduled, "actual_elapsed_seconds": None if start is None else at-start,
                    "captured_at": base.utcnow(), "phase": phase})

        try:
            config, resolved = self.resolve(self.project, self.work, settings)
            self.compose_call(self.project, config, "up", "-d", "--no-build", "--pull", "never", "--wait",
                              "--wait-timeout", "180", "postgres", "hospital-a", "hospital-b")
            # Initial recovery backlog: same count, dependencies and phase in every project.
            for site in SITES:
                capture(site, 32, 0, -1, None)
                assert all(e["status"] == "pending" for e in request("GET", hospitals[site]+"/events"))
            self.compose_call(self.project, config, "stop", "hospital-a", "hospital-b")
            self.compose_call(self.project, config, "up", "-d", "--no-build", "--pull", "never", "--wait",
                              "--wait-timeout", "180", "controller", "patient", "emergency", "admin")
            # Same observed starting state/rate, reached through real telemetry (no DB mutation).
            ready_deadline = time.monotonic()+60
            while True:
                status = request("GET", controller+"/admission/status", headers=operator)
                if status["state"] == "Normal" and status["rate_rps"] >= 1.999 and status["telemetry_fresh"]:
                    break
                assert time.monotonic() < ready_deadline, "Controller did not reach fresh Normal/2RPS"
                time.sleep(.5)
            self.output["initial_controller"] = status
            self.set_replay(config, True)
            start_wall = time.time()
            start = time.monotonic()
            self.output["recovery_start_at"] = datetime.fromtimestamp(start_wall, timezone.utc).isoformat()
            self.compose_call(self.project, config, "up", "-d", "--no-build", "--pull", "never", "--wait",
                              "--wait-timeout", "180", "hospital-a", "hospital-b")
            next_sample, next_feed = 0, {SITES[0]: 10., SITES[1]: 30.}
            sustained_snapshot = None
            with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
                while True:
                    elapsed = time.monotonic()-start
                    assert elapsed <= self.seconds+self.drain_seconds, "Bounded drain deadline reached"
                    if elapsed < next_sample:
                        time.sleep(min(.2, next_sample-elapsed))
                        continue
                    sample_started = time.monotonic()
                    applied_rows = conn.execute("SELECT event_id,site_id,applied_at FROM events ORDER BY applied_at,event_id").fetchall()
                    applied = {row["event_id"]: row for row in applied_rows}
                    counts = {site: sum(row["site_id"] == site for row in applied_rows) for site in SITES}
                    pending = {site: sum(e["site_id"] == site and e["event_id"] not in applied for e in expected.values()) for site in SITES}
                    status = request("GET", controller+"/admission/status", headers=operator)
                    tickets = conn.execute("SELECT hospital_id,event_id,priority,bypass_count,queue_position FROM controller_tickets ORDER BY queue_position").fetchall()
                    grants = conn.execute("SELECT hospital_id,event_id,status FROM controller_grants WHERE status IN ('issued','acknowledged','reconciliation_required') ORDER BY hospital_id,event_id").fetchall()
                    sample = {"elapsed_seconds": time.monotonic()-start, "sampled_at": base.utcnow(),
                        "completed": counts, "pending_cloud": pending,
                        "produced": {site: sum(e["site_id"] == site for e in expected.values()) for site in SITES},
                        "state": status["state"], "rate_rps": status["rate_rps"], "reason": status["reason"],
                        "telemetry_fresh": status["telemetry_fresh"], "allowed": status["allowed"], "denied": status["denied"],
                        "tickets": tickets, "active_grants": grants,
                        "sampling_duration_seconds": time.monotonic()-sample_started}
                    if not self.output["samples"] or elapsed >= self.output["samples"][-1].get("cloud_sample_elapsed", -60)+30:
                        sample["cloud"] = {name: request("GET", address+"/metrics") for name,address in cloud.items()}
                        sample["cloud_sample_elapsed"] = elapsed
                    else:
                        sample["cloud_sample_elapsed"] = self.output["samples"][-1].get("cloud_sample_elapsed", -60)
                    self.output["samples"].append(sample)
                    if elapsed >= self.seconds and sustained_snapshot is None:
                        sustained_snapshot = sample
                        self.output["sustained_end"] = sample
                        print(f"SUSTAINED {self.policy}{self.ordinal}: {elapsed:.1f}s completed={counts} pending={pending}", flush=True)
                    if elapsed < self.seconds:
                        phase = min(2, int(elapsed/(self.seconds/3)))
                        for site, interval, watermark, size in ((SITES[0],10,24,8),(SITES[1],30,32,24)):
                            if elapsed >= next_feed[site]:
                                scheduled = next_feed[site]
                                next_feed[site] += interval
                                if pending[site] < watermark:
                                    capture(site,size,phase,scheduled,start)
                    elif len(applied) == len(expected):
                        local = {site: request("GET", address+"/events") for site,address in hospitals.items()}
                        if all(all(row["status"] == "applied" for row in rows) for rows in local.values()):
                            break
                    next_sample = time.monotonic()-start+2
                    if len(self.output["samples"]) % 30 == 0:
                        self.save()
                    if len(self.output["samples"]) % 60 == 0:
                        print(f"PROGRESS {self.policy}{self.ordinal}: {elapsed:.0f}s A={counts[SITES[0]]} B={counts[SITES[1]]}",flush=True)

                events = conn.execute("SELECT * FROM events ORDER BY event_id").fetchall()
                grants = conn.execute("SELECT * FROM controller_grants ORDER BY original_queue_position,grant_id").fetchall()
                audit = conn.execute("SELECT a.*,g.hospital_id,g.event_id,g.priority,g.original_queue_position,g.original_bypass_count FROM controller_grant_audit a JOIN controller_grants g USING(grant_id) ORDER BY a.id").fetchall()
                tickets = conn.execute("SELECT * FROM controller_tickets ORDER BY queue_position").fetchall()
                pg_integrity = conn.execute("SELECT count(*) AS count,count(DISTINCT event_id) AS distinct_count FROM events").fetchone()
                server_time = conn.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
                actual = {row["event_id"]:row for row in events}
                assert set(actual) == set(expected) and len(events) == len(expected)
                assert all(row["status"] == "applied" for row in events)
                for event_id,event in expected.items():
                    assert all(actual[event_id][key] == value for key,value in event.items())
                    assert all(actual[parent]["applied_at"] <= actual[event_id]["applied_at"] for parent in event["prerequisite_ids"])
                completed = [g for g in grants if g["status"] == "completed"]
                assert len(completed) == len(expected) and {g["event_id"] for g in completed} == set(expected)
                assert not tickets and all(g["status"] in ("completed","superseded","expired","cancelled") for g in grants)
                assert all(g["original_bypass_count"] <= 1 for g in grants)

                # A completed grant is terminal: even identical transport
                # resubmissions must be fenced. Concurrent duplicate-write
                # idempotency while ACKed is covered by the focused PG tests.
                duplicate_checks=[]
                for site in SITES:
                    chosen=[event for event in expected.values() if event["site_id"]==site][:4]
                    token=settings["HOSPITAL_A_CONTROLLER_TOKEN" if site==SITES[0] else "HOSPITAL_B_CONTROLLER_TOKEN"]
                    for event in chosen:
                        grant=next(g for g in completed if g["event_id"]==event["event_id"])
                        response=request("POST",cloud[base.ROUTE[event["event_type"]]]+"/events",body=event,
                            headers={"Authorization":"Bearer "+token,"X-MetaGuard-Grant":str(grant["grant_id"])},allowed=(409,))
                        duplicate_checks.append({"event_id":event["event_id"],"http_status":409,"result":"completed_grant_fenced"})
                assert conn.execute("SELECT count(*) AS count FROM events").fetchone()["count"] == len(expected)
            sqlite={}
            probe=("import sqlite3,json; c=sqlite3.connect('/data/hospital.db'); c.row_factory=sqlite3.Row; "
                   "print(json.dumps({'integrity':c.execute('PRAGMA integrity_check').fetchone()[0],"
                   "'events':[dict(r) for r in c.execute('SELECT * FROM events ORDER BY event_id')],"
                   "'recovery':[dict(r) for r in c.execute('SELECT * FROM recovery_state ORDER BY event_id')]}))")
            for service,site in (("hospital-a",SITES[0]),("hospital-b",SITES[1])):
                data=json.loads(self.compose_capture(self.project,config,"exec","-T",service,"python","-c",probe))
                assert data["integrity"] == "ok"
                rows={row["event_id"]:row for row in data["events"]}
                assert set(rows)=={e["event_id"] for e in expected.values() if e["site_id"]==site}
                assert all(r["status"]=="applied" for r in rows.values())
                for event_id,row in rows.items():
                    expected_event=expected[event_id]
                    assert all(row[key]==expected_event[key] for key in ("site_id","event_type","base_priority"))
                    assert json.loads(row["prerequisite_ids"])==expected_event["prerequisite_ids"]
                    assert json.loads(row["payload"])==expected_event["payload"]
                assert all(not r["quarantined"] and not r["in_flight"] for r in data["recovery"])
                sqlite[site]=data
            self.output.update(result="passed",finished_at=base.utcnow(),duration_seconds=time.monotonic()-start,
                expected_events=list(expected.values()),postgres_events=events,grants=grants,audit=audit,
                sqlite=sqlite,duplicate_checks=duplicate_checks,postgres_integrity=pg_integrity,
                host_minus_postgres_clock_seconds=time.time()-server_time.timestamp(),
                integrity={"exact_event_set":True,"contents_equal":True,"dependency_order":True,
                           "one_completed_grant_per_event":True,"remaining_tickets":0,"max_recorded_bypass":max(g["original_bypass_count"] for g in grants),
                           "idempotency":True,"sqlite_integrity":"ok"})
            # Run only relevant real-PostgreSQL regression selections once, in
            # independently created schemas within this disposable database.
            if self.policy=="fixed" and self.ordinal==1:
                env=os.environ.copy()
                env["CONTROLLER_TEST_DATABASE_URL"]=url
                for key in tuple(env):
                    if key.startswith("CONTROLLER_ADMISSION_") or key in ("METAGUARD_ENABLED","METAGUARD_REQUIRED","CONTROLLER_HOSPITAL_TOKENS"):
                        env.pop(key,None)
                junit=HERE/f"cp7b-sustained-fairness-postgres-tests-{STAMP}.xml"
                result=subprocess.run([sys.executable,"-m","pytest","tests/integration/test_controller_postgres.py",
                    "tests/integration/test_metaguard_reliability.py","-k",
                    "fairness or sustained_priority or concurrent_identical or candidate_replacement or out_of_order or idempotent",
                    "-q","-p","no:cacheprovider","--basetemp="+str(self.work/"pytest-temp"),"--junitxml="+str(junit)],
                    cwd=ROOT,env=env,text=True,capture_output=True,timeout=120)
                (self.work/"pytest.log").write_text(result.stdout+result.stderr,encoding="utf-8")
                suites=ET.parse(junit).getroot().findall("testsuite")
                counts={k:sum(int(s.attrib.get(k,0)) for s in suites) for k in ("tests","failures","errors","skipped")}
                self.output["postgres_regressions"]=counts
                assert result.returncode==0 and not any(counts[k] for k in ("failures","errors","skipped"))
            print(f"PASS {self.policy}{self.ordinal}: {len(expected)} events {self.output['duration_seconds']:.1f}s",flush=True)
        except Exception as exc:
            self.output.update(result="failed",error=f"{type(exc).__name__}: {exc}",finished_at=base.utcnow())
            print(f"FAIL {self.policy}{self.ordinal}: {self.output['error']}",flush=True)
        finally:
            client.close()
            self.save()
            if config:
                try:
                    volumes=self.docker("volume","ls","-q","--filter","label=com.docker.compose.project="+self.project).splitlines()
                    assert not(set(volumes)&self.original_volumes)
                    self.compose_call(self.project,config,"down","--remove-orphans")
                    for volume in volumes:
                        metadata=json.loads(self.docker("volume","inspect",volume))[0]
                        assert metadata["Labels"]["com.docker.compose.project"]==self.project
                        self.docker("volume","rm",volume)
                    for resource in ("volume","network"):
                        assert not self.docker(resource,"ls","-q","--filter","label=com.docker.compose.project="+self.project)
                    assert not self.docker("ps","-aq","--filter","label=com.docker.compose.project="+self.project)
                    self.output["cleanup"]="passed"
                except Exception as exc:
                    self.output.update(result="failed",cleanup_error=f"{type(exc).__name__}: {exc}")
                finally:
                    config.unlink(missing_ok=True)
                    self.save()
        return self.output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials",type=int,default=3)
    parser.add_argument("--seconds",type=int,default=660)
    parser.add_argument("--drain-seconds",type=int,default=420)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    assert args.trials>=3 and args.seconds>=660
    assert not args.output.exists(),"Refuse to overwrite existing evidence"
    work=Path(tempfile.mkdtemp(prefix="cp7b-fairness-",dir=ROOT/".venv"))
    image="crisisguard-fairness-eval:"+uuid.uuid4().hex[:12]
    inventory=base.Runner(work,image,args.output,1)
    before,volumes=inventory.inventory()
    report={"schema":"cp7b-sustained-fairness-v1","started_at":base.utcnow(),"image":image,
        "design":{"trials_per_policy":args.trials,"sustained_arrival_seconds":args.seconds,
                  "maximum_drain_seconds":args.drain_seconds,"concurrency":2*args.trials,
                  "initial_backlog_per_site":32,"initial_controller":"fresh Normal at 2 RPS",
                  "arrival_A":"every 10s, add 8 events if Cloud-pending <24",
                  "arrival_B":"every 30s, add 24 events if Cloud-pending <32",
                  "priority_phases":"first third A urgent/B low; second third B urgent/A low; final third mixed",
                  "dependencies":"two-event chains; base-priority-1 roots inherit child urgency; three Cloud routes",
                  "controlled_comparison":"same workload generation rules and thresholds; only admission policy differs; adaptive replenishment permits different total counts",
                  "starvation_interpretation":"finite observation only; no indefinite wall-clock fairness claim"},
        "source_sha256":source_hashes(),"baseline_containers":before,"baseline_volumes":sorted(volumes),"trials":[]}
    try:
        inventory.run(["docker","build","-q","-t",image,"-f",str(ROOT/"infrastructure/docker/Dockerfile"),str(ROOT)],timeout=1200)
        # Compare every Python application source file to the built image.
        probe="import pathlib,hashlib,json;print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for d in ('cloud','edge','dashboard') for p in pathlib.Path(d).rglob('*.py') if '__pycache__' not in p.parts}))"
        built=json.loads(inventory.docker("run","--rm","--network","none","--entrypoint","python",image,"-c",probe))
        assert built==report["source_sha256"]
        report["image_source_match"]=True
        report["image_id"]=inventory.docker("image","inspect",image,"--format","{{.Id}}")
        with ThreadPoolExecutor(max_workers=2*args.trials) as pool:
            tasks=[pool.submit(FairnessTrial(work,image,policy,n,args.seconds,args.drain_seconds,volumes).execute_trial)
                   for n in range(1,args.trials+1) for policy in ("fixed","aimd")]
            for future in as_completed(tasks):
                report["trials"].append(future.result())
                args.output.write_text(json.dumps(report,indent=2,default=str),encoding="utf-8")
    finally:
        after,after_volumes=inventory.inventory()
        report["preservation"]={"container_identity_image_mounts_equal":before==after,
            "volume_set_equal":volumes==after_volumes,"containers":len(before),"volumes":len(volumes),
            "application_sources_equal":report["source_sha256"]==source_hashes()}
        report["finished_at"]=base.utcnow()
        report["result"]="passed" if (len(report["trials"])==2*args.trials and
            all(t["result"]=="passed" and t.get("cleanup")=="passed" for t in report["trials"]) and
            all(report["preservation"][k] for k in ("container_identity_image_mounts_equal","volume_set_equal","application_sources_equal"))) else "failed"
        report["trials"].sort(key=lambda t:(t["trial"],t["policy"]))
        args.output.write_text(json.dumps(report,indent=2,default=str),encoding="utf-8")
        print(f"FINAL {report['result']}; evidence={args.output}; private logs={work}",flush=True)
    if report["result"]!="passed":
        raise SystemExit(1)


if __name__=="__main__":
    main()
