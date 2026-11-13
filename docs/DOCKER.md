# Docker and Deployment Guide

## Choose the deployment mode

| Mode | Services | Protection |
|---|---:|---|
| `start.project.bat` | 8 | Recommended local mode; MetaGuard, host outage agent and browser |
| Base Compose + MetaGuard overlay/profile | 8 | Same protected services without the host agent/browser |
| Bare root `docker compose up` | 7 | Legacy compatibility/API mode; no protected ingestion |
| Split Cloud/hospital Compose + AWS overlays | 7 across three hosts | Protected when the MetaGuard overlays are explicitly activated; no Dashboard |

Do not apply legacy configuration to a retained protected project or mix legacy
and grant-fenced writers against one PostgreSQL ledger. Another extracted folder
does not isolate the fixed `crisisguard-local` project or its volumes.

## Protected local startup

Follow the [README environment setup](../README.md#5-python-environment-setup) for Python 3.11,
Docker Desktop Linux containers, Compose v2 with `up --wait`, project dependencies
and private configuration. The documented commands use PowerShell; the batch
launcher itself uses Command Prompt. Node.js is needed only for JavaScript tests,
not application startup. Both Windows launchers use `.venv311\Scripts\python.exe`.
Create it with `py -3.11 -m venv .venv311` in a fresh clone, then run
the following commands from the repository root. Check each command succeeds.

```powershell
.\.venv311\Scripts\python.exe -m pip install -r requirements.txt
.\.venv311\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv311\Scripts\python.exe -m pip check
.\.venv311\Scripts\python.exe -c "import httpx, fastapi, uvicorn; from infrastructure.scripts import local_outage_agent; print('Host imports OK')"
```

The test requirements include the runtime file and add optional coverage tooling.
`httpx`, `fastapi`, `uvicorn[standard]` and `pydantic` are shared host/Docker
requirements. Building a Docker image installs its own dependencies. The host
agent uses the packages installed in `.venv311`. Startup validates Python 3.11
and host imports before contacting Docker, and prints repair commands on failure.
Install dependencies during setup. Repeat startup reuses the installed environment.

Replace the example database password only before creating a fresh database;
preserve credentials for an existing database. The local initializer generates
missing distinct telemetry tokens with `--with-local-test-traffic`; the launcher
passes this option automatically and preserves valid credentials on repeat startup.
It requires a local Docker Desktop named pipe context and rejects remote Docker
endpoints before initializing `.env`.
The plain initializer intentionally leaves optional telemetry tokens unchanged.

```powershell
.\start.project.bat
```

It selects `compose.yaml`, `infrastructure/docker/compose.metaguard.yaml`,
`infrastructure/docker/compose.dashboard-test-traffic.local.yaml`, then
`compose.override.yaml` if present, with `--profile metaguard`. It builds and
waits for PostgreSQL, Patient, Emergency, Admin, controller, Hospital A/B and
Dashboard; exports SQLite snapshots; launches the local outage agent; and opens
http://localhost:8080. Monitoring's Generate Test Traffic control is enabled on
this local stack, including after repeat startup. It still requires the
server's local-target and deployment checks and sends bounded authenticated
read requests only. The snapshot copies are not the live databases.

In Monitoring, use **Prepare Demo Records** first. It captures three fixed
synthetic Hospital A events through the normal capture API: registration,
emergency record and discharge, each dependent on the previous event. Wait for
all three to show `applied` before selecting 10 read-only requests per API.
Repeated preparation checks the same records and does not duplicate captures.
The dashboard hides SLO rows unless a valid `CLOUD_LIVE_SLO_MS` threshold is
already configured; startup does not set one.

For manual startup, use the same ordered files and explicit service set:

```powershell
.\.venv311\Scripts\python.exe -m infrastructure.scripts.init_local_env --with-local-test-traffic
$cgComposeFiles = @('-f', 'compose.yaml', '-f', 'infrastructure/docker/compose.metaguard.yaml', '-f', 'infrastructure/docker/compose.dashboard-test-traffic.local.yaml')
if (Test-Path .\compose.override.yaml) { $cgComposeFiles += @('-f', 'compose.override.yaml') }
docker compose -p crisisguard-local @cgComposeFiles --profile metaguard config --quiet
docker compose -p crisisguard-local @cgComposeFiles --profile metaguard up -d --build --wait postgres patient emergency admin controller hospital-a hospital-b dashboard
docker compose -p crisisguard-local @cgComposeFiles --profile metaguard ps
```

`config --quiet` avoids printing credentials. Starting either path can recreate
containers and replay pending events. For disposable verification use the
[guarded E2E procedure](testing.md#real-distributed-system-e2e-mode). The launcher's
`--verify` mode is used by `verify_windows_launcher.py`; it does not independently
supply that verifier's fresh-source/alternate-port setup.

## Ports, storage and access

Default host ports are Hospital A 8000, Patient 8001, Hospital B 8002, Emergency
8003, Admin 8004, controller 8005, Dashboard 8080, PostgreSQL 5433 and outage agent
8765. API `/docs` endpoints and controller `/health` are available locally.
Published port variables can be set privately, but Dashboard/agent browser origins
currently require ports 8080/8765. Linux engine, virtualization, writable source
and sufficient Docker disk/memory are prerequisites; no capacity minimum is claimed.

| Persistent volume | Contents |
|---|---|
| `hospital-a-data` | Hospital A SQLite database and recovery journals at `/data/hospital.db` |
| `hospital-b-data` | Independent Hospital B SQLite database and recovery journals |
| `postgres-data` | Shared Cloud ledger, controller tickets/grants/admission state |

Hospitals have no Cloud startup dependency, allowing local capture during outages.
Protected writes require hospital-specific credentials and a matching acknowledged
grant. The Dashboard routes synthetic writes through hospital capture; manual
replay is disabled by default. Health/metrics/status endpoints are not hospital-
authenticated and must remain on trusted/loopback networks. Keep the operator token
separate from hospital credentials and never publish resolved Compose settings.

## Local outage controls

The host agent is a trusted local demo tool bound to `127.0.0.1:8765`. It authorizes
browser requests and restricts operations to Patient, Emergency and Admin in
`crisisguard-local`. It does not manage AWS or expose Docker's socket to Dashboard.

The launcher opens its terminal. To start it separately:

```powershell
.\.venv311\Scripts\python.exe -m infrastructure.scripts.local_outage_agent --allow-local-demo
```

Copy its temporary token into Dashboard and authorize local controls. Simulate
Cloud outage, capture synthetic hospital events, then restore Cloud and verify
exact applied IDs. Restore before closing the agent; restart the agent after
relevant container recreation. Do not expose Dashboard or agent publicly.
The old opt-in original-stack outage integration test operates real demo APIs;
inspect its instructions before running it on an intentionally idle deployment.

## Admission policies

The default fixed policy uses global grant issue rates/caps of 2/s and 4 in
Normal, 0.5/s and 2 in Caution, and 0.2/s and 1 in Recovery. The controller polls
all three Cloud `/metrics`; missing/stale telemetry denies unsafe admission.
Healthy samples and dwell govern recovery rather than an immediate jump to Normal.

AIMD and experimental policies remain opt-in. Admission/fairness statements apply
to grant-based replay within documented assumptions, not arbitrary traffic.
See [configuration](CONFIGURATION.md), [metrics](reference/METAGUARD_METRICS_CONTRACT.md)
and [policy-only backout](../infrastructure/aws/README.md#policy-only-backout-on-an-existing-deployment).

## Legacy seven-service local stack

The bare root Compose command retains the non-MetaGuard compatibility mode:

```powershell
docker compose up -d --build --wait
docker compose ps
```

It permits direct Dashboard-to-Cloud test submissions and uses the same root
project name by default. Use it only for its intended separate legacy deployment;
it must not downgrade an existing protected stack. A local API stop/start is not
proof of an AWS packet partition. For all new submission demonstrations, prefer
the protected launcher and procedures above.

## AWS deployment and restoration

The split deployment uses Cloud and two hospital hosts, private Cloud/controller
ingress restricted to the hospital security group, internal PostgreSQL and
loopback hospital capture. The protected split stack has seven containers without
Dashboard. Private HTTP bearer transport is a documented lab limitation.

- [Fresh lab launcher](runbooks/AWS_LAUNCHER.md) and [manual AWS guide](../infrastructure/aws/README.md).
- [Historical MetaGuard deployment acceptance](../infrastructure/aws/README.md#historical-tx-acceptance-1-november-2026).
- [Image update/rollback](runbooks/DEPLOYMENT_UPDATES.md), [backups](runbooks/BACKUPS.md),
  [health checks](runbooks/AWS_HEALTH_CHECKS.md) and [retirement](runbooks/AWS_TEARDOWN.md).

The AWS MetaGuard overlays and `deploy.sh --metaguard` support reviewed activation;
existing deployments require staged source, backups, preflight and compatible rollback.
A settings-file edit affects a running service only after configuration is reapplied.
Do not replace retained databases or credentials as an upgrade shortcut.

<a id="verification-recorded-on-16-october-2026"></a>

### Historical verification references

The legacy 16 October fragment is retained for older links. Use the actual dated
[31 October local verification](evidence/CP7D_FINAL_VERIFICATION_20261031.md),
[7B AWS snapshot](evidence/CP7B_AWS_ADAPTIVE_TRANSITIONS_20261029.md) and
[1 November TX assessment](evidence/tx-aws-review-20261101.json) for evidence.
The 7B AWS Caution/Recovery transitions were not tested; TX's later acceptance
retains private-HTTP and current-release power-cycle limits. No historical check
establishes performance capacity, final revision CI or a clean Windows installation.

<a id="repeatable-deployment-checks"></a>

## Tests and CI

Use [the master runner](testing.md) for safe regressions and explicit isolated
integration/E2E procedures. Existing `smoke_deployment.py` and
`smoke_metaguard_deployment.py --ci` build/use disposable resources; inspect their
requirements before invoking them. Do not run fault tools on retained services
just to verify cosmetic changes.

CI contains `unit-tests`, `docker-deployment` and `metaguard-integration`.
[TX's CI record](evidence/tx-ci-final-release-20261101.json) identifies historical green
release `16e74b8` and retained artifacts. Verify all jobs on the actual submitted
commit. Evaluation experiments are separate from functional smoke/reliability tests.

## Persistence, stopping and troubleshooting

Use the same `$cgComposeFiles` selection as startup:

```powershell
docker compose -p crisisguard-local @cgComposeFiles --profile metaguard stop
```

Stop retains named volumes; close the outage-agent terminal separately. Do not
use `down -v`, volume/image prune, SQLite deletion or grant resets on retained data.
Existing PostgreSQL credentials are stored in its volume; changing `.env` does
not rotate them. Back up PostgreSQL and SQLite using the [backup runbook](runbooks/BACKUPS.md)
and test restores in isolation.

For unhealthy services, inspect `ps`, scoped logs, PostgreSQL health and private
routing. For stalled replay, inspect identity, Cloud URLs, `AUTO_REPLAY`, controller
admission, hospital `/recovery/status`, pending records and grants. A zero replayable
backlog is not proof of no unresolved work. Store local logs under ignored `logs/`;
keep private state intact and publish only sanitized evidence.
