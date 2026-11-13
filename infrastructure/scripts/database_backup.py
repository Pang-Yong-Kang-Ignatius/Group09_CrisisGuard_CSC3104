"""Back up Compose databases and test restoration without replacing live data.

Uses Python's standard library and Docker CLI. Run --help for commands.
Restore checks accept only your own trusted backups; PostgreSQL dumps contain SQL.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
SQLITE_BACKUP = '''import os, sqlite3, sys, tempfile
from pathlib import Path
from contextlib import closing
source = Path(os.environ["HOSPITAL_DB_PATH"]).resolve()
with tempfile.TemporaryDirectory(prefix="cg-backup-") as directory:
    target = Path(directory) / "hospital.sqlite3"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    with target.open("rb") as data:
        import shutil
        shutil.copyfileobj(data, sys.stdout.buffer)
'''


def run(args, *, stdin=None, stdout=None, timeout=180, env=None):
    result = subprocess.run(
        args, stdin=stdin if stdin is not None else subprocess.DEVNULL,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=ROOT, timeout=timeout, env=env,
    )
    if result.returncode:
        # Database commands can include private config or row data in stderr.
        raise RuntimeError(f"{args[0]} command failed (exit {result.returncode}); private command output suppressed")
    return result.stdout or b""


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_checksum(path, expected):
    actual = checksum(path)
    if actual != expected.lower():
        raise ValueError("Backup SHA-256 does not match; restore was not attempted")
    return actual


def private_file(path):
    # Exclusive creation prevents overwriting a backup or following an existing symlink.
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def backup(kind, compose, destination, service=None, env=None):
    destination = Path(destination).resolve()
    service = service or ("postgres" if kind == "postgres" else "hospital")
    if kind == "postgres":
        command = compose + ["exec", "-T", service, "sh", "-c",
                             'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom']
    else:
        command = compose + ["exec", "-T", service, "python", "-c", SQLITE_BACKUP]
    # A failed export leaves no file that might be mistaken for a valid backup.
    with private_file(destination) as output:
        try:
            run(command, stdout=output, env=env)
        except BaseException:
            output.close()
            destination.unlink()
            raise
    if destination.stat().st_size == 0:
        destination.unlink()
        raise RuntimeError("Backup export was empty")
    return {"kind": kind, "file": str(destination), "bytes": destination.stat().st_size,
            "sha256": checksum(destination), "restore_tested": False}


def summarize_sqlite(path, expected_events=()):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("SQLite foreign-key check failed")
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if "events" not in tables or "recovery_state" not in tables:
            raise RuntimeError("Hospital backup must contain events and recovery_state")
        counts = {name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for name in tables}
        for event_id in expected_events:
            if not conn.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
                raise RuntimeError("An expected event is missing from the restored database")
        statuses = dict(conn.execute("SELECT status, count(*) FROM events GROUP BY status"))
        return {"tables": counts, "event_status_counts": statuses,
                "expected_events_found": len(expected_events)}


def restore_sqlite(path, expected_events=()):
    # Restore into a new disposable file, never a live path or mounted volume.
    with tempfile.TemporaryDirectory(prefix="cg-restore-") as directory:
        restored = Path(directory) / "restored.sqlite3"
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(restored)) as target:
                source.backup(target)
        result = summarize_sqlite(restored, expected_events)
        # Compare schema plus all tables, including retries/leases/quarantine fields.
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(restored)) as target:
            if list(source.iterdump()) != list(target.iterdump()):
                raise RuntimeError("Restored SQLite contents differ from the backup")
        return result


def table_fingerprint_sql(name):
    if not re.fullmatch(r'[a-z_][a-z0-9_]*', name):
        raise ValueError('Invalid public table identifier')
    return ('SELECT json_build_object(\'rows\',count(*),\'fingerprint\','
            "md5(coalesce(string_agg(row_to_json(t)::text, E'\\n' ORDER BY row_to_json(t)::text),''))) "
            'FROM public."' + name + '" t')


def restore_postgres(path, expected_events=(), image="postgres:16", expected_tables=None):
    name = "cg-restore-" + uuid.uuid4().hex
    container_id = None
    try:
        # No ports, external network, source env files or persistent volumes.
        container_id = run([
            "docker", "run", "-d", "--name", name, "--network", "none",
            "--label", "crisisguard.restore-check=" + name,
            "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=restore_check",
            image, "-c", "shared_buffers=32MB", "-c", "max_connections=10",
        ]).decode().strip()
        if not container_id:
            raise RuntimeError("Docker did not return a restore container ID")
        deadline = time.monotonic() + 60
        while True:
            try:
                run(["docker", "exec", container_id, "pg_isready", "-U", "postgres", "-d", "restore_check"], timeout=10)
                # Require TCP as well: the entrypoint's temporary initialization
                # server accepts sockets before the final server is ready.
                run(["docker", "exec", container_id, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], timeout=10)
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Isolated PostgreSQL did not become ready") from None
                time.sleep(1)
        with Path(path).open("rb") as source:
            run(["docker", "exec", "-i", container_id, "pg_restore", "-U", "postgres",
                 "-d", "restore_check", "--exit-on-error", "--single-transaction",
                 "--no-owner", "--no-privileges"], stdin=source)
        # Report only aggregate counts; the fingerprint covers all event columns.
        sql = """SELECT json_build_object(
          'event_count', (SELECT count(*) FROM public.events),
          'event_status_counts', (SELECT coalesce(json_object_agg(status,n), '{}'::json)
            FROM (SELECT status,count(*) n FROM public.events GROUP BY status) s),
          'events_fingerprint', (SELECT md5(coalesce(string_agg(row_to_json(e)::text, E'\\n' ORDER BY event_id),'')) FROM public.events e)
        );"""
        base = ["docker", "exec", container_id, "psql", "-X", "-U", "postgres", "-d", "restore_check", "-v", "ON_ERROR_STOP=1", "-tAc"]
        result = json.loads(run(base + [sql]))
        for event_id in expected_events:
            # Hex-encoded UTF-8 avoids interpreting event IDs as SQL syntax.
            literal = "convert_from(decode('" + event_id.encode().hex() + "','hex'),'UTF8')"
            if run(base + ["SELECT count(*) FROM public.events WHERE event_id=" + literal]).strip() != b"1":
                raise RuntimeError("An expected event is missing from restored PostgreSQL")
        result["expected_events_found"] = len(expected_events)
        verified = {}
        for table, expected in (expected_tables or {}).items():
            summary = json.loads(run(base + [table_fingerprint_sql(table)]))
            if summary != expected:
                raise RuntimeError('Restored PostgreSQL table contents differ from the expected backup state')
            verified[table] = summary
        if expected_tables is not None:
            result['verified_tables'] = verified
        result["restore_target"] = "isolated container; no network or persistent volume"
        return result
    finally:
        if container_id:
            # Only the exact container ID returned by this invocation is removed.
            run(["docker", "rm", "-f", container_id], timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="New JSON report file; never overwritten")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup")
    create.add_argument("kind", choices=["postgres", "sqlite"])
    create.add_argument("--env-file", type=Path, required=True)
    create.add_argument("--compose-file", type=Path, action="append", required=True)
    create.add_argument("--service", help="Default: postgres or hospital; combined Compose: hospital-a/hospital-b")
    create.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore-check")
    restore.add_argument("kind", choices=["postgres", "sqlite"])
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--sha256", required=True)
    restore.add_argument("--expect-event", action="append", default=[])
    restore.add_argument("--postgres-image", default="postgres:16")
    args = parser.parse_args()
    # Reserve the report before any work, so an old report is never overwritten.
    report_file = private_file(args.report) if args.report else None
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "operation": args.command}
    try:
        if args.command == "backup":
            compose = ["docker", "compose", "--env-file", str(args.env_file.resolve())]
            for path in args.compose_file:
                compose += ["-f", str(path.resolve())]
            report.update(backup(args.kind, compose, args.output, args.service))
        else:
            report["sha256"] = verify_checksum(args.backup, args.sha256)
            report["kind"] = args.kind
            report.update(restore_postgres(args.backup, args.expect_event, args.postgres_image)
                          if args.kind == "postgres" else restore_sqlite(args.backup, args.expect_event))
        report["result"] = "passed"
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        report.update(result="failed", error=str(error))
    encoded = (json.dumps(report, indent=2) + "\n").encode()
    if report_file:
        with report_file:
            report_file.write(encoded)
    print(encoded.decode(), end="")
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
