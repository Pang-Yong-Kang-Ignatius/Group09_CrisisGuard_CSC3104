"""Test fresh Docker deployments without using existing projects or volumes.

Run from any directory: python infrastructure/scripts/smoke_deployment.py
Requires Docker Engine/Compose; Python standard library only.
All generated test containers, networks, volumes and the test image are removed.
"""

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.request import Request, urlopen

# Script entry points place this directory on sys.path; package imports support pytest.
if __package__:
    from . import database_backup
else:
    import database_backup


ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infrastructure" / "docker"
REPLAY = (
    "import json; from edge.capture_api.scheduled_replay import "
    "replay_scheduled_events; print(json.dumps(replay_scheduled_events()))"
)
EVENT_TYPES = ("patient_registration", "encounter_creation", "emergency_record", "patient_discharge")
SERVICE_NAMES = ("patient", "emergency", "admin")
HOSPITAL_NAMES = ("hospital-a", "hospital-b")


def run(args, *, env=None, capture=False):
    result = subprocess.run(
        args, cwd=ROOT, env=env, text=True, check=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def free_ports(count):
    # Reserve simultaneously so all selected ports are distinct.
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


def request(port, path, payload=None, expected=200, *, timeout=15):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(f"http://127.0.0.1:{port}{path}", data=data,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        check(response.status == expected, f"Unexpected HTTP status: {response.status}")
        return json.load(response)


class PeerNetwork:
    """A fresh private test bridge; remove only the ID created by this run."""
    def __init__(self, name):
        self.name = name
        self.id = None

    def create(self):
        self.id = run(["docker", "network", "create", "--internal", "--label",
                       "crisisguard.test-network=" + self.name, self.name], capture=True)

    def remove(self):
        if self.id:
            run(["docker", "network", "rm", self.id])
            self.id = None


class Project:
    def __init__(self, directory, name, compose_file, settings, image, services, *,
                 peer_network=None, peer_aliases=None):
        self.name = name
        # Isolate interpolation from the user's shell and root .env.
        self.env = os.environ.copy()
        for key in settings:
            self.env.pop(key, None)
        self.env.pop("COMPOSE_PROFILES", None)
        env_file = directory / f"{name}.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in settings.items()))
        override = {"services": {s: {"image": image} for s in services}}
        if peer_network:
            override["networks"] = {"smoke-peers": {"external": True, "name": peer_network}}
            for service in services:
                alias = (peer_aliases or {}).get(service, service)
                override["services"][service]["networks"] = {
                    "default": {}, "smoke-peers": {"aliases": [alias]}}
        if "dashboard" in services and compose_file.name == "compose.cloud.yaml":
            check(peer_network, "A split dashboard requires the private test peer network")
            # A test-only dashboard on the split cloud network. Production split
            # and AWS Compose files stay unchanged. Cross-project traffic uses
            # private container DNS, not Docker Desktop's host-loopback forwarding.
            override["services"]["dashboard"].update({
                "command": ["python", "-m", "uvicorn", "dashboard.main:app",
                            "--host", "0.0.0.0", "--port", "8080"],
                "environment": {
                    "DATABASE_URL": f"postgresql://{settings['POSTGRES_USER']}:{settings['POSTGRES_PASSWORD']}@postgres:5432/{settings['POSTGRES_DB']}",
                    **{f"DASHBOARD_{name.upper()}_URL": f"http://{name}:8000" for name in SERVICE_NAMES},
                    **{f"DASHBOARD_HOSPITAL_{suffix}_URL": f"http://hospital-{suffix.lower()}:8000"
                       for suffix in ("A", "B")},
                },
                "ports": [f"127.0.0.1:{settings['DASHBOARD_PORT']}:8080"],
                "healthcheck": {
                    "test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"],
                    "interval": "5s", "timeout": "3s", "retries": 12,
                },
            })
        override_file = directory / f"{name}.json"
        override_file.write_text(json.dumps(override))
        self.args = ["docker", "compose", "--project-name", name,
                     "--env-file", str(env_file), "-f", str(compose_file),
                     "-f", str(override_file)]
        self.checker_args = ["--env-file", str(env_file), "--project-name", name,
                             "--compose-file", str(compose_file), "--compose-file", str(override_file)]

    def call(self, *args, capture=False):
        return run(self.args + list(args), env=self.env, capture=capture)

    def start(self, recreate=False):
        args = ["up", "-d", "--no-build", "--wait", "--wait-timeout", "120"]
        if recreate:
            args.append("--force-recreate")
        self.call(*args)


def event_chain(run_id, layout, site):
    events = []
    for index, kind in enumerate(EVENT_TYPES, 1):
        events.append({
            "event_id": f"{run_id}_{layout}_{site}_{index}", "site_id": site,
            "event_type": kind, "base_priority": 5 if kind == "emergency_record" else 1,
            "prerequisite_ids": [events[-1]["event_id"]] if events else [],
            "payload": {"patient_id": f"SYNTHETIC_{run_id}_{site}"},
        })
    return events


def check_records(rows, expected, status):
    """Check complete event content and reject extra/mixed-site queue records."""
    actual = {row["event_id"]: row for row in rows}
    check(len(actual) == len(rows) == len(expected)
          and set(actual) == {event["event_id"] for event in expected},
          "Unexpected event IDs/count; possible missing data or site isolation failure")
    for event in expected:
        row = actual[event["event_id"]]
        check(row["status"] == status and all(row.get(k) == v for k, v in event.items()),
              f"Event content/status mismatch: {event['event_id']}")


def wait_for_recovery(port, reachable, backlog, *, drained=False, timeout=60,
                      expected_events=None):
    """Poll actual recovery state with a bound; include the last state on failure."""
    deadline = time.monotonic() + timeout
    event_statuses = None
    while True:
        state = request(port, "/recovery/status")
        if (state.get("enabled") is True and state.get("cloud_reachable") is reachable
                and state.get("service_health") == dict.fromkeys(SERVICE_NAMES, reachable)
                and state.get("replayable_backlog") == backlog
                and state.get("last_error") is None
                and (not drained or (state.get("last_reconnected_at")
                                     and state.get("drain_completed_at")))):
            if expected_events is None:
                return state
            # Recovery telemetry is sampled asynchronously and may still describe
            # the empty queue before capture. Verify the current durable records.
            rows = request(port, "/events")
            event_statuses = {row["event_id"]: row["status"] for row in rows}
            if all(row["status"] == "applied" for row in rows):
                check_records(rows, expected_events, "applied")
                return state
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for automatic recovery on port {port}: {state}; "
                               f"event_statuses={event_statuses}")
        time.sleep(0.5)


