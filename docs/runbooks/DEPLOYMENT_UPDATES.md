# Application-image updates and compatible rollback

This maintenance procedure updates the existing three-host deployment while
retaining its configuration, project names, PostgreSQL container and database
volumes. It does not launch AWS or replace a database. Use one operator and pause
all team workloads, dashboard submissions and direct API writers for the window.
The tools do not enforce a distributed write lock.

**Scope:** application-image changes with reviewed backward-compatible database
schema, event format and APIs. Confirm compatibility with the backend/edge owners
and test the candidate before using real retained data. A healthy startup or a
compatibility note is not proof of migration safety. PostgreSQL upgrades,
Compose/network/storage changes, controller additions and incompatible migrations
need their own reviewed plan. Do not use an older application against a schema
it cannot read.

`release_checkpoint.py` prepares protected local image pins and verifies identity;
it **never runs Compose up/down, stops containers or restores databases**. It adds
local image-retention tags. Activation and rollback below are explicit operator
commands, gated by the existing read-only health checker. Trusted checkpoint and
source files must remain available until the release is retired.

## 1. Review and stage committed source separately

Record the exact reviewed candidate commit and CI result. Package it with
`git archive HEAD` using the staging commands below. See the [AWS deployment overview](../../README.md#12-optional-aws-academy-deployment) for deployment prerequisites.
Use that commit in the archive filename, copy the same archive to all three
hosts, and record its SHA-256 locally. Never unpack it over the existing checkout
or replace private env files. The archive must include the current checkpoint,
backup and health-check tools; uncommitted files are excluded.

On each host over SSH, use Bash and replace the two placeholders below:

```bash
set -euo pipefail
umask 077
new_revision='REPLACE_WITH_REVIEWED_FULL_COMMIT_SHA'
expected_archive_sha='REPLACE_WITH_RECORDED_ARCHIVE_SHA256'
current="$HOME/CrisisGuard"
release_parent="$HOME/crisisguard-releases"
mkdir -p "$release_parent"
archive="$HOME/crisisguard-${new_revision}.tar"
printf '%s  %s\n' "$expected_archive_sha" "$archive" | sha256sum -c -
release_src="$release_parent/$new_revision"
mkdir -m 700 "$release_src"
tar -xf "$archive" -C "$release_src"
tools="$release_src/infrastructure/scripts"
sudo docker build --label "org.opencontainers.image.revision=$new_revision" -f "$release_src/infrastructure/docker/Dockerfile" -t "crisisguard-app:$new_revision" "$release_src"
```

`mkdir` deliberately refuses an existing release-source folder. For another
attempt, choose a new reviewed staging directory explicitly; do not merge source
trees. The source tag is a convenience: the checkpoint tool resolves it to a
local immutable image ID before activation. Record each host's actual ID rather
than assuming independently built images are identical.

## 2. Select the existing deployment on each host

Choose **one** role block per host:

```bash
# Cloud host
role=cloud
env_file="$current/infrastructure/aws/cloud.env"
services=(patient emergency admin)
check_role=(cloud)
backup_kind=postgres
backup_name=cloud.dump
```

```bash
# Each hospital; use Hospital_B on B's host
role=hospital
env_file="$current/infrastructure/aws/hospital.env"
services=(hospital)
check_role=(hospital --site-id Hospital_A --require-drained)
backup_kind=sqlite
backup_name=hospital.sqlite3
```

Then, on each host, construct matching arguments for the **existing** deployment:

```bash
files=("$current/infrastructure/docker/compose.$role.yaml" "$current/infrastructure/aws/compose.$role.override.yaml")
compose=(sudo docker compose --env-file "$env_file")
checker=(sudo python3 "$tools/verify_aws_recovery.py" "${check_role[@]}" --env-file "$env_file")
audit=(sudo python3 "$tools/audit_deployment.py" "$role" --env-file "$env_file")
prepare=(sudo python3 "$tools/release_checkpoint.py" prepare "$role" --env-file "$env_file")
backup_args=(--env-file "$env_file")
for file in "${files[@]}"; do
  compose+=(-f "$file")
  checker+=(--compose-file "$file")
  audit+=(--compose-file "$file")
  prepare+=(--compose-file "$file")
  backup_args+=(--compose-file "$file")
done
expected_manifest="$HOME/verification-events.private.json"
```

Prepare/copy your own full synthetic capture-body manifest to `expected_manifest`
using the [health-check guide](AWS_HEALTH_CHECKS.md). It must describe records
already present, not the unsubmitted example IDs. The same A/B manifest can be
used on all hosts. If deployment uses additional accepted overrides or an explicit
project name, add those **same** files/name to all five argument arrays. Preserve
the established merge order and never source the env file as shell code.

Pause all producers and let both hospitals drain. Before proceeding, run on
**all three hosts** and require every requested check to pass:

```bash
"${audit[@]}"
"${checker[@]}" --expect-events "$expected_manifest"
```

Do not proceed with missing records, pending/quarantined backlogs, wrong site
identity, stale/disabled replay or unhealthy APIs. Keep workloads paused through
backup and cutover. If you cannot stop writers, agree on a coordinated snapshot
and migration strategy rather than treating three independent exports as atomic.

## 3. Export and isolate-restore fresh backups

On each host, create a fresh private maintenance directory and export its DB:

```bash
run_dir="$release_parent/maintenance-${new_revision}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$run_dir"
sudo install -m 600 "$env_file" "$run_dir/settings-before.env"
sudo python3 "$tools/database_backup.py" --report "$run_dir/backup.json" backup "$backup_kind" "${backup_args[@]}" --output "$run_dir/$backup_name"
backup_sha=$(sudo python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "$run_dir/backup.json")
sudo python3 "$tools/database_backup.py" --report "$run_dir/restore-check.json" restore-check "$backup_kind" --backup "$run_dir/$backup_name" --sha256 "$backup_sha"
```

Require successful exports, matching checksums and successful isolated restores
for cloud, A and B. Add `--expect-event ID` arguments to restore checks for the
records that must exist, and review status counts. Keep protected off-host copies
as described in [BACKUPS.md](BACKUPS.md). No deployed database is replaced by
these commands. Files created by sudo are root-owned; give only the operator
access when transferring copies. Do not publish raw database/config backups.

## 4. Prepare old-image pins and a candidate override

Run on each host **before activating any host**:

```bash
old_revision='unknown'
checkpoint="$run_dir/checkpoint"
"${prepare[@]}" --directory "$checkpoint" --revision "$old_revision"
sudo python3 "$tools/release_checkpoint.py" candidate --directory "$checkpoint" --image "crisisguard-app:$new_revision" --revision "$new_revision" --compatibility-note 'REPLACE_WITH_REVIEWED_SCHEMA_API_AND_EVENT_COMPATIBILITY_REASON'
sudo python3 "$tools/release_checkpoint.py" check --directory "$checkpoint" --expect previous
```

Set `old_revision` to the actual recorded deployed commit if known. Do not use
the new checkout's revision as a guess; `unknown` is explicit and actual running
image IDs remain authoritative. The compatibility note records operator review,
not an automated migration guarantee.

The helper checks healthy starting containers and that resolved Compose settings
match their running environment. It saves only hashes of private configuration,
image IDs, mount/volume identity and provenance. The protected directory contains:

- `checkpoint.json`: original project, configuration hashes, image/volume IDs and
  PostgreSQL container witness. Raw container environment/passwords are omitted.
- `rollback.compose.json`: previous immutable application images, pull disabled.
- `candidate.json` and `candidate.compose.json`: selected candidate image ID,
  reviewed source/compatibility note and image-only override.

Existing checkpoints/candidates are never overwritten. Extra local tags retain
old/candidate images; avoid image pruning while rollback may be needed. Keep the
original checkout, configuration, checkpoint directory and image cache intact.

## 5. Activate cloud, then A, then B with gates

One host at a time, first cloud then Hospital A then Hospital B:

```bash
sudo python3 "$tools/release_checkpoint.py" check --directory "$checkpoint" --expect previous
"${compose[@]}" -f "$checkpoint/candidate.compose.json" up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 "${services[@]}"
sudo python3 "$tools/release_checkpoint.py" check --directory "$checkpoint" --expect candidate
"${checker[@]}" --compose-file "$checkpoint/candidate.compose.json" --expect-events "$expected_manifest" --output "$run_dir/health-after-update.json"
```

Require success before moving to the next host. Only the selected application
services are recreated. PostgreSQL is not selected, and the same named volumes
are used. The checkpoint refuses changed env/config hashes, shell-resolved
configuration, mounts, data-volume creation identity, unrecorded images or a
recreated PostgreSQL container. Its image check is separate from application
health; both gates must pass.

Report files refuse overwrites. If repeating a check after investigating a
failure, choose a new report filename to retain the earlier evidence.

If a command fails, stop the rollout and inspect that host's private logs/state.
Do not edit artifacts to force a pass or start a second deployment. If the release
is still known compatible, use the backout below. If a migration ran or
compatibility is uncertain, keep writers paused and involve the backend/edge
owners before attempting a downgrade.

After all hosts pass, test new synthetic capture/replay from A and B, retain
before/after reports, and record the accepted image IDs, candidate commit and
complete Compose file order. Retain the accepted override and use it for later
Compose operations; rebuilding the old checkout with `deploy.sh` would not select
this pinned candidate. Normal Docker restart policies retain existing containers
across reboot. Resume workloads only after the acceptance gates succeed.

## 6. Roll back only a confirmed compatible application

Pause producers again. Back out updated hosts in reverse order (B, A, cloud),
using each host's own protected checkpoint and original config. For each host:

```bash
sudo python3 "$tools/release_checkpoint.py" check --directory "$checkpoint"
"${compose[@]}" -f "$checkpoint/rollback.compose.json" up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 "${services[@]}"
sudo python3 "$tools/release_checkpoint.py" check --directory "$checkpoint" --expect previous
"${checker[@]}" --compose-file "$checkpoint/rollback.compose.json" --expect-events "$expected_manifest" --output "$run_dir/health-after-rollback.json"
```

If a cloud outage is deliberately being recovered, bring it back before expecting
hospital health/drained gates to pass. Gate all three hosts again once connectivity
is restored. Records captured since the update should remain in their existing
databases; include their capture bodies in the current manifest where available.
Do not restore an older database merely to make counts match a previous release.

**Application rollback is not database restoration.** This path never runs
`down -v`, volume pruning, reinitialization or a live restore. An incompatible
schema/data change needs a separately reviewed recovery plan; restoring a backup
can lose writes made after that backup. A failed prepare/check leaves services
untouched; inspect any partial private artifacts/retention tags before retrying
with a fresh checkpoint. Do not remove backups/images until their retention
window and coursework needs are resolved.

## Local rehearsal and scope

From the repository root with Docker Desktop running:

```powershell
$updateTemp = Join-Path (Get-Location) ('.venv\update-rehearsal-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $updateTemp | Out-Null
$env:TEMP = $updateTemp
$env:TMP = $updateTemp
$env:TMPDIR = $updateTemp
python infrastructure/scripts/rehearse_update.py --report "$updateTemp/result.json"
```

The rehearsal uses fresh split projects, AWS restart/logging overrides and
synthetic data. Cross-project traffic uses a fresh internal peer bridge and
container DNS; published test ports stay on loopback on Windows and Linux.
That bridge is removed after the generated projects. It verifies isolated backup
restores, a compatible image update,
configuration-drift rejection, retention of post-update records, pending SQLite
events/recovery state through rollback, and recovery after a rejected startup.
It checks the exact target image IDs and unchanged PostgreSQL container/volume.
Only its own projects, volumes and image tags are removed afterward.

Candidate images contain the **same application code**, with one label-only
version change and one deliberately broken entrypoint. This validates release
mechanics, not arbitrary application/schema compatibility, zero downtime or
cross-host atomic deployment. No AWS resources are used. The CI Docker job runs
this rehearsal after the standard smoke test; hosted results must be checked
after pushing the change.

Verified locally on **24 October 2026** in run `cg-update-09dbb3fe278a`: all
rehearsal stages passed, six final cloud records and three per hospital were
retained, exact image gates passed, and all generated resources were removed.
The full Python 3.11 unit suite passed **424 tests**, including 16 new checkpoint
cases. See [sanitized evidence](../evidence/deployment-update-rollback-20261024.json).
No live EC2 update or hosted CI result is claimed by this local verification.

## Original EC2 maintenance rehearsal — 25 October 2026

After local checks, `verify-20261025-e2900ef8` passed on the original cloud/A/B
instances. The new preflight accepted all three real configurations. Deployed
cloud/edge Python files matched recorded commit `ab76b88638f7a1ebaa50e45f866a62eb69a03390`
after CRLF/LF normalization; the first raw-byte preparation attempt stopped before
cutover, and no runtime source files were edited to resolve the newline difference.

Fresh PostgreSQL and SQLite backups passed checksum and isolated restore checks
(ten cloud records, five per hospital initially). A label-only candidate derived
from each actual running image passed exact-image and full-record health gates,
cloud then A then B. One new synthetic patient registration per hospital replayed
automatically. Compatible rollback in reverse order preserved all **twelve cloud
records and six per hospital**, hospital database/recovery state and the original
PostgreSQL container/image/volume identity.

A fresh Hospital A backup restored all six records before a separate broken-image
attempt. Its entrypoint exited before application/schema initialization; Compose's
wait gate rejected it. Backout restored the exact previous image and retained the
same SQLite contents/recovery state. Final cloud/A/B gates passed.

No new EC2 instances, CloudFormation stacks, IAM/security-group changes, live DB
restore or backup download occurred. Private backups, manifests, checkpoints and
verification image tags remain on the existing hosts. All three instances were
confirmed **Stopped**, followed by **Lab terminated / 00:00**. The portal showed
`Used $1.2 of $50`; this rounded display is not an exact incremental cost measure.
See [sanitized live evidence](../evidence/aws-update-rollback-20261025.json).

This exercised release mechanics with the existing application source. It does
not approve a new application release, arbitrary migration or controller activation.
Hosted CI for these uncommitted changes and teammate reproduction remain pending.
