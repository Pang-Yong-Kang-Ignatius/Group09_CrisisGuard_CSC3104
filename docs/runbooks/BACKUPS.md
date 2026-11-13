# Database backup and restore checks (TX)

**1 November closeout:** the protected split deployment verifier also compares
all public PostgreSQL table counts/content fingerprints after an isolated restore,
including controller tickets, grants, admission state and audit history. Hospital
restores compare the complete SQLite schema/data. Its exported/copy files are local
test artifacts; fresh off-host backups of the selected retained AWS deployment
still require that owner's active session. See [TX AWS assessment](../evidence/tx-aws-review-20261101.json).
The historical host-specific examples below describe TX's original deployment;
select Ignatius's actual project/configuration paths when working on his stack.

For application releases, follow the [deployment update and rollback runbook](DEPLOYMENT_UPDATES.md).
It uses these exports and isolated restore checks before changing images; a
compatible application rollback keeps the live databases rather than restoring
an older export.

The tool `infrastructure/scripts/database_backup.py` provides two operations:

| Operation | Purpose | Effect on live data |
|---|---|---|
| `backup postgres` | Create a PostgreSQL custom-format archive through Compose | Reads the configured cloud database |
| `backup sqlite` | Snapshot a hospital database using SQLite's online backup API | Reads the configured hospital; includes events and recovery state |
| `restore-check postgres` | Restore the archive into a fresh temporary PostgreSQL container | Does not connect to the deployed cloud database |
| `restore-check sqlite` | Restore into a fresh temporary SQLite file; compare schema and all contents | Does not open the deployed hospital database for writing |

Use Python 3.11+ and Docker Engine with Compose. The host Python needs no extra
packages. Run from the repository root. On EC2, use `sudo python3`; on Windows,
use your virtual environment's Python with Docker Desktop running.

PostgreSQL restore checks use `postgres:16`, matching this project's database
major version. They publish no ports, have no external network, and use temporary
storage rather than production volumes. Only the container created by the check
is removed afterward. Use your own trusted dumps: restoring PostgreSQL archives
executes their SQL. This checks restoration into a clean database; it does not
perform an application cutover or restore users/passwords from PostgreSQL roles.

## 1. Back up the cloud on EC2

The existing AWS checkout predates this tool. After resuming the existing lab,
copy the single script to each server using its new public IP, from local PowerShell:

```powershell
scp -i "$env:USERPROFILE\Downloads\labsuser.pem" infrastructure/scripts/database_backup.py ubuntu@CURRENT_PUBLIC_IP:~/CrisisGuard/infrastructure/scripts/database_backup.py
```

Use the existing source checkout, project name and private environment file.
The commands below use a new directory outside the repository:

```bash
cd ~/CrisisGuard
umask 077
backup_dir="$HOME/crisisguard-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"

sudo python3 infrastructure/scripts/database_backup.py \
  --report "$backup_dir/cloud-backup.json" backup postgres \
  --env-file infrastructure/aws/cloud.env \
  --compose-file infrastructure/docker/compose.cloud.yaml \
  --compose-file infrastructure/aws/compose.cloud.override.yaml \
  --output "$backup_dir/cloud.dump"

# Keep mode 600; allow the SSH user to download only these newly created files.
sudo chown "$(id -u):$(id -g)" "$backup_dir/cloud.dump" "$backup_dir/cloud-backup.json"
```

The JSON report records the archive size and SHA-256, with `restore_tested: false`.
Exit code 0 and `result: passed` confirm export, not a successful restore.
Existing files are never overwritten. Failed exports remove their new partial
file. Linux files are created with owner-only permissions; the targeted ownership
change lets the SSH user download them while preserving those permissions.

## 2. Back up Hospital A and Hospital B

Run separately on each hospital, using its existing `hospital.env` and a fresh
backup directory as above. Give A and B separate off-host destinations.

```bash
sudo python3 infrastructure/scripts/database_backup.py \
  --report "$backup_dir/hospital-backup.json" backup sqlite \
  --env-file infrastructure/aws/hospital.env \
  --compose-file infrastructure/docker/compose.hospital.yaml \
  --compose-file infrastructure/aws/compose.hospital.override.yaml \
  --output "$backup_dir/hospital.sqlite3"
sudo chown "$(id -u):$(id -g)" "$backup_dir/hospital.sqlite3" "$backup_dir/hospital-backup.json"
```

This uses SQLite's backup API while the capture API is running. It includes
committed WAL data and the whole database, including retry counts, leases and
quarantine state. Do not copy only the live `.db` file while it is being written.

For local combined Compose, supply its actual env file, use
`--compose-file compose.yaml`, omit the AWS override and select
`--service hospital-a` or `--service hospital-b`. For independent local projects,
use their matching private env file and the corresponding base Compose file.

## 3. Test restoration

Copy backups and their reports to protected off-host storage, then verify the
checksum there as well. The scripts do not upload or download files automatically.
Never commit the database files. `.gitignore` and `.dockerignore` exclude the
standard backup formats and the root `backups/` directory. Commit only reviewed,
sanitized evidence reports, not reports containing unnecessary private paths.