def cloud_records(cloud):
    return json.loads(cloud.call(
        "exec", "-T", "postgres", "psql", "-X", "-U", "crisisguard", "-d", "crisisguard",
        "-v", "ON_ERROR_STOP=1", "-tAc",
        "SELECT coalesce(json_agg(e ORDER BY e.event_id), '[]'::json) FROM public.events e",
        capture=True))


def check_application_order(rows):
    applied = {row["event_id"]: datetime.fromisoformat(row["applied_at"]) for row in rows}
    for row in rows:
        check(all(applied[parent] <= applied[row["event_id"]] for parent in row["prerequisite_ids"]),
              f"Dependent event applied before its prerequisite: {row['event_id']}")


def check_dashboard_views(services, hospitals, history, chains, status, reachable):
    check(set(services) == set(SERVICE_NAMES), "Dashboard cloud service list mismatch")
    for name in SERVICE_NAMES:
        result = services[name]
        check(result.get("service") == name, "Dashboard service identity mismatch")
        if reachable:
            check(result.get("http_status") == 200
                  and result.get("response", {}).get("status") == "healthy"
                  and result["response"].get("service") == name,
                  f"Dashboard cannot reach healthy {name} API")
        else:
            # The dashboard's outer HTTP response is still 200 when forwarding
            # fails. Inspect the inner result instead of accepting a false pass.
            check(result.get("http_status") is None
                  and result.get("response", {}).get("code") == "SERVICE_UNREACHABLE",
                  f"Dashboard did not report the {name} API outage")
    check(len(chains) == 2 and set(hospitals) == set(HOSPITAL_NAMES),
          "Dashboard hospital list mismatch")
    for name, chain in zip(HOSPITAL_NAMES, chains):
        hospital = hospitals[name]
        check(hospital.get("status") == "healthy", f"Dashboard cannot reach {name}")
        check_records(hospital["events"], chain, status)
        check(hospital.get("total") == len(chain)
              and hospital.get("pending") == (len(chain) if status == "pending" else 0)
              and hospital.get("applied") == (len(chain) if status == "applied" else 0),
              f"Dashboard hospital counts mismatch: {name}")
    expected = [event for chain in chains for event in chain] if status == "applied" else []
    check(history.get("count") == len(expected), "Dashboard history count mismatch")
    check_records(history["events"], expected, "applied")
    for row in history["events"]:
        datetime.fromisoformat(row["applied_at"])


