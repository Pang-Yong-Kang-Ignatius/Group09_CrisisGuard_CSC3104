# AWS configuration audit

Local development and AWS use separate configurations. The local `compose.yaml`
and Python fallback URLs/password are development conveniences. AWS cloud Compose
requires explicit PostgreSQL settings; the AWS deployment preflight validates
the rendered configuration before any `up`. It does not change application code,
rotate an existing password, source an env file as shell code or start AWS.

## Hospital scheduler settings

`SCHEDULER_ENABLE_INHERITANCE` defaults to `1` and accepts `1`, `0`, `true`,
or `false`, case-insensitively. `SCHEDULER_AGEING_INTERVAL_SECONDS` defaults to
`600` and must be positive and finite. Both root/local and split/AWS hospital
Compose configurations pass these values into the hospital container. Settings
belong in the private env file used by that project's actual deployment.

These settings affect automatic and authorized manual scheduled replay. They do
not disable dependency checks, grant fencing, or retry/uncertainty handling.
The Inspector's default follows active settings; an explicit inheritance query
only changes a read-only preview. Inspect `replay_policy` and `comparison_only`
in `/scheduler/inspect` to distinguish the two. Selection logs include the active
policy. The Dashboard shows active inheritance and ageing separately from any
comparison preview.

Changing an env file requires recreating the hospital container with the full
deployment file set. It also requires an application image containing this
feature; existing pinned AWS images are not automatically upgraded. Keep the
current defaults during an ordinary upgrade, then use separately reviewed
settings for controlled policy comparisons. Invalid values fail application
startup before capture/replay and before DB initialization. Compose's
`config --quiet` checks interpolation/syntax; it does not replace application
validation of these settings.


Experimental replay uses this same settings object. `REPLAY_SELECTION_POLICY`
accepts `priority` (default) or `fifo`; FIFO requires
`CRISISGUARD_EXPERIMENTAL_POLICIES=1`. Optional `REPLAY_INHERITANCE=0/1`
overrides `SCHEDULER_ENABLE_INHERITANCE`; setting it to `0`, or overriding a
different configured inheritance value, also requires explicit opt-in. Without
that alias, Bettina's configured inheritance is used. Ageing always comes from
`SCHEDULER_AGEING_INTERVAL_SECONDS`; invalid base settings are rejected even when
an experimental override is present. Existing experiment runners inject these
aliases into their private Compose config; normal deployment mappings are unchanged.

Replay, the default Inspector and selection logs share the resolved policy,
including `selection_policy` in `replay_policy`/`scheduler_policy`. FIFO only
changes ranking among dependency-eligible events. Effective-priority reporting,
retry/quarantine checks and uncertain-grant pinning remain active. Explicit
Inspector inheritance comparisons remain read-only and retain the active ranking.

## Check before deployment

On the selected EC2 host, from its checkout, with its existing private settings:

```bash
sudo python3 infrastructure/scripts/audit_deployment.py cloud --env-file infrastructure/aws/cloud.env
# Hospital host instead:
sudo python3 infrastructure/scripts/audit_deployment.py hospital --env-file infrastructure/aws/hospital.env
```

Choose only the matching role. The default merge is the role's Docker Compose
base followed by its AWS runtime override. Custom deployments can supply repeated
`--compose-file` arguments in their actual merge order and `--project-name`.
`deploy.sh` now invokes this check automatically before creating/recreating
containers. An audit failure exits nonzero; fix the private settings before
retrying. Existing data is not changed by the audit.

The cloud check requires:

- One RFC1918 private bind IP shared by three distinct application ports;
  no published PostgreSQL port.
- Explicit matching database user/name/URL and a random hex password at least
  48 characters long, matching `openssl rand -hex 24` in the bootstrap procedure.
- Writable named PostgreSQL storage, health checks, `unless-stopped` restart
  policies and bounded Docker logs (`10m`, three files).

The hospital check requires:

- A loopback listener, named `/data` storage and `/data/hospital.db`.
- Explicit `Hospital_A` or `Hospital_B` identity, automatic replay enabled and a
  finite positive recovery interval.
- Explicit Patient/Emergency/Admin `/events` URLs on three distinct ports of
  the same RFC1918 private cloud IP, without embedded credentials/query strings.
- The same health, restart and log policies.

Private DNS names, proxies, TLS endpoints and alternate storage layouts need an
explicitly reviewed extension to this checker. The current contract intentionally
matches this project's three-host, private-IP deployment.

**Keep the existing database password.** Editing `POSTGRES_PASSWORD` in an env
file does not rotate credentials inside an initialized PostgreSQL volume. If an
older deployment does not meet the password policy, coordinate a tested database
credential rotation with Ignatius; do not generate a replacement on every start
or delete the volume to make startup succeed.

## Secrets and source packaging

Use `umask 077` and mode 0600 for real env/credentials files. Keep PEM keys,
temporary AWS credentials, private record manifests, backups and release
checkpoints outside source control. The Git/Docker ignore rules now also exclude
key files, `.aws`, `release-checkpoints` and `*.private.json`. Example env files
and sanitized evidence remain available to teammates.

Run the tracked-artifact audit locally or in CI:

```powershell
python infrastructure/scripts/audit_deployment.py repository
```

It checks tracked **filenames**, not secret values or Git history. The launcher
also rejects private artifact paths in its source archive and requires the new
audit script before provisioning. Commit/push all intended files before using
`git archive HEAD`. Ignore rules do not remove an already tracked secret or
revoke a previously exposed credential; handle any actual exposure with the
account owner/instructor rather than publishing its value in an issue or report.

