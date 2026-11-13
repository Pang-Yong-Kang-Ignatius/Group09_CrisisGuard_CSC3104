"""Opt-in disposable Docker regression: RUN_DASHBOARD_DOCKER=1 pytest this file."""
import os
from pathlib import Path
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from infrastructure.scripts import smoke_deployment as smoke


@pytest.mark.skipif(os.getenv("RUN_DASHBOARD_DOCKER") != "1", reason="Requires disposable Docker resources")
def test_offline_dashboard_capture_and_automatic_recovery(monkeypatch):
    run_id = "cg-dashboard-" + uuid.uuid4().hex[:12]
    image = "crisisguard-smoke:" + run_id
    original_ports, original_request, original_call = smoke.free_ports, smoke.request, smoke.Project.call
    ports = []
    stop = threading.Event()

    def allocate(count):
        ports[:] = original_ports(count)
        return ports

    def request(port, path, payload=None, expected=200, **kwargs):
        if path == "/capture" and port in ports[:2]:
            hospital = "hospital-a" if port == ports[0] else "hospital-b"
            result = original_request(ports[5], f"/api/hospitals/{hospital}/capture", payload)
            assert result["http_status"] == expected
            return result["response"]
        return original_request(port, path, payload, expected, **kwargs)

    def call(project, *args, **kwargs):
        if args and args[0] == "down":
            # Never use down -v. Remove only volumes labelled for this fresh UUID project.
            volumes = smoke.run(["docker", "volume", "ls", "-q", "--filter",
                                 "label=com.docker.compose.project=" + project.name], capture=True).splitlines()
            result = original_call(project, "down", "--remove-orphans", **kwargs)
            for volume in volumes:
                smoke.run(["docker", "volume", "rm", volume])
            return result
        return original_call(project, *args, **kwargs)

    def poll_cloud():
        # Match the browser's concurrent Cloud probes while hospital operations run.
        with ThreadPoolExecutor(max_workers=6) as pool:
            while not stop.wait(1):
                if not ports:
                    continue
                futures = [pool.submit(original_request, ports[5], path, timeout=30)
                           for path in ("/api/services", "/api/metrics") * 3]
                for future in futures:
                    try:
                        future.result()
                    except Exception:
                        pass  # Startup/shutdown may race; smoke assertions validate actual health.

    monkeypatch.setattr(smoke, "free_ports", allocate)
    monkeypatch.setattr(smoke, "request", request)
    monkeypatch.setattr(smoke.Project, "call", call)
    thread = threading.Thread(target=poll_cloud, daemon=True)
    try:
        smoke.run(["docker", "build", "-f", str(smoke.DOCKER / "Dockerfile"), "-t", image, str(smoke.ROOT)])
        thread.start()
        with tempfile.TemporaryDirectory(prefix=run_id) as directory:
            smoke.verify_layout("combined", Path(directory), image, run_id, automatic=True)
    finally:
        stop.set()
        if thread.is_alive():
            thread.join(timeout=40)
        smoke.run(["docker", "image", "rm", image])
