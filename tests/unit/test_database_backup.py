"""Exercise real SQLite snapshots/restores and failed-export protections."""

from contextlib import closing
import os
import sqlite3
import subprocess
import sys

import pytest

from infrastructure.scripts import database_backup as backup


def seed(conn):
    conn.executescript("""
      CREATE TABLE events(event_id TEXT PRIMARY KEY, status TEXT, payload TEXT);
      CREATE TABLE recovery_state(event_id TEXT PRIMARY KEY REFERENCES events(event_id),
        retry_count INTEGER, claim_token TEXT, quarantined INTEGER);
      INSERT INTO events VALUES ('pending-event','pending','synthetic-pending');
      INSERT INTO events VALUES ('applied-event','applied','synthetic-applied');
      INSERT INTO recovery_state VALUES ('pending-event',3,'lease-token',1);
      INSERT INTO recovery_state VALUES ('applied-event',0,NULL,0);
    """)
    conn.commit()


def test_online_snapshot_includes_wal_and_recovery_state(tmp_path):
    source = tmp_path / "live.sqlite3"
    target = tmp_path / "backup.sqlite3"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        seed(conn)
        # Keep writer open so committed WAL rows cannot be lost by a raw file copy.
        with target.open("wb") as output:
            subprocess.run([sys.executable, "-c", backup.SQLITE_BACKUP],
                           env=dict(os.environ, HOSPITAL_DB_PATH=str(source)),
                           stdout=output, check=True)
        result = backup.restore_sqlite(target, ["pending-event", "applied-event"])
        assert result["tables"] == {"events": 2, "recovery_state": 2}
        assert result["event_status_counts"] == {"applied": 1, "pending": 1}
        with closing(sqlite3.connect(target)) as restored:
            assert restored.execute("SELECT retry_count,claim_token,quarantined FROM recovery_state WHERE event_id='pending-event'").fetchone() == (3, "lease-token", 1)
        assert conn.execute("SELECT count(*) FROM events").fetchone() == (2,)


def test_missing_source_is_not_created(tmp_path):
    source = tmp_path / "missing.sqlite3"
    result = subprocess.run([sys.executable, "-c", backup.SQLITE_BACKUP],
                            env=dict(os.environ, HOSPITAL_DB_PATH=str(source)),
                            capture_output=True)
    assert result.returncode != 0
    assert not source.exists()
    assert result.stdout == b""


def test_corruption_is_rejected_before_restore(tmp_path):
    path = tmp_path / "archive.dump"
    path.write_bytes(b"original")
    expected = backup.checksum(path)
    path.write_bytes(b"modified")
    with pytest.raises(ValueError, match="does not match"):
        backup.verify_checksum(path, expected)


def test_existing_backup_is_never_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "keep.dump"
    path.write_bytes(b"keep")
    monkeypatch.setattr(backup, "run", lambda *a, **kw: pytest.fail("must not export"))
    with pytest.raises(FileExistsError):
        backup.backup("postgres", ["docker", "compose"], path)
    assert path.read_bytes() == b"keep"


def test_failed_export_removes_only_new_partial_file(tmp_path, monkeypatch):
    path = tmp_path / "partial.dump"
    unrelated = tmp_path / "keep.dump"
    unrelated.write_bytes(b"keep")
    def fail(args, **kwargs):
        kwargs["stdout"].write(b"partial")
        raise RuntimeError("export failed")
    monkeypatch.setattr(backup, "run", fail)
    with pytest.raises(RuntimeError, match="export failed"):
        backup.backup("postgres", ["docker", "compose"], path)
    assert not path.exists()
    assert unrelated.read_bytes() == b"keep"


def test_restore_requires_expected_events_and_valid_relationships(tmp_path):
    path = tmp_path / "backup.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        seed(conn)
        with pytest.raises(RuntimeError, match="expected event"):
            backup.restore_sqlite(path, ["missing"])
        conn.execute("INSERT INTO recovery_state VALUES ('orphan',0,NULL,0)")
        conn.commit()
    with pytest.raises(RuntimeError, match="foreign-key"):
        backup.restore_sqlite(path)


def test_wrong_sqlite_schema_rejected(tmp_path):
    path = tmp_path / "unrelated.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE unrelated(x)")
    with pytest.raises(RuntimeError, match="must contain"):
        backup.restore_sqlite(path)


def test_failed_postgres_restore_cleans_only_created_container(tmp_path, monkeypatch):
    archive = tmp_path / "bad.dump"
    archive.write_bytes(b"invalid dump")
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "run", "-d"]:
            assert args[args.index("--network") + 1] == "none"
            assert "-p" not in args and "--publish" not in args
            assert "--volume" not in args and "-v" not in args
            return b"owned-container-id\n"
        if "pg_restore" in args:
            raise RuntimeError("bad archive")
        return b""
    monkeypatch.setattr(backup, "run", fake_run)
    with pytest.raises(RuntimeError, match="bad archive"):
        backup.restore_postgres(archive)
    assert calls[-1] == ["docker", "rm", "-f", "owned-container-id"]


def test_checksum_mismatch_cli_never_starts_docker(tmp_path, monkeypatch, capsys):
    archive = tmp_path / "bad.dump"
    archive.write_bytes(b"modified")
    monkeypatch.setattr(sys, "argv", ["database_backup", "restore-check", "postgres",
                                     "--backup", str(archive), "--sha256", "0" * 64])
    monkeypatch.setattr(backup, "run", lambda *a, **kw: pytest.fail("must not invoke Docker"))
    assert backup.main() == 1
    assert '"result": "failed"' in capsys.readouterr().out