## What a passing audit proves

This is a static configuration preflight. It does not inspect AWS security groups,
discover the instance's real private IP, validate running container identity,
test connectivity or prove data persistence. Follow it with the
[read-only health checker](runbooks/AWS_HEALTH_CHECKS.md) and full-record expectations.
Use the [update runbook](runbooks/DEPLOYMENT_UPDATES.md) for retained deployments; use the
[teardown runbook](runbooks/AWS_TEARDOWN.md) only when retiring an identified deployment.
No configuration values or raw Docker errors are printed by the preflight.


## MetaGuard Admission and Performance Monitoring

### Admission Policies

MetaGuard supports two admission policies:

- **Fixed-rate (default):** Uses configured request rates and in-flight limits for the Normal, Caution and Recovery states.
- **AIMD (opt-in):** Multiplicatively decreases the admission rate when overload is detected and additively increases it after consecutive healthy observations.

Both policies retain the existing grant lifecycle, authentication, admission limits and reconciliation mechanisms.

The local and AWS MetaGuard overlays both pass `CONTROLLER_ADMISSION_POLICY`
(`fixed` by default, `aimd` to opt in) and `CONTROLLER_ADMISSION_ADDITIVE_RPS`
(`0.2` by default) to the controller. AWS preflight rejects an absent/unknown
resolved policy or a non-finite/non-positive additive rate. Changing a private
settings file only affects a running service after the selected Compose configuration
is reapplied. Do not print resolved configuration containing tokens.

For a policy-only backout on an existing protected deployment, follow the
[same-image procedure](../infrastructure/aws/README.md#policy-only-backout-on-an-existing-deployment).

AIMD is implemented as an alternative policy. Its performance superiority over fixed-rate admission has not been established.

### Active Admission Signals

The controller evaluates telemetry from the Patient, Emergency and Admin Cloud APIs.

The following measurements influence admission decisions:

| Signal | Purpose |
|---|---|
| Average request latency | Detect increased response times |
| Request error rate | Detect application failures |
| Request throughput | Detect high request activity |
| PostgreSQL health | Detect database unavailability |
| Telemetry freshness | Prevent admission decisions based on stale or missing observations |

Missing, invalid or stale telemetry causes the controller to enter Recovery.

### Monitoring-Only Signals

The following measurements are collected and displayed but do not currently influence admission decisions:

- Live-request p95 latency
- Live SLO violation count and rate
- Executor queue depth
- Executor queue pressure

These measurements support monitoring and evaluation. They must not be described as active AIMD feedback signals.

The preceding signal lists describe the default `throughput` health policy.
The separately gated `pressure-experimental` policy requires
`CRISISGUARD_EXPERIMENTAL_POLICIES=1`. It retains identity, counter and freshness
validation. Missing, malformed, stale or nonadvancing required observations deny
grants; database failure forces Recovery. The worst of general/live p95, live
SLO fraction, HTTP error fraction, executor waiting and database health sets
severity. Any waiting executor worker prevents a healthy vote and triggers
Caution; waiting work at least one worker-pool triggers Recovery. Throughput is
validated and recorded but does not determine severity. The policy reuses the
configured latency and error-fraction thresholds for its additional signals.
Code defaults for healthy/caution/recovery are 150/250/1000 ms and
0.01/0.05/0.20. The recorded evaluation used 50/80/1000 ms and
0.01/0.05/0.15; these are **not calibrated overload thresholds**. Idle tails may be null,
while missing required fields are not treated as idle. The existing AIMD cuts,
additive recovery, dwell, caps and fencing remain. See the [W5 validation](reports/W5_AIMD_RECOVERY_VALIDATION_20261102.md)
and [diagnosis](reports/AIMD_RECOVERY_DIAGNOSIS_20261101.md). Healthy read-load
comparisons do not establish genuine-overload sensitivity.

### Default Admission Configuration

| Parameter | Normal | Caution | Recovery |
|---|---:|---:|---:|
| Admission rate (requests/s) | 2.0 | 0.5 | 0.2 |
| In-flight limit | 4 | 2 | 1 |

Default degradation thresholds:

| Signal | Caution | Recovery |
|---|---:|---:|
| Average latency | 250 ms | 1000 ms |
| Error rate | 5% | 20% |
| Throughput | 20 req/s | 50 req/s |

The controller polls Cloud telemetry every 5 seconds and considers observations stale after 15 seconds.

These values are implementation defaults, not experimentally established optimal thresholds.

### Evaluation and Limitations

The Cloud exposes live-request p95 latency and SLO violation measurements for authenticated live traffic.

A 250 ms SLO threshold has been used in local calibration experiments. This is an experimental configuration, not proof of a proposal-wide SLO guarantee.

Initial read-only load calibration did not establish sustained Cloud capacity. Consequently, the proposed 30% and 70% capacity conditions cannot yet be claimed as fully calibrated.

The matched read-only constant-rate versus experimental AIMD comparison is
recorded in [W5 validation](reports/W5_AIMD_RECOVERY_VALIDATION_20261102.md).
Proposal-wide performance superiority and genuine-overload protection remain
unestablished; no performance advantage should be claimed beyond that evidence.

The default admission algorithm is retained. Experimental pressure feedback and
retry handling require separate design review and genuine-overload validation
before production use; they do not change the default signal set.