def check_dashboard_metrics(metrics, active_services):
    check(set(metrics) == set(SERVICE_NAMES), "Dashboard metrics service list mismatch")
    for name in SERVICE_NAMES:
        result = metrics[name]
        data = result.get("response", {})
        check(result.get("service") == name and result.get("http_status") == 200
              and data.get("service") == name and data.get("database_healthy") is True,
              f"Dashboard metrics unavailable or misrouted: {name}")
        for key in ("total_requests", "failed_requests", "window_requests"):
            check(type(data.get(key)) is int and data[key] >= 0, f"Invalid dashboard metric: {name}.{key}")
        check(data["failed_requests"] <= data["total_requests"]
              and data["window_requests"] <= data["total_requests"], "Inconsistent dashboard counters")
        for key in ("average_latency_ms", "throughput_rps", "error_rate"):
            value = data.get(key)
            check(type(value) in (int, float) and math.isfinite(value) and value >= 0,
                  f"Invalid dashboard metric: {name}.{key}")
        check(data["error_rate"] <= 1 and data.get("window_seconds") == 60,
              "Invalid dashboard metrics window/error rate")
        if name in active_services:
            check(data["total_requests"] > 0, f"Dashboard metrics did not observe {name} event traffic")


def verify_dashboard(port, chains, status, *, reachable=True, assets=False, event_traffic=False):
    check(request(port, "/health") == {"status": "ok", "service": "dashboard"},
          "Dashboard health check failed")
    if assets:
        for path, media_type in (("/", "text/html"), ("/static/app.js", "javascript"),
                                 ("/static/style.css", "text/css")):
            with urlopen(f"http://127.0.0.1:{port}{path}", timeout=15) as response:
                body = response.read()
                check(response.status == 200 and media_type in response.headers.get_content_type()
                      and bool(body), f"Dashboard page/static asset missing: {path}")
                if path == "/":
                    check(b"CrisisGuard" in body, "Dashboard HTML was replaced by a fallback response")
    services = request(port, "/api/services", timeout=35)
    hospitals = request(port, "/api/hospitals")
    history = request(port, "/api/history?limit=100")
    check_dashboard_views(services, hospitals, history, chains, status, reachable)
    if reachable:
        active = {"patient" if event["event_type"] in EVENT_TYPES[:2] else
                  "emergency" if event["event_type"] == "emergency_record" else "admin"
                  for chain in chains for event in chain} if status == "applied" and event_traffic else set()
        check_dashboard_metrics(request(port, "/api/metrics"), active)
    print(f"CHECK dashboard: APIs={'healthy' if reachable else 'unreachable'}, "
          f"{sum(map(len, chains))} {status} hospital events, {history['count']} cloud history rows", flush=True)


def verify_health_checker(cloud, hospitals, events, directory, *, automatic):
    """Exercise the reusable EC2 checker against disposable real Docker services."""
    manifest = directory / (cloud.name + '-expected.json')
    manifest.write_text(json.dumps(events), encoding='utf-8')
    targets = [(cloud, 'cloud', None)] + [(project, 'hospital', service) for project, service, _, _ in hospitals]
    for project, role, service in targets:
        args = [sys.executable, str(ROOT / 'infrastructure/scripts/verify_aws_recovery.py'),
                role, *project.checker_args, '--expect-events', str(manifest)]
        if service:
            args += ['--hospital-service', service, '--require-drained']
            if not automatic:
                args.append('--allow-manual-replay')
        report = json.loads(run(args, env=project.env, capture=True))
        expected_count = len(events) if role == 'cloud' else len(events) // 2
        check(report['result'] == 'passed' and len(report.get('verified_events') or []) == expected_count,
              'Reusable health checker did not verify the expected records')
        print(f"CHECK reusable health: {role}/{service or 'patient'}, {expected_count} full records verified", flush=True)