On the Linux host holding the cloud backup, run:

```bash
cloud_sha=$(sudo python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "$backup_dir/cloud-backup.json")
sudo python3 infrastructure/scripts/database_backup.py \
  --report "$backup_dir/cloud-restore-check.json" restore-check postgres \
  --backup "$backup_dir/cloud.dump" --sha256 "$cloud_sha"
```

For a hospital backup, on the host holding it:

```bash
hospital_sha=$(sudo python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "$backup_dir/hospital-backup.json")
sudo python3 infrastructure/scripts/database_backup.py \
  --report "$backup_dir/hospital-restore-check.json" restore-check sqlite \
  --backup "$backup_dir/hospital.sqlite3" --sha256 "$hospital_sha"
```

Add `--expect-event EVENT_ID` for each event that must exist. For the original
AWS cloud backup, use both `TX_AWS_A_20261016_01` and `TX_AWS_B_20261016_01`.
For each original hospital, use only its matching event ID. Expected-ID checks
verify presence; review the reported status counts separately.

On Windows, put `--report` before `restore-check`, as above. If the default
temporary directory gives `WinError 5`, create a fresh working directory inside
`.venv` and set `TEMP` and `TMP` to it for that PowerShell session, as described
for pytest in `DOCKER.md`.

Checks require a matching SHA-256 before restoration. SQLite additionally runs
integrity and foreign-key checks, requires the events/recovery tables and compares
the restored schema and every row against the backup. PostgreSQL restores schema,
data and constraints in one transaction and stops on an error. Reports expose
counts and a content fingerprint, not event payloads or credentials.

## Verification and remaining work

- **22 October 2026:** all **280 unit tests** passed, including nine new backup
  tests and the ten recovery-checker tests. Tests cover committed WAL data,
  pending/applied events, recovery state, corrupted checksums, missing sources,
  failed exports, no-overwrite behavior and cleanup after a failed restore.
- **Actual AWS PostgreSQL backup restored locally:** the 2,115-byte backup with
  SHA-256 `67fef154f9a66cd26ddefc99770622c43d5159d3a333ca6308be7111738f5579`
  restored in an isolated Docker container. Both expected events were found;
  there were two applied records. See [raw evidence](../evidence/aws-20261022-backup-local-restore.json).
- The Docker smoke test now backs up and restores both hospital stores and the
  cloud in each layout. It checks full cloud row fingerprints and hospital
  schema/data. **Both combined and split layouts passed locally**, run
  `cg-smoke-5fabb64fcb68`; all generated test resources were removed. See the
  [verification record](../evidence/backup-verification-20261022.json).
  GitHub Actions runs it through the existing Docker job; a new
  hosted CI result requires committing and pushing these changes.
- **Actual AWS Hospital A/B backups restored on EC2 and Windows:** each backup
  is 20,480 bytes and contains one applied event plus one recovery-state row.
  SHA-256 matched after download; integrity, foreign keys, expected event IDs
  and full schema/data comparisons passed. See [combined evidence](../evidence/aws-20261022-hospital-backups.json)
  and the raw [A](../evidence/aws-20261022-hospital-a-backup-local-restore.json) /
  [B](../evidence/aws-20261022-hospital-b-backup-local-restore.json) restore reports.
  The backup tool is installed on both hospital hosts. The running application
  remains at `2c7cb71`; no application redeployment or live database replacement
  was performed. All six services and the original records passed checks afterward.
- Next: coordinate a scoped hospital/cloud network-outage test with Haris.
  Backup validation does not establish automatic replay or network recovery.

### Locations of the verified hospital backups

On **each hospital**, the private backup and reports are under
`~/crisisguard-backups/20261022-0614/`: `hospital.sqlite3`,
`hospital-backup.json` and `hospital-restore-check.json`.
Approved off-host copies are on TX's computer, outside this repository, named
`hospital-a-20261022.sqlite3` and `hospital-b-20261022.sqlite3` in the local task
artifact folder. The combined evidence records each checksum. Keep the A/B
filenames distinct and store any additional copies in approved private storage.

To repeat a downloaded SQLite restore check in **PowerShell** from the repository
root, replace the two placeholders using your backup path and its server report:

```powershell
.\.venv311\Scripts\python.exe infrastructure/scripts/database_backup.py `
  restore-check sqlite --backup 'PATH_TO_HOSPITAL_A_BACKUP' `
  --sha256 'SHA256_FROM_SERVER_BACKUP_REPORT' `
  --expect-event TX_AWS_A_20261016_01
```

For B, use its own file/checksum and `TX_AWS_B_20261016_01`. Successful output must
show `result: passed`, `expected_events_found: 1` and `applied: 1` for these
specific saved backups. Future backups may contain more events. This restores
into a temporary file and removes that temporary copy after checking it; it
does not replace the live hospital database.

Implementation references: [SQLite online backup](https://docs.python.org/3.11/library/sqlite3.html#sqlite3.Connection.backup)
and [PostgreSQL 16 pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html).
