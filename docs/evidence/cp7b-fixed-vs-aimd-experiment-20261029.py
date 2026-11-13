"""Run isolated, reproducible fixed-rate versus AIMD recovery trials.

Every trial uses a fresh Compose project and fresh volumes.  The original
desktop stack is inventoried but never addressed by a Compose command.
Secrets live only in the disposable per-trial resolved config and are never
written to the evidence report or command output.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
BASE_FILES = [
    ROOT / "compose.yaml",
    ROOT / "compose.override.yaml",
    ROOT / "infrastructure/docker/compose.metaguard.yaml",
]
SERVICES = ("patient", "emergency", "admin")
SITES = ("Hospital_A", "Hospital_B")
KINDS = ("patient_registration", "encounter_creation", "emergency_record", "patient_discharge")
ROUTE = {
    "patient_registration": "patient",
    "encounter_creation": "patient",
    "emergency_record": "emergency",
    "patient_discharge": "admin",
}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def percentile(values, fraction=.95):
    ordered = sorted(values)
    return ordered[max(0, (int(fraction * len(ordered) + .999999) - 1))] if ordered else None


def chain(prefix, site):
    events = []
    priorities = (2, 3, 5, 1)
    for index, (kind, priority) in enumerate(zip(KINDS, priorities), 1):
        event_id = f"{prefix}-{site}-{index}"
        events.append({
            "event_id": event_id,
            "site_id": site,
            "event_type": kind,
            "base_priority": priority,
            "prerequisite_ids": [events[-1]["event_id"]] if events else [],
            "payload": {"patient_id": f"SYNTHETIC-{prefix}-{site}", "sequence": index},
        })
    return events


class Runner:
    def __init__(self, work, image, raw_path, trials):
        self.work = work
        self.image = image
        self.raw_path = raw_path
        self.trials = trials
        self.log = work / "commands.log"
        self.report = {
            "schema": "cp7b-fixed-vs-aimd-v1",
            "started_at": utcnow(),
            "experiment": {
                "trials_per_policy": trials,
                "order": [policy for _ in range(trials) for policy in ("fixed", "aimd")],
                "events_per_hospital": 4,
                "fault": "Cloud and controller absent while the identical local backlogs are captured; all recovery services start at t=0",
                "controlled_difference": "CONTROLLER_ADMISSION_POLICY only",
                "controller": {
                    "normal_rps": 2.0, "caution_rps": .5, "recovery_rps": .2,
                    "normal_inflight": 4, "caution_inflight": 2, "recovery_inflight": 1,
                    "poll_seconds": 1, "max_age_seconds": 5, "timeout_seconds": 1,
                    "dwell_seconds": 3, "healthy_samples": 2, "additive_rps": .2,
                    "caution_latency_ms": 80, "recovery_latency_ms": 1000,
                    "healthy_latency_ms": 50, "recovery_error_rate": .15,
                },
                "measurement_boundaries": {
                    "middleware": "Cloud ASGI middleware duration for authenticated replay POST and reconciliation GET traffic in each process-local rolling window",
                    "client": "host-observed end-to-end duration of one authenticated replay status GET per applied event, measured after middleware snapshots",
                    "queueing": "Hospital durable pending-event trajectory plus AnyIO executor wait depth sampled by each Cloud /metrics endpoint",
                    "not_measured": "socket accept queues, upstream/network queues, PostgreSQL lock/connection queues",
                },
            },
            "image": image,
            "trials": [],
        }

    def run(self, args, *, env=None, capture=False, timeout=900):
        result = subprocess.run(args, cwd=ROOT, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        with self.log.open("a", encoding="utf-8") as output:
            output.write("COMMAND " + " ".join(str(x) for x in args[:4]) + " ...\n")
            output.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"Command failed ({result.returncode}): {args[:4]}; see {self.log}")
        return result.stdout.strip() if capture else ""

    def docker(self, *args):
        return self.run(["docker", *args], capture=True)

    def inventory(self):
        ids = self.docker("ps", "-aq").splitlines()
        inspected = json.loads(self.docker("inspect", *ids)) if ids else []
        containers = {row["Id"]: {
            "name": row["Name"], "image": row["Image"],
            "mounts": sorted((m["Type"], m.get("Name") or m.get("Source"), m["Destination"])
                             for m in row["Mounts"]),
        } for row in inspected}
        return containers, set(self.docker("volume", "ls", "-q").splitlines())

    def compose_call(self, project, config, *args):
        self.run(["docker", "compose", "-p", project, "-f", str(config), *args])

    def compose_capture(self, project, config, *args):
        return self.run(["docker", "compose", "-p", project, "-f", str(config), *args], capture=True)

    def api(self, method, url, *, headers=None, body=None, expected=200, timeout=10):
        response = httpx.request(method, url, headers=headers, json=body, timeout=timeout)
        if response.status_code != expected:
            raise AssertionError((method, url, response.status_code, response.text[:500]))
        return response.json()

    def resolve(self, project, trial_dir, settings):
        env_path = trial_dir / "trial.env"
        config_path = trial_dir / "compose.json"
        env_path.write_text("".join(f"{key}={value}\n" for key, value in settings.items()), encoding="utf-8")
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("COMPOSE_") or key in settings:
                environment.pop(key, None)
        command = ["docker", "compose", "--env-file", str(env_path), "-p", project]
        for path in BASE_FILES:
            command += ["-f", str(path)]
        command += ["--profile", "metaguard", "config", "--format", "json"]
        resolved = json.loads(self.run(command, env=environment, capture=True))
        resolved["services"] = {name: resolved["services"][name] for name in
                                ("postgres", "controller", "patient", "emergency", "admin", "hospital-a", "hospital-b")}
        for name, service in resolved["services"].items():
            # The resolved file is itself the complete test definition.  Remove
            # the source profile marker so `down` always includes controller.
            service.pop("profiles", None)
            if name != "postgres":
                service["image"] = self.image
                service["pull_policy"] = "never"
                service.pop("build", None)
        for volume in resolved["volumes"].values():
            assert not volume.get("external") and volume["name"].startswith(project + "_")
        for network in resolved["networks"].values():
            assert not network.get("external") and network["name"].startswith(project + "_")
        config_path.write_text(json.dumps({key: value for key, value in resolved.items()
                                           if not key.startswith("x-")}), encoding="utf-8")
        env_path.unlink()
        self.compose_call(project, config_path, "config", "--quiet")
        return config_path, resolved

    def set_replay(self, config_path, enabled):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for service in ("hospital-a", "hospital-b"):
            config["services"][service]["environment"]["AUTO_REPLAY"] = "1" if enabled else "0"
        config_path.write_text(json.dumps(config), encoding="utf-8")

    def trial(self, policy, ordinal, original_volumes):
        project = f"cg-policy-{policy}-{ordinal}-{uuid.uuid4().hex[:8]}"
        trial_dir = self.work / project
        trial_dir.mkdir()
        ports = dict(zip(("POSTGRES_PORT", "CONTROLLER_PORT", "PATIENT_PORT", "EMERGENCY_PORT",
                          "ADMIN_PORT", "HOSPITAL_A_PORT", "HOSPITAL_B_PORT", "DASHBOARD_PORT"),
                         map(str, free_ports(8))))
        replay_token = secrets.token_hex(32)
        settings = {
            **ports,
            "POSTGRES_USER": "cp7b_eval", "POSTGRES_DB": "cp7b_eval",
            "POSTGRES_PASSWORD": secrets.token_hex(24),
            "CONTROLLER_CLOUD_TOKEN": secrets.token_hex(32),
            "HOSPITAL_A_CONTROLLER_TOKEN": secrets.token_hex(32),
            "HOSPITAL_B_CONTROLLER_TOKEN": secrets.token_hex(32),
            "CLOUD_LIVE_TELEMETRY_TOKEN": secrets.token_hex(32),
            "CLOUD_REPLAY_TELEMETRY_TOKEN": replay_token,
            "AUTO_REPLAY": "0", "AUTO_REPLAY_INTERVAL_SECONDS": "0.5",
            "ENABLE_MANUAL_REPLAY": "0", "MANUAL_REPLAY_TOKEN": "",
            "CONTROLLER_ADMISSION_POLICY": policy,
            "CONTROLLER_ADMISSION_ENABLED": "1",
            "CONTROLLER_ADMISSION_POLL_SECONDS": "1",
            "CONTROLLER_ADMISSION_MAX_AGE_SECONDS": "5",
            "CONTROLLER_ADMISSION_TIMEOUT_SECONDS": "1",
            "CONTROLLER_ADMISSION_DWELL_SECONDS": "3",
            "CONTROLLER_ADMISSION_HEALTHY_SAMPLES": "2",
            "CONTROLLER_ADMISSION_ADDITIVE_RPS": "0.2",
            "CONTROLLER_ADMISSION_NORMAL_RPS": "2",
            "CONTROLLER_ADMISSION_CAUTION_RPS": "0.5",
            "CONTROLLER_ADMISSION_RECOVERY_RPS": "0.2",
            "CONTROLLER_ADMISSION_NORMAL_INFLIGHT": "4",
            "CONTROLLER_ADMISSION_CAUTION_INFLIGHT": "2",
            "CONTROLLER_ADMISSION_RECOVERY_INFLIGHT": "1",
            "CONTROLLER_ADMISSION_CAUTION_LATENCY_MS": "80",
            "CONTROLLER_ADMISSION_RECOVERY_LATENCY_MS": "1000",
            "CONTROLLER_ADMISSION_HEALTHY_LATENCY_MS": "50",
            "CONTROLLER_ADMISSION_RECOVERY_ERROR_RATE": "0.15",
        }
        output = {
            "policy": policy, "ordinal": ordinal, "project": project,
            "started_at": utcnow(), "result": "running", "samples": [],
        }
        config_path = None
        try:
            config_path, resolved = self.resolve(project, trial_dir, settings)
            assert resolved["services"]["controller"]["environment"]["CONTROLLER_ADMISSION_POLICY"] == policy
            assert all(resolved["services"][name]["environment"]["AUTO_REPLAY"] == "0"
                       for name in ("hospital-a", "hospital-b"))
            self.compose_call(project, config_path, "up", "-d", "--no-build", "--pull", "never",
                              "--wait", "--wait-timeout", "180", "postgres", "hospital-a", "hospital-b")
            prefix = f"EVAL-{policy}-{ordinal}"
            chains = {site: chain(prefix, site) for site in SITES}
            hospital_ports = dict(zip(SITES, (int(ports["HOSPITAL_A_PORT"]), int(ports["HOSPITAL_B_PORT"]))))
            capture_times = {}
            for site in SITES:
                for event in chains[site]:
                    capture_times[event["event_id"]] = time.time()
                    self.api("POST", f"http://127.0.0.1:{hospital_ports[site]}/capture", body=event, expected=201)
                rows = self.api("GET", f"http://127.0.0.1:{hospital_ports[site]}/events")
                assert len(rows) == 4 and all(row["status"] == "pending" for row in rows)
            self.compose_call(project, config_path, "stop", "hospital-a", "hospital-b")
            self.set_replay(config_path, True)
            start_wall = time.time()
            start_mono = time.monotonic()
            self.compose_call(project, config_path, "up", "-d", "--no-build", "--pull", "never",
                              "--wait", "--wait-timeout", "180", "controller", "patient", "emergency", "admin",
                              "hospital-a", "hospital-b")
            controller_url = f"http://127.0.0.1:{ports['CONTROLLER_PORT']}"
            operator = {"X-Cloud-Token": settings["CONTROLLER_CLOUD_TOKEN"]}
            cloud_urls = {name: f"http://127.0.0.1:{ports[name.upper() + '_PORT']}" for name in SERVICES}
            completion = {}
            deadline = time.monotonic() + 150
            last_sample = 0.0
            all_applied_at = None
            while time.monotonic() < deadline:
                elapsed = time.monotonic() - start_mono
                if elapsed - last_sample >= .25:
                    last_sample = elapsed
                    sample = {"elapsed_seconds": round(elapsed, 6), "sampled_at": utcnow()}
                    try:
                        status = self.api("GET", controller_url + "/admission/status", headers=operator)
                        sample["controller"] = {key: status.get(key) for key in
                                                ("state", "reason", "rate_rps", "telemetry_fresh", "allowed", "denied", "last_decision")}
                    except Exception as exc:
                        sample["controller_error"] = type(exc).__name__
                    sample["cloud"] = {}
                    for name, url in cloud_urls.items():
                        try:
                            metric = self.api("GET", url + "/metrics")
                            sample["cloud"][name] = {key: metric.get(key) for key in (
                                "sampled_at", "p95_latency_ms", "average_latency_ms", "throughput_rps",
                                "error_rate", "latency_sample_count", "replay_latency_sample_count",
                                "executor_queue_depth", "executor_queue_pressure")}
                        except Exception as exc:
                            sample["cloud"][name] = {"error": type(exc).__name__}
                    pending_total = 0
                    sample["hospitals"] = {}
                    for site, port in hospital_ports.items():
                        try:
                            rows = self.api("GET", f"http://127.0.0.1:{port}/events")
                            status_map = {row["event_id"]: row["status"] for row in rows}
                            pending = sum(value == "pending" for value in status_map.values())
                            pending_total += pending
                            sample["hospitals"][site] = {"pending": pending, "applied": len(rows) - pending}
                            for event_id, event_status in status_map.items():
                                if event_status == "applied" and event_id not in completion:
                                    completion[event_id] = {"elapsed_seconds": round(elapsed, 6), "observed_at": utcnow()}
                        except Exception as exc:
                            sample["hospitals"][site] = {"error": type(exc).__name__}
                            pending_total += 99
                    output["samples"].append(sample)
                    if pending_total == 0 and len(completion) == 8:
                        all_applied_at = elapsed
                        break
                time.sleep(.05)
            if all_applied_at is None:
                raise RuntimeError("Timed out before all eight events were applied")

            # Snapshot middleware populations before deliberately adding client status reads.
            middleware = {name: self.api("GET", url + "/metrics") for name, url in cloud_urls.items()}
            client_reads = []
            replay_header = {"Authorization": "Bearer " + replay_token}
            for site in SITES:
                for event in chains[site]:
                    url = cloud_urls[ROUTE[event["event_type"]]] + "/events/" + event["event_id"]
                    before = time.perf_counter()
                    body = self.api("GET", url, headers=replay_header)
                    duration = (time.perf_counter() - before) * 1000
                    assert body["event_id"] == event["event_id"] and body["status"] == "applied"
                    client_reads.append({"event_id": event["event_id"], "service": ROUTE[event["event_type"]],
                                         "latency_ms": duration})

            db_url = (f"postgresql://{settings['POSTGRES_USER']}:{settings['POSTGRES_PASSWORD']}"
                      f"@127.0.0.1:{ports['POSTGRES_PORT']}/{settings['POSTGRES_DB']}")
            with psycopg.connect(db_url, row_factory=dict_row) as conn:
                events = conn.execute("SELECT event_id,site_id,event_type,base_priority,prerequisite_ids,payload,status,applied_at FROM events ORDER BY event_id").fetchall()
                grants = conn.execute("SELECT hospital_id,event_id,status,acknowledged_at,completed_at FROM controller_grants ORDER BY event_id,grant_id").fetchall()
                tickets = conn.execute("SELECT hospital_id,event_id,priority FROM controller_tickets ORDER BY hospital_id").fetchall()
                audit = conn.execute("SELECT g.hospital_id,g.event_id,a.action,a.created_at FROM controller_grant_audit a JOIN controller_grants g USING(grant_id) ORDER BY a.id").fetchall()
            expected = {event["event_id"]: event for site in SITES for event in chains[site]}
            actual = {row["event_id"]: row for row in events}
            assert len(events) == len(actual) == 8 and set(actual) == set(expected)
            assert all(row["status"] == "applied" for row in events)
            assert len(grants) == 8 and all(row["status"] == "completed" for row in grants) and not tickets
            for event_id, row in actual.items():
                event = expected[event_id]
                assert all(row[key] == event[key] for key in
                           ("site_id", "event_type", "base_priority", "prerequisite_ids", "payload"))
            applied_times = {row["event_id"]: row["applied_at"].timestamp() for row in events}
            for event in expected.values():
                assert all(applied_times[parent] <= applied_times[event["event_id"]]
                           for parent in event["prerequisite_ids"])

            final_status = self.api("GET", controller_url + "/admission/status", headers=operator)
            hospital_journals = {}
            sqlite_probe = ("import json,sqlite3; c=sqlite3.connect('/data/hospital.db'); "
                            "print(json.dumps({'retry_count_total':c.execute('SELECT coalesce(sum(retry_count),0) FROM recovery_state').fetchone()[0],"
                            "'retry_count_max':c.execute('SELECT coalesce(max(retry_count),0) FROM recovery_state').fetchone()[0],"
                            "'attempt_phases':c.execute('SELECT phase,count(*) FROM metaguard_attempts GROUP BY phase ORDER BY phase').fetchall()}))")
            for service, site in (("hospital-a", "Hospital_A"), ("hospital-b", "Hospital_B")):
                hospital_journals[site] = json.loads(self.compose_capture(
                    project, config_path, "exec", "-T", service, "python", "-c", sqlite_probe))
            output.update({
                "result": "passed",
                "recovery_start_at": datetime.fromtimestamp(start_wall, timezone.utc).isoformat(),
                "recovery_completion_seconds": all_applied_at,
                "event_completion": completion,
                "site_completion_seconds": {site: max(completion[e["event_id"]]["elapsed_seconds"] for e in chains[site]) for site in SITES},
                "middleware_metrics": {name: {key: metric.get(key) for key in (
                    "p95_latency_ms", "average_latency_ms", "throughput_rps", "error_rate",
                    "latency_sample_count", "replay_latency_sample_count", "failed_requests",
                    "executor_queue_depth", "executor_queue_pressure", "observation_seconds")}
                    for name, metric in middleware.items()},
                "client_status_reads": client_reads,
                "client_status_read_p95_ms": percentile([row["latency_ms"] for row in client_reads]),
                "controller_final": {key: final_status.get(key) for key in
                                     ("state", "reason", "rate_rps", "telemetry_fresh", "allowed", "denied")},
                "hospital_journals": hospital_journals,
                "integrity": {
                    "expected_events": 8, "postgres_events": len(events), "unique_event_ids": len(actual),
                    "completed_grants": sum(row["status"] == "completed" for row in grants),
                    "remaining_tickets": len(tickets), "same_eligible_event_set": True,
                    "dependency_order_preserved": True, "loss_or_duplication": False,
                },
                "grant_audit": [{**row, "created_at": row["created_at"].isoformat()} for row in audit],
                "finished_at": utcnow(),
            })
            print(f"PASS {policy} trial {ordinal}: {all_applied_at:.3f}s", flush=True)
            return output
        except Exception as exc:
            output.update(result="failed", error=f"{type(exc).__name__}: {exc}", finished_at=utcnow())
            raise
        finally:
            self.report["trials"].append(output)
            self.raw_path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")
            if config_path and config_path.exists():
                try:
                    volumes = self.docker("volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project).splitlines()
                    assert not (set(volumes) & original_volumes)
                    self.compose_call(project, config_path, "down", "--remove-orphans")
                    for volume in volumes:
                        metadata = json.loads(self.docker("volume", "inspect", volume))[0]
                        assert metadata["Labels"]["com.docker.compose.project"] == project
                        self.docker("volume", "rm", volume)
                    assert not self.docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + project)
                    assert not self.docker("volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project)
                    assert not self.docker("network", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project)
                    output["cleanup"] = "passed"
                finally:
                    config_path.unlink(missing_ok=True)
                    self.raw_path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def execute(self):
        original_containers, original_volumes = self.inventory()
        self.report["original_resource_baseline"] = {
            "containers": len(original_containers), "volumes": len(original_volumes),
            "container_ids": sorted(original_containers), "volume_names": sorted(original_volumes),
        }
        self.run(["docker", "build", "-q", "-t", self.image, "-f",
                  str(ROOT / "infrastructure/docker/Dockerfile"), str(ROOT)], timeout=1200)
        try:
            for ordinal in range(1, self.trials + 1):
                for policy in ("fixed", "aimd"):
                    self.trial(policy, ordinal, original_volumes)
        finally:
            current_containers, current_volumes = self.inventory()
            preserved = current_containers == original_containers and original_volumes <= current_volumes
            self.report["original_resources_preserved"] = {
                "result": "passed" if preserved else "failed",
                "container_identity_image_mounts_equal": current_containers == original_containers,
                "all_original_volumes_present": original_volumes <= current_volumes,
                "containers": len(original_containers), "volumes": len(original_volumes),
                "retained_disposable_image": self.image,
            }
            self.report["finished_at"] = utcnow()
            self.report["result"] = "passed" if preserved and len(self.report["trials"]) == 2 * self.trials and all(
                row["result"] == "passed" and row.get("cleanup") == "passed" for row in self.report["trials"]) else "failed"
            self.raw_path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")
        if self.report["result"] != "passed":
            raise RuntimeError("Experiment or preservation verification failed; inspect raw evidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.trials < 3:
        parser.error("at least three trials per policy are required")
    if args.output.exists():
        parser.error("output already exists")
    work = Path(tempfile.mkdtemp(prefix="cp7b-policy-eval-", dir=ROOT / ".venv"))
    image = "crisisguard-policy-eval:" + uuid.uuid4().hex[:12]
    runner = Runner(work, image, args.output.resolve(), args.trials)
    try:
        runner.execute()
    finally:
        print("Raw evidence: " + str(args.output.resolve()), flush=True)
        print("Private disposable command log: " + str(work), flush=True)


if __name__ == "__main__":
    main()