def verify_automatic_recovery(layout, cloud, hospitals, projects, ports, run_id, dashboard_port, directory):
    for _, _, port, _ in hospitals:
        wait_for_recovery(port, True, 0)

    cloud.call("stop", "patient", "emergency", "admin")
    chains = [event_chain(run_id, layout, site) for _, _, _, site in hospitals]
    for (_, _, port, _), chain in zip(hospitals, chains):
        wait_for_recovery(port, False, 0)
        for event in chain:
            check(request(port, "/capture", event, 201)["status"] == "pending", "Capture failed")
        check_records(request(port, "/events"), chain, "pending")
        wait_for_recovery(port, False, len(chain))
    check(cloud_records(cloud) == [], "Cloud accepted records during its API outage")
    verify_dashboard(dashboard_port, chains, "pending", reachable=False)

    # Recreate only hospitals; --no-deps keeps all cloud APIs stopped in the
    # combined layout too. Automatic replay cannot drain the queue at this point.
    for project, service, _, _ in hospitals:
        project.call("up", "-d", "--no-build", "--no-deps", "--force-recreate",
                     "--wait", "--wait-timeout", "120", service)
    for (_, _, port, _), chain in zip(hospitals, chains):
        wait_for_recovery(port, False, len(chain))
        check_records(request(port, "/events"), chain, "pending")
    check(cloud_records(cloud) == [], "Cloud records appeared before API restoration")
    verify_dashboard(dashboard_port, chains, "pending", reachable=False)

    # Never execute REPLAY or call /replay in this scenario. The background loop
    # must notice reconnection and deliver each event to its configured service.
    cloud.call("up", "-d", "--no-build", "--wait", "--wait-timeout", "120",
               "patient", "emergency", "admin")
    recovery = {}
    for (_, _, port, site), chain in zip(hospitals, chains):
        state = wait_for_recovery(port, True, 0, drained=True)
        check_records(request(port, "/events"), chain, "applied")
        recovery[site] = {key: state[key] for key in
                          ("last_reconnected_at", "drain_completed_at", "replayable_backlog")}

    events = [event for chain in chains for event in chain]
    before = cloud_records(cloud)
    check_records(before, events, "applied")
    check_application_order(before)
    verify_dashboard(dashboard_port, chains, "applied", event_traffic=True)
    service_ports = dict(zip(EVENT_TYPES, (ports[0], ports[0], ports[1], ports[2])))
    for event in events:
        port = service_ports[event["event_type"]]
        stored = request(port, "/events/" + event["event_id"])
        check(all(stored.get(key) == event[key] for key in ("event_id", "site_id", "event_type"))
              and stored.get("status") == "applied", "Cloud lookup mismatch")
        duplicate = request(port, "/events", event)
        check(duplicate.get("result") == "already_applied", "Duplicate was not acknowledged")
    check(cloud_records(cloud) == before, "Duplicate retries changed cloud rows")

    for project in projects:
        project.start(recreate=True)
    for (_, _, port, _), chain in zip(hospitals, chains):
        wait_for_recovery(port, True, 0)
        check_records(request(port, "/events"), chain, "applied")
    check(cloud_records(cloud) == before, "Applied cloud records changed after recreation")
    verify_dashboard(dashboard_port, chains, "applied")
    verify_health_checker(cloud, hospitals, events, directory, automatic=True)
    print(f"PASS {layout} automatic: 8 events captured during API outage, pending queues survived "
          "hospital recreation, automatic three-service recovery without manual replay, dependency "
          "order, duplicate ACKs, applied persistence, dashboard and reusable health checker; " + json.dumps(recovery), flush=True)


