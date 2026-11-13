"""Build the minimal, reproducible AWS source archive without Git.

Run in the launcher container with the source mounted read-only. No AWS access.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile


SOURCE_FILES = (
    ".dockerignore",
    ".env.example",
    "compose.yaml",
    "requirements.txt",
    "infrastructure/docker/Dockerfile",
    "infrastructure/docker/compose.metaguard.yaml",
    "infrastructure/docker/compose.cloud.yaml",
    "infrastructure/docker/compose.hospital.yaml",
    "infrastructure/aws/bootstrap_lab.sh",
    "infrastructure/aws/configure_host.py",
    "infrastructure/aws/compose.dashboard.cloud.yaml",
    "infrastructure/aws/deploy.sh",
    "infrastructure/aws/cloud.env.example",
    "infrastructure/aws/hospital-a.env.example",
    "infrastructure/aws/hospital-b.env.example",
    "infrastructure/aws/compose.cloud.override.yaml",
    "infrastructure/aws/compose.hospital.override.yaml",
    "infrastructure/aws/compose.metaguard.cloud.yaml",
    "infrastructure/aws/compose.metaguard.hospital.yaml",
    "infrastructure/scripts/audit_deployment.py",
    "infrastructure/scripts/database_backup.py",
    "infrastructure/scripts/init_local_env.py",
    "infrastructure/scripts/verify_aws_recovery.py",
    "edge/capture_api/admission_retry.py",
    "edge/capture_api/auto_replay.py",
    "edge/capture_api/cloud_client.py",
    "edge/capture_api/grant_client.py",
    "edge/capture_api/main.py",
    "edge/capture_api/scheduled_replay.py",
    "edge/database/db.py",
    "edge/scheduler/config.py",
    "edge/scheduler/dag.py",
    "edge/scheduler/inspector.py",
    "edge/scheduler/live_preview.py",
    "edge/scheduler/priority.py",
    "edge/scheduler/scheduler.py",
    "edge/scheduler/session.py",
    "edge/scheduler/validation.py",
    "cloud/admin_service/main.py",
    "cloud/controller/admission.py",
    "cloud/controller/experimental.py",
    "cloud/controller/grants.py",
    "cloud/controller/main.py",
    "cloud/controller/pressure_health.py",
    "cloud/controller/scheduler.py",
    "cloud/controller/storage.py",
    "cloud/database/db.py",
    "cloud/emergency_service/main.py",
    "cloud/metaguard.py",
    "cloud/patient_service/main.py",
    "cloud/telemetry.py",
    "cloud/traffic.py",
    "dashboard/__init__.py",
    "dashboard/main.py",
    "dashboard/test_traffic.py",
    "dashboard/static/app.js",
    "dashboard/static/index.html",
    "dashboard/static/outage.js",
    "dashboard/static/quick-events.js",
    "dashboard/static/style.css",
    "dashboard/static/sync-status.js",
    "dashboard/static/test-traffic.js",
    "dashboard/static/workspace.js",
)


def source_files(root: Path) -> list[tuple[str, Path]]:
    """Select only known deployment inputs and reject links at every selected path."""
    result = []
    for name in sorted(SOURCE_FILES):
        path = root / name
        if path.is_symlink() or any((root / parent).is_symlink() for parent in Path(name).parents if str(parent) != "."):
            raise ValueError(f"Source path is linked: {name}")
        if not path.is_file():
            raise ValueError(f"Required source file is missing: {name}")
        result.append((name, path))
    return result


def package(root: Path, output: Path) -> dict:
    root = root.resolve(strict=True)
    files = source_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".source-", suffix=".tar", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream, tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, path in files:
                data = path.read_bytes()
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                entry.mtime = entry.uid = entry.gid = 0
                entry.uname = entry.gname = ""
                entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(data))
        sha = hashlib.sha256(Path(temporary).read_bytes()).hexdigest()
        os.replace(temporary, output)
        output.with_name(output.name + ".sha256").write_text(
            f"{sha}  {output.name}\n", encoding="ascii"
        )
        return {"archive": str(output), "sha256": sha, "files": len(files)}
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(package(args.source, args.output)))
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"Source packaging failed: {error}\n")


if __name__ == "__main__":
    main()
