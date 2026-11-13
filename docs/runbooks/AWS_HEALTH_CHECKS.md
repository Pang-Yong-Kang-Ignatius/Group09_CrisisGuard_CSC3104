# Reusable deployment health checker

For an existing deployment update, use the [update and rollback runbook](DEPLOYMENT_UPDATES.md)
to combine these health checks with backups, immutable image pins and configuration
guards. Health alone does not establish schema compatibility or the active image.

Run `infrastructure/scripts/verify_aws_recovery.py` **on each EC2 host over SSH**,
from its CrisisGuard checkout, with Docker access. The checker uses only the host
Python standard library and does not need AWS CLI credentials. It executes
read-only probes inside the selected running application containers. It does
not open SSH connections, start AWS, deploy, capture, replay, restart services or
write database records.

There is no default cloud IP or required TX record ID. `hospital`, `hospital-a`
and `hospital-b` are aliases for a hospital check; choose the correct host/env
file yourself. The alias does not select a remote machine or enforce a site name.

## Standard three-host deployment

Copy the updated single Python script to the same repository path on all three
hosts. Keep their existing private configuration files. In each host's checkout:

```bash
# Cloud host: healthy PostgreSQL and all three cloud APIs.
sudo python3 infrastructure/scripts/verify_aws_recovery.py cloud --env-file infrastructure/aws/cloud.env --output cloud-health-01.json

# Hospital A host: running automatic replay, correct identity and an empty queue.
sudo python3 infrastructure/scripts/verify_aws_recovery.py hospital --env-file infrastructure/aws/hospital.env --site-id Hospital_A --require-drained --output hospital-a-health-01.json

# Hospital B host: same check against B's own configuration/container.
sudo python3 infrastructure/scripts/verify_aws_recovery.py hospital --env-file infrastructure/aws/hospital.env --site-id Hospital_B --require-drained --output hospital-b-health-01.json
```

Use a new report filename for each run. The checker reserves it **before probing**
and refuses to overwrite it. New reports use mode 0600 on systems supporting
Unix permissions. JSON is also printed to stdout. Exit code **0** means the
requested checks passed; **1** means verification/configuration failed; argument
usage errors return **2**. Inspect individual `checks` entries for failures and
explicitly skipped checks. Failures do not repair or reset anything.

Reports created with `sudo` are root-owned. Inspect them with `sudo`, or transfer
ownership to your own login user before copying them off the host; retain private
file permissions.

By default, the checker selects the role's `infrastructure/aws/cloud.env` or
`hospital.env`, standard split Compose file and AWS override. Cloud checks
required services `postgres`, `patient`, `emergency`, `admin`; hospital checks
service `hospital`. Docker Compose must find the same project used to deploy.

## What is checked

- Exactly one running, healthy container per required service.
- On **each hospital**, live health requests from inside its container to all
  three URLs used by the application's routing client. This checks the configured
  container-to-cloud path, rather than only the EC2 host's network access.
- Capture API availability and identity matching the container's nonempty
  `SITE_ID`; `--site-id` can additionally assert your intended site name.
- Separate counts of local pending/applied events, with no mixed-site records.
- `AUTO_REPLAY=1`, an enabled loop, all three reported services reachable, no
  current replay error and a recent `last_check_at` (default maximum age 30s).
- On the cloud, live health requests from the Patient container to all three
  service DNS names and a PostgreSQL read-only transaction checking ledger counts.
- Optional expected applied events, with metadata or full-record scope below.

The cloud's default Docker-DNS checks do not establish hospital connectivity;
run **both hospital checks as well**. `--require-drained` additionally requires
zero pending and zero replayable backlog at that hospital. Without it, a healthy
system can pass while events are still queued; the report shows those counts.

The command is a bounded point-in-time check, not a wait-for-recovery command.
After a restart, wait for the application to initialize/check cloud health, then
rerun if status is still unknown. For an intentionally longer replay interval,
set an appropriate `--max-status-age SECONDS` explicitly. Per-HTTP-request timeout
is three seconds by default (`--timeout 1` through `--timeout 5`). A partial cloud
outage, disabled loop, stale status or wrong site fails the standard hospital
check. `--allow-manual-replay` explicitly skips the automatic-loop requirement
for an older/manual-only deployment; it is reported as **skipped**, not verified.

## Verify your own test records

Health checks alone do not prove event delivery or persistence. When no expected
events are supplied, the JSON says `record_verification: not_requested` and marks
the expected-record check skipped. No original TX records are checked implicitly.

For an existing synthetic event, add a metadata-only expectation:

```bash
sudo python3 infrastructure/scripts/verify_aws_recovery.py hospital --site-id Hospital_A --expect-event MY_REGISTRATION_ID Hospital_A patient_registration --require-drained --output hospital-a-record-01.json
```

Repeat `--expect-event ID SITE_ID EVENT_TYPE` for additional records. This verifies
each ID occurs exactly once with the specified site/type and applied status; it
does **not** compare payload, priority or prerequisites. The report labels this
`metadata_only`.

For stronger verification, use a JSON array of the **original capture request
bodies**, containing event ID, site, type, priority, prerequisite list and payload.
An optional `status` must be `applied`. The example contains synthetic data only:

```bash
cp infrastructure/aws/verification-events.example.json infrastructure/aws/verification-events.private.json
```

Edit this local copy to match records you actually captured. **The manifest does
not create events**; sample IDs that have not been submitted will fail verification.
The local `*.private.json` copy is ignored by Git. Use synthetic data only; the
report excludes payloads, passwords, raw exception text and full environment data.

On **each host**, run its appropriate role, sharing the same A/B manifest:

```bash
sudo python3 infrastructure/scripts/verify_aws_recovery.py cloud --expect-events infrastructure/aws/verification-events.private.json --output cloud-records-01.json
sudo python3 infrastructure/scripts/verify_aws_recovery.py hospital --expect-events infrastructure/aws/verification-events.private.json --require-drained --output hospital-records-01.json
```

The cloud checks all listed events directly in PostgreSQL. A hospital selects
only expectations matching its runtime `SITE_ID`; it fails if none match. Full
record comparisons reject changed payloads, priority, dependencies, site/type,
missing/duplicate IDs or non-applied status. The report labels this `full_record`
and lists only verified IDs, site/type/status. Use the same manifest before and
after a restart to establish that **those records** survived; a single healthy
report alone is not restart/persistence evidence.

## Custom projects, Compose files and addresses

Select the exact settings and Compose merge order used by your deployment:

```bash
sudo python3 infrastructure/scripts/verify_aws_recovery.py hospital --env-file path/to/my-hospital.env --project-name my-hospital-a --compose-file infrastructure/docker/compose.hospital.yaml --compose-file infrastructure/aws/compose.hospital.override.yaml --site-id Hospital_A --output custom-health-01.json
```

When `--compose-file` is supplied, it replaces the default Compose file list;
repeat it for **every** required base/override file, in deployment order. For a
combined local project, select `compose.yaml` and the same env/project as your
deployment, with `--hospital-service hospital-a` or `hospital-b`. Relative file
arguments resolve from the invoking terminal's working directory.

Hospital cloud addresses and ports are discovered from the running container,
including `PATIENT_URL`/`CLOUD_URL`, `EMERGENCY_URL` and `ADMIN_URL`. To assert an
expected private host and published ports explicitly, add:

```text
--cloud-host YOUR_CLOUD_PRIVATE_IP --patient-port 8001 --emergency-port 8003 --admin-port 8004
```

Replace the placeholder. On hospitals, these options **validate** the configured
destinations; they do not silently retarget a misconfigured application. On the
cloud, `--cloud-host` instead makes its container probe that explicit address
and published ports rather than internal service DNS. Port overrides require
`--cloud-host`. Endpoint URLs containing credentials, query strings or fragments
are rejected and not echoed. HTTPS uses standard certificate verification.

## Local regression coverage

On 24 October, run `cg-smoke-d495b62bae75` passed all four smoke scenarios and
12 real Docker-backed checker runs. All **408 unit tests passed on Python
3.11.17**, including 30 new tests plus the retained 10 checker regressions. The
generated resources were removed. See [evidence](../evidence/reusable-health-checker-20261024.json).
No new live EC2 check or hosted CI result is claimed for this update.

The default Docker smoke test runs this checker for the cloud and both hospitals
in each combined/split and manual/automatic scenario. Generated manifests use
that run's IDs and full synthetic event contents. Manual scenarios explicitly
select `--allow-manual-replay`; automatic ones require the active replay loop.
This validates the reusable host-side checker against real disposable Docker
services without launching AWS:

```powershell
python infrastructure/scripts/smoke_deployment.py
python -m pytest tests/unit/test_aws_recovery_verification.py tests/unit/test_reusable_health_checker.py -q
```

These checks do not establish controller grants, lost-ACK recovery, network
partition tolerance or performance. Earlier AWS evidence retains the historical
checker/version/record scope documented in its dated reports. No new EC2 result
is implied by a local regression pass.