def verify_layout(layout, directory, image, run_id, *, automatic=False):
    a_port, b_port, patient, emergency, admin, dashboard_port, postgres_port = free_ports(7)
    common = {
        "POSTGRES_USER": "crisisguard", "POSTGRES_PASSWORD": "smoke_local_only",
        "POSTGRES_DB": "crisisguard", "POSTGRES_PORT": postgres_port, "PATIENT_PORT": patient,
        "EMERGENCY_PORT": emergency, "ADMIN_PORT": admin,
        "CLOUD_BIND_ADDRESS": "127.0.0.1",
        # Always explicit: shell/root .env overrides must not enable replay in
        # the manual test or disable it in the automatic test.
        "AUTO_REPLAY": "1" if automatic else "0", "AUTO_REPLAY_INTERVAL_SECONDS": "1",
        "ENABLE_MANUAL_REPLAY": "0", "MANUAL_REPLAY_TOKEN": "",
        "DASHBOARD_PORT": dashboard_port, "HOSPITAL_A_PORT": a_port, "HOSPITAL_B_PORT": b_port,
    }
    projects = []
    peers = None
    try:
        if layout == "combined":
            settings = dict(common)
            cloud = Project(directory, f"{run_id}-local", ROOT / "compose.yaml",
                            settings, image,
                            ["patient", "emergency", "admin", "hospital-a", "hospital-b", "dashboard"])
            projects.append(cloud)
            hospitals = [(cloud, "hospital-a", a_port, "Hospital_A"),
                         (cloud, "hospital-b", b_port, "Hospital_B")]
        else:
            peers = PeerNetwork(f"{run_id}-peers")
            peers.create()
            name = f"{run_id}-cloud"
            cloud = Project(directory, name, DOCKER / "compose.cloud.yaml",
                            dict(common, CLOUD_PROJECT=name), image,
                            ["patient", "emergency", "admin", "dashboard"], peer_network=peers.name)
            projects.append(cloud)
            hospitals = []
            for suffix, port, site in [("a", a_port, "Hospital_A"), ("b", b_port, "Hospital_B")]:
                name = f"{run_id}-{suffix}"
                settings = {
                    "HOSPITAL_PROJECT": name, "HOSPITAL_PORT": port,
                    "HOSPITAL_BIND_ADDRESS": "127.0.0.1",
                    "CLOUD_URL": "http://patient:8000/events",
                    "EMERGENCY_URL": "http://emergency:8000/events",
                    "ADMIN_URL": "http://admin:8000/events",
                    "SITE_ID": site, "AUTO_REPLAY": common["AUTO_REPLAY"],
                    "AUTO_REPLAY_INTERVAL_SECONDS": common["AUTO_REPLAY_INTERVAL_SECONDS"],
                }
                project = Project(directory, name, DOCKER / "compose.hospital.yaml",
                                  settings, image, ["hospital"], peer_network=peers.name,
                                  peer_aliases={"hospital": f"hospital-{suffix}"})
                projects.append(project)
                hospitals.append((project, "hospital", port, site))

        for project in projects:
            project.call("config", "--quiet")
            project.start()
        for port in [patient, emergency, admin]:
            check(request(port, "/health")["status"] == "healthy", "Cloud unhealthy")
        for service, port in zip(SERVICE_NAMES, (patient, emergency, admin)):
            check(cloud.call("port", service, "8000", capture=True) == f"127.0.0.1:{port}",
                  "Cloud test ports must stay loopback-only")
        for project, service, port, _ in hospitals:
            check(project.call("port", service, "8000", capture=True) == f"127.0.0.1:{port}",
                  "Hospital test ports must stay loopback-only")
        check(cloud.call("port", "dashboard", "8080", capture=True) == f"127.0.0.1:{dashboard_port}",
              "Dashboard must have exactly the test's temporary loopback port")
        verify_dashboard(dashboard_port, [[], []], "applied", assets=True)

        if automatic:
            verify_automatic_recovery(layout, cloud, hospitals, projects,
                                      (patient, emergency, admin), run_id, dashboard_port, directory)
            return

        # Capture while the cloud APIs are stopped. This is an API outage,
        # not a simulated network partition. Hospital capture stays running.
        cloud.call("stop", "patient", "emergency", "admin")
        events = []
        for project, service, port, site in hospitals:
            event = {
                "event_id": f"{run_id}_{layout}_{site}", "site_id": site,
                "event_type": "patient_registration", "base_priority": 1,
                "prerequisite_ids": [], "payload": {"patient_id": f"SYNTHETIC_{site}"},
            }
            check(request(port, "/capture", event, 201)["status"] == "pending", "Capture failed")
            events.append(event)
        verify_dashboard(dashboard_port, [[event] for event in events], "pending", reachable=False)

        # Force-recreate every container, retaining only their named volumes.
        # Pending events must survive before any replay occurs.
        for project in projects:
            project.start(recreate=True)
        for (project, service, port, site), event in zip(hospitals, events):
            rows = request(port, "/events")
            check(len(rows) == 1 and rows[0]["event_id"] == event["event_id"]
                  and rows[0]["status"] == "pending", f"Pending data/isolation failure: {site}")
            results = json.loads(project.call("exec", "-T", service, "python", "-c", REPLAY, capture=True))
            check(any(r["event_id"] == event["event_id"] and r["status"] == "applied"
                      for r in results), f"Replay failed: {results}")
            duplicate = request(patient, "/events", event)
            check(duplicate["result"] == "already_applied", "Duplicate was not acknowledged")

        # Recreate again to verify applied SQLite status and PostgreSQL rows.
        for project in projects:
            project.start(recreate=True)
        for (_, _, port, site), event in zip(hospitals, events):
            rows = request(port, "/events")
            check(len(rows) == 1 and rows[0]["event_id"] == event["event_id"]
                  and rows[0]["status"] == "applied", f"Applied data lost: {site}")
            check(request(patient, "/events", event)["result"] == "already_applied",
                  "Cloud record did not survive recreation")
        count = cloud.call("exec", "-T", "postgres", "psql", "-U", "crisisguard",
                           "-d", "crisisguard", "-tAc", "SELECT count(*) FROM events", capture=True)
        check(count == "2", f"Expected exactly two cloud records, got {count}")
        verify_dashboard(dashboard_port, [[event] for event in events], "applied", event_traffic=True)
        verify_health_checker(cloud, hospitals, events, directory, automatic=False)

        # Back up each site's full SQLite store, including recovery state, and
        # restore to temporary files. Never overwrite the running databases.
        for (project, service, _, site), event in zip(hospitals, events):
            path = directory / f"{layout}-{site}.sqlite3"
            saved = database_backup.backup("sqlite", project.args, path, service, env=project.env)
            database_backup.verify_checksum(path, saved["sha256"])
            restored = database_backup.restore_sqlite(path, [event["event_id"]])
            check(restored["tables"]["events"] == 1, f"Restored SQLite count mismatch: {site}")
            check(restored["event_status_counts"] == {"applied": 1}, "Restored status mismatch")

        path = directory / f"{layout}-cloud.dump"
        saved = database_backup.backup("postgres", cloud.args, path, env=cloud.env)
        database_backup.verify_checksum(path, saved["sha256"])
        restored = database_backup.restore_postgres(path, [event["event_id"] for event in events])
        check(restored["event_count"] == 2, "Restored cloud count mismatch")
        # Compare every cloud event column, including JSON payload and timestamp.
        fingerprint = cloud.call("exec", "-T", "postgres", "psql", "-X", "-U", "crisisguard",
                                 "-d", "crisisguard", "-tAc",
                                 "SELECT md5(coalesce(string_agg(row_to_json(e)::text, E'\\n' ORDER BY event_id),'')) FROM public.events e",
                                 capture=True)
        check(restored["events_fingerprint"] == fingerprint, "Restored cloud contents differ")
        print(f"PASS {layout} manual: capture during API outage, site isolation, scheduled replay, "
              "duplicate ACKs, pending/applied persistence, SQLite/PostgreSQL backup restoration "
              "and dashboard/reusable health checks", flush=True)
    except BaseException:
        for project in projects:
            try:
                project.call("logs", "--no-color", "--tail", "40")
            except subprocess.CalledProcessError:
                pass
        raise
    finally:
        cleanup_errors = []
        for project in reversed(projects):
            try:
                # These names are generated in this invocation; never user projects.
                project.call("down", "--volumes", "--remove-orphans")
            except subprocess.CalledProcessError as error:
                cleanup_errors.append(project.name)
                print(f"Cleanup failed for {project.name}: {error}", file=sys.stderr)
        if peers:
            try:
                peers.remove()
            except subprocess.CalledProcessError:
                cleanup_errors.append(peers.name)
        if cleanup_errors:
            raise RuntimeError(f"Remove remaining smoke projects manually: {cleanup_errors}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", choices=["combined", "split", "both"], default="both")
    parser.add_argument("--replay-mode", choices=["manual", "automatic", "both"], default="both",
                        help="Default: retain manual/backup checks and also test automatic recovery")
    args = parser.parse_args()
    run(["docker", "info"], capture=True)
    run_id = f"cg-smoke-{uuid.uuid4().hex[:12]}"
    image = f"crisisguard-smoke:{run_id}"
    print(f"RUN {run_id}: layout={args.layout}, replay-mode={args.replay_mode}", flush=True)
    try:
        run(["docker", "build", "-f", str(DOCKER / "Dockerfile"), "-t", image, str(ROOT)])
        with tempfile.TemporaryDirectory(prefix=f"{run_id}-") as tmp:
            for layout in (["combined", "split"] if args.layout == "both" else [args.layout]):
                for mode in (["manual", "automatic"] if args.replay_mode == "both" else [args.replay_mode]):
                    verify_layout(layout, Path(tmp), image, f"{run_id}-{mode}", automatic=mode == "automatic")
    finally:
        subprocess.run(["docker", "image", "rm", image], cwd=ROOT, check=False)


if __name__ == "__main__":
    main()
