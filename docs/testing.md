# CrisisGuard Testing Guide

Use synthetic records. [README](../README.md#5-python-environment-setup) covers Windows
installation; [DOCKER.md](DOCKER.md) covers the protected eight-service startup,
ports, local outage agent and persistent storage. The Windows runner requires
Python 3.11 in the repository-local `.venv311`, matching `start.project.bat`.
Node.js 24 is the documented reference runtime for JavaScript tests; it is not
needed to start the application. Run these `.ps1` commands in Windows PowerShell
5.1 or PowerShell 7. Quick/Full need no Docker engine; Docker/E2E/integration modes
have the additional requirements below. Activation and npm installation are not
required; the runner never installs dependencies. Retained run versions and
results are listed in the [evidence index](EVIDENCE_INDEX.md).

## Automated regressions

Install `requirements-test.txt` into `.venv311` and run `pip check` using the
explicit interpreter in the README before testing.

Run from the repository root:

```powershell
.\test.project.ps1 -Quick
.\test.project.ps1 -Help
```

Quick runs all Python unit tests, the mocked Cloud API/manual-replay security
integration modules and all `tests/unit/*.cjs` files. It does not use Docker.
For a downloaded ZIP without `.git`, the runner records Source revision as
`NOT VERIFIED`; it does not claim a Git revision. Copy the complete source and
test snapshot when moving a local fix into an isolated installation. Partial
Dashboard-only updates can pair new behavior with obsolete test expectations.
The runner clears inherited service/database credentials and third-party pytest
plugins, directs SQLite to a unique run directory and disables default live targets.
It does not discover arbitrary integration scripts, AWS tools or experiments.

For coverage and the current audit snapshot:

```powershell
.\.venv311\Scripts\python.exe -m pip install -r requirements-test.txt
.\test.project.ps1 -Full
```

| Mode | Scope and side effects |
|---|---|
| `-Quick` | Python/API/security and Node regressions; unique local test outputs, no coverage or Docker operations |
| Default / `-Full` | Same suites; optional Python coverage, function/API/V8 evidence and refreshed `docs/evidence/PROJECT_TEST_COVERAGE.md` |
| `-Full -WithDocker` | Adds read-only local Compose/container/HTTP health observations; does not start or repair services |
| `-Full -WithIntegration` | Adds the PostgreSQL allowlist only after explicit scratch-database isolation proof, below |
| `-EndToEnd` | Builds/provisions a unique disposable system; faults and cleanup require explicit confirmation |

Do not combine Quick with Full or EndToEnd. `-Full -EndToEnd` runs Full before
the E2E session; E2E alone dispatches directly to that runner. Quick and Full
execute the same regression population, so their counts are not additive.

Each Node file runs in a separate Python-launched process with
`--test-isolation=none`. The legacy queue harness counts as one file-level test.
DOM-model tests do not establish browser, accessibility or rendering acceptance.
V8 maps complete byte-identical VM scripts only; sliced/prepended scripts remain
unattributed. No complete JavaScript coverage percentage is claimed.

If `coverage.py` is absent, suites still run and coverage is NOT VERIFIED.
When installed, `.coveragerc` covers production Python code and excludes tests
and generated outputs. Function entry alone does not establish correctness.

### Windows dependency and launcher regressions

The following runs the real Windows batch launcher with a fake Docker executable
inside source copies whose paths contain spaces. It checks interpreter selection,
missing packages, incompatible Python and startup failures. Docker calls remain
inside the test fixture.

```powershell
.\.venv311\Scripts\python.exe -m pytest -q tests/unit/test_windows_launcher_batch.py tests/unit/test_windows_dependency_setup.py
```

Enable the clean-install regression explicitly to create a new Python 3.11
environment, install both declared requirements files, run `pip check` and verify
host imports. It requires access to the configured package index or pip cache.

```powershell
$env:CG_TEST_CLEAN_INSTALL = '1'
.\.venv311\Scripts\python.exe -m pytest -q -s tests/unit/test_windows_dependency_setup.py
Remove-Item Env:CG_TEST_CLEAN_INSTALL
```

The clean-install test checks installation separately from the launcher tests,
which share the invoking environment's installed dependencies for speed.
The clean-install test retains its new environment in a short temporary path and
prints that path after successful verification.

## Results and troubleshooting

Each invocation writes `.project-test-results/<UTC>-<random>/`: `summary.json`,
JUnit XML and logs. Full adds available coverage JSON/XML/HTML, function/route
inventories and V8 data. Historical evidence is not overwritten; Full refreshes
only the current coverage-audit document. Raw E2E directories contain generated
private settings and must remain ignored. Publish only reviewed, sanitized summaries.

| Status | Meaning |
|---|---|
| PASS | Executed check met its assertions |
| FAIL | Check or safety guard failed; investigate |
| SKIP | Deliberately omitted, not passed |
| NOT VERIFIED | Evidence or isolation is insufficient |

Exit 0 means executed required checks passed, 1 means failure, and E2E exit 2
means isolation confirmation is pending. Check `$LASTEXITCODE` immediately;
some task hosts collapse nonzero statuses to 1. SKIP/NOT VERIFIED can coexist
with successful execution. API/security subsets overlap the Python total.

- **Wrong Python:** install dependencies into Python 3.11; the runner deliberately
  rejects other minor versions. Preserve existing environments and local evidence.
- **Missing Node:** install Node 24 or place its directory on `PATH`; a skipped
  JavaScript suite is not a successful verification.
- **PowerShell blocked:** for the trusted current session only, use
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then retry.
- **Ports occupied:** inspect `netstat -ano`; do not terminate another project's
  processes. Use reviewed private port settings or the E2E runner's unique ports.
- **Docker unavailable:** start its Linux/WSL2 engine before a separately approved
  Docker check. Quick/Full do not require it; do not start original services just
  to make a readiness observation green.
- **Preservation/ownership failure:** retain manifests, evidence and resources;
  investigate the engine epoch and exact resource identities. Never erase a
  failure, rebaseline an existing session or override its latch.

### Dependent encounter demonstration and pending events

For a fresh Test 2, confirm the protected eight-service stack is healthy and
Hospital B `/recovery/status` reports `enabled: true` and a reachable Patient
service. Use unique synthetic IDs for each run. Capture a Hospital B
`patient_registration` with `prerequisite_ids: []`, then an
`encounter_creation` whose `prerequisite_ids` contains **the registration's event
ID**, never the encounter's own ID. Give both the same synthetic `patient_id`.
For example, with registration `CG-LECT-B-REG-NEW` and encounter
`CG-LECT-B-ENC-NEW`, the encounter must specify
`["CG-LECT-B-REG-NEW"]`. Verify the saved values with Hospital B `GET /events`.

Wait for both local rows to become `applied`, then refresh Dashboard Events and
Cloud Event History and check the same IDs and Hospital B ownership. Admission
control and retry backoff can delay an eligible event; a 200 response from the
controller's `/candidate` route only confirms candidate handling, not a grant.
If a prior malformed event remains pending, the total backlog will stay nonzero
even when the new chain applies. Verify the new IDs individually.

If an event remains pending, inspect Hospital B `GET /scheduler/inspect` and
`GET /events` first. `Dependency cycle or downstream of cycle` with the event's
own ID in `prerequisite_ids` is invalid captured test data: that immutable event
cannot replay. Preserve it for diagnosis and repeat with a new event ID and the
correct prerequisite. `Waiting for prerequisite acknowledgement` requires the
registration to apply first. For `Waiting for retry deadline` or `In flight`,
check the deadline/lease and allow the automatic loop to retry; for a MetaGuard
wait, inspect `outstanding_event_id`, the controller grant status and admission
decision. A quarantined event needs its validation error investigated. Do not
edit SQLite, force event status, bypass grants or clear persistent Docker data.
Capture the exact event IDs, local rows, Inspector reason and recovery status
when reporting an unresolved pending event.

### Local Monitoring traffic demonstration

After the standard Windows launcher has started the protected local stack, open
Monitoring and select **Prepare Demo Records**. Confirm Patient, Emergency and
Admin each report `applied`; pending synchronization is not a pass. The action
uses synthetic Hospital A capture and automatic MetaGuard replay, and repeated
clicks reuse matching captured events. Select 10 requests per API, then verify
10 successful reads for each service and inspect real live samples, mean/p95
latency, throughput, error rate and database health. The 60-second window can
expire while observing; generate a new bounded read batch if needed. SLO rows
are intentionally absent without a configured `CLOUD_LIVE_SLO_MS` threshold.
Do not infer an end-to-end pass from unit tests or Compose configuration alone.

## Read-only Docker observations

`-WithDocker` accepts only a local Unix socket or Windows named pipe. It renders
the explicit base Compose file using example settings and reads health/loopback
HTTP readiness for the eight expected `crisisguard-local` services. PostgreSQL
readiness is the existing `pg_isready` health result. It neither starts resources
nor verifies private overrides, clean-machine startup or database content equality.
Unavailable evidence is NOT VERIFIED; observed invalid/unhealthy configuration fails.
Response bodies and private environment values are not written to reports.

### Explicitly isolated PostgreSQL integration

`-WithIntegration` does not provision a database. Supply a separately provisioned,
empty scratch container through `$env:CG_TEST_POSTGRES_CONTAINER`. The read-only
preflight requires:

- A local Docker endpoint and a 12-64-character hexadecimal container ID.
- Label `org.crisisguard.test-isolation` containing a UUID.
- A running container with no persistent mounts (tmpfs allowed), one explicit
  IPv4 loopback PostgreSQL published port and password authentication.
- PostgreSQL system identity matching a read-only query inside that exact container.
- No existing user relations or nonstandard schemas in the target database.

Only then does the runner set `CONTROLLER_TEST_DATABASE_URL` for
`test_controller_postgres.py`, `test_project_e2e_postgres.py`, `test_cp8_postgres.py` and
`test_admission_deferral_postgres.py`. Their UUID schemas are created/cleaned in
that scratch database; no database/container is dropped. Inherited URLs,
localhost or schema isolation alone are not proof. Refusal is NOT VERIFIED,
not a PostgreSQL pass. The container stays owned by its operator.

### Real distributed-system E2E mode

E2E changes Docker resources. Use it only when that disposable run is intended:

```powershell
.\test.project.ps1 -EndToEnd
```

The runner builds a uniquely tagged image and starts eight services plus the
existing lost-response proxy. It allocates new credentials, loopback ports,
PostgreSQL/A/B SQLite volumes and internal-only backend/control networks. It
never reads the root `.env` or private override. PostgreSQL 16 must be available
locally; build dependencies stay inside the image. It rejects remote Docker
endpoints, host networking/mounts, external volumes/networks and remote service URLs.
No AWS clients or performance experiments are invoked.

Before taking a baseline it requires direct live-daemon access, an actual
`dockerd` process, matching kernel boot/PID/start-time identity and two advancing
engine timestamps. On Windows it uses Docker Desktop's bundled Linux CLI in the
`docker-desktop` WSL distribution. Cached inventory is insufficient; missing
access blocks execution without a fallback or automatic installation.

Two matching baseline snapshots record container lifecycle/images/mounts/network
attachments, volume metadata and all network identities, including built-in
networks. Engine restart or changed identities fail preservation. The failure
latches; confirmation cannot override it. Every disruptive action must re-prove
preservation and ownership of the exact disposable resources. Metadata equality
does not prove original database content equality.

The first stage captures eight events, covering four types at each hospital,
then checks protected replay, SQLite/PostgreSQL content, grants, Dashboard history
and duplicate replay. It injects no faults and performs no cleanup. HTTP/SQL
verification runs inside the owned Dashboard container so its internal networks
remain usable while hospital backend links are partitioned. Real browser behavior
and accessibility remain NOT VERIFIED.

After smoke, inspect its `isolation.json`, `SUMMARY.md` and `report.json`.
Interactive continuation requires the exact `<project>:<UUID>` token;
noninteractive execution returns exit 2 and retains the stack. Elapsed time is
not consent. Confirmation runs the **broader implemented suite followed by owned
cleanup**; there is no retain-after-faults CLI flag. The retained 2 November
selected continuation used a separately reviewed local driver.

```powershell
.\test.project.ps1 -EndToEnd -E2EResume '<results-directory>' -ConfirmIsolation '<project>:<UUID>'
# Cleanup only, after reviewing this session's manifest:
.\test.project.ps1 -EndToEnd -E2EResume '<results-directory>' -ConfirmIsolation '<project>:<UUID>' -E2ECleanupOnly
```

Never reuse historical confirmation tokens. The broader scenarios cover Cloud
contracts, ON/OFF scheduling, ageing/ties, FIFO/priority/bounded/RR/DRR,
A/B/both partitions, Cloud/controller outages, restarts, expiry/fencing, lost
ACK/commit responses, SIGKILL after commit, retries/quarantine, admission states,
experimental policies and Dashboard consistency. Fixture-triggered expiry/DB
errors are distinguished from actual network loss/process termination.
The [5 November complete E2E run](evidence/PROJECT_VERIFICATION_20261105.md)
passed all 18 executable checks, including cleanup. This verifies that run's
implemented functional scenarios; it does not establish exhaustive failure
coverage, browser acceptance or performance acceptance.

Fault contexts restore services and network attachments in `finally`; restoration
failure blocks deletion. Cleanup verifies labels/configuration and exact owned IDs,
preserves failure evidence, then removes only owned containers/networks/volumes.
It uses no prune, wildcard deletion or another project's `down`; the test image
is retained. Partial smoke can resume only its exact synthetic chain.
Private session/configuration files stay ignored, and public reports omit
credentials and clinical bodies. A failed historical baseline remains failed.

## Feature verification references


These references map implemented features to existing procedures and evidence.
They do not claim that every scenario was executed on the final submitted commit.

| Feature | Procedure or test reference | Evidence boundary |
|---|---|---|
| Both hospitals, four event types, persistence and protected recovery | [E2E mode](#real-distributed-system-e2e-mode), [Cloud API contract](CLOUD_API_CONTRACT.md) | [5 November complete E2E](evidence/PROJECT_VERIFICATION_20261105.md); final submitted-commit CI remains separate |
| Quick Event Generator and hospital capture | [Dashboard guide](DASHBOARD.md), [JavaScript capture tests](../tests/unit/test_dashboard_capture.cjs), [Quick Event tests](../tests/unit/test_dashboard_quick_events.cjs) | DOM-model tests do not establish actual browser acceptance |
| Scheduler eligibility, ageing, inheritance and Inspector | [Dependency demonstration](#6-dependency-and-priority-inheritance), [Bettina's review](reports/BETTINA_SCHEDULER_REVIEW_20261102.md) | Inspector selection is read-only; owner approval has its recorded revision scope |
| Monitoring, authenticated live reads and optional local test traffic | [Dashboard guide](DASHBOARD.md), [traffic tests](../tests/unit/test_dashboard_test_traffic.py), [metrics contract](reference/METAGUARD_METRICS_CONTRACT.md) | Optional settings are required; healthy read traffic is not overload evidence |
| Grant security, uncertain attempts, retries and quarantine | [Security checks](#7-security-and-failure-checks), [edge operator guide](../edge/README.md), [reliability tests](../tests/unit/test_metaguard_reliability_security.py) | [5 November E2E](evidence/PROJECT_VERIFICATION_20261105.md) passed its bounded fault scenarios; exhaustive failure/eligibility coverage remains unestablished |
| Admission states, fixed/AIMD and experimental policies | [Configuration](CONFIGURATION.md), [E2E scenarios](../infrastructure/scripts/project_e2e_scenarios.py), [W5/H5 review](reports/H5_W5_REVIEW_20261103.md) | [5 November E2E](evidence/PROJECT_VERIFICATION_20261105.md) passed functional state/pacing probes; W5 targets failed and H5 strict acceptance is unestablished |
| PostgreSQL/SQLite backups and isolated restores | [Backup runbook](runbooks/BACKUPS.md), [backup tests](../tests/unit/test_database_backup.py) | Preserve original databases; use the documented isolated restore procedure |
| Deployment health, image updates and rollback | [AWS health checks](runbooks/AWS_HEALTH_CHECKS.md), [update/rollback runbook](runbooks/DEPLOYMENT_UPDATES.md), [TX AWS assessment](evidence/tx-aws-review-20261101.json) | Historical local/AWS acceptance is not CI or deployment verification of a later revision |

For remaining submission gaps, use the
[requirements audit](reports/CORE_REQUIREMENTS_AUDIT.md#requirements-to-implementation-matrix) and
[evidence availability notes](EVIDENCE_INDEX.md#availability-and-historical-records).
The former `docs/TODO.md` and missing-evidence inventory are unavailable here.
Unavailable raw archives prevent independent raw reproduction; retained summaries
and historical test counts must not be relabeled as fresh executions.

## Manual demonstrations

Use a disposable isolated stack and unique synthetic event IDs. These procedures
retain their historical observations as examples; do not treat them as new runs.
Use the [Docker guide](DOCKER.md) for startup and keep protected replay enabled
unless a specific isolated procedure below calls for pausing it.

## 5. Offline capture, recovery and persistence

In a **disposable isolated** stack:

1. Start both hospital services with `AUTO_REPLAY=0` to prevent automatic delivery.
2. Submit a valid synthetic `POST /capture` event to Hospital A and Hospital B. Expect HTTP 201 and `pending` locally.
3. Confirm the shared PostgreSQL `events` count has not increased.
4. Restart both hospital containers while retaining their SQLite volumes. Confirm the same IDs remain `pending`.
5. Enable `AUTO_REPLAY=1` only for the isolated hospitals, using Compose configuration and scoped recreation without deleting volumes.
6. Confirm both IDs reach PostgreSQL and each hospital marks its local event `applied`.
7. Confirm the Cloud ledger has one row per expected event ID and site.

The 31 October verification followed this procedure for `CP7D_A_082907d4` and `CP7D_B_082907d4`. Both were applied after replay. These two events do not establish a general concurrency or throughput guarantee.

## 6. Dependency and priority inheritance

For the live dependency test, capture two synthetic Hospital A events while replay is disabled:

- A prerequisite patient registration with base priority 1 and no prerequisites.
- A dependent encounter with base priority 5 and `prerequisite_ids` containing the prerequisite event ID.

Before replay, read `GET /scheduler/inspect`. The expected active-policy snapshot is that the dependent is ineligible while waiting for prerequisite acknowledgement, and the eligible prerequisite inherits effective priority 5. This is a **read-only scheduler preview**, not an execution trace.

Enable isolated hospital replay and verify Cloud `events.applied_at` order and exact event IDs. On 31 October, the inspector selected `CP7D_PREREQ_7019925f` and PostgreSQL applied it at 02:46:54.121217 UTC, before `CP7D_DEPENDENT_7019925f` at 02:46:59.385141 UTC. This verifies the observed pair, not sustained starvation prevention.

For the final recorded demonstration, preserve the separate **inheritance ON/OFF comparison** using a disposable environment: `DEMO_A` base priority 1; `DEMO_B` priority 5 depending on A; and independent `DEMO_C` priority 3. ON should select A because its effective priority is inherited as 5; OFF should select C. Clearly distinguish Inspector selection from actual replay, and record the configured policy for each case.

## 7. Security and failure checks

| Scenario | Expected result |
|---|---|
| Duplicate hospital capture ID | HTTP 409 |
| Hospital A capture claiming `Hospital_B` | HTTP 403 |
| Manual hospital replay when disabled | HTTP 404 |
| Unauthenticated direct Cloud event POST in protected mode | HTTP 401 and no new ledger row |
| Invalid event schema or priority | HTTP 422 |
| Missing prerequisite on Cloud apply | HTTP 409 with dependency error |
| Cloud database unavailable | HTTP 503 under the documented failure test |

Only the first four denial scenarios were directly exercised during the 31 October isolated live session; the others have separate API/unit evidence. Token isolation, grant fencing, lost-ack reconciliation, controller restart and concurrent retries require their corresponding regression/integration evidence, not the four HTTP observations alone.

Manual replay is **disabled by default** in the protected stack. The older unauthenticated manual replay workflow is not valid for the current configuration. Do not stop original Cloud services to induce outages unless an approved isolated or disposable test plan is in place.

## Evidence and evaluation boundaries

- [5 November complete functional E2E and regression record](evidence/PROJECT_VERIFICATION_20261105.md):
  18 executable E2E checks passed with owned cleanup; browser, per-event log
  correlation and performance acceptance remain NOT VERIFIED.
- [2 November E2E verification](evidence/PROJECT_E2E_VERIFICATION_20261102.md):
  recorded functional scenarios; broader live fault plans and exact original
  PostgreSQL content equality remain outside that result.
- [31 October merged integration](evidence/CP8_MERGED_INTEGRATION_20261031.md)
  and [Cloud contract evidence](evidence/CP7D_CLOUD_API_CONTRACT_20261031.md).
- [Earlier integration source/checks](evidence/update4-integration-20261031.json)
  and [scheduler integration evidence](evidence/scheduler-policy-integration-20261031.json)
  retain their original counts and source revisions.
- [Evaluation overview](EVALUATION.md), [H5/W5 review](reports/H5_W5_REVIEW_20261103.md)
  and [raw evidence availability](EVIDENCE_INDEX.md#availability-and-historical-records).

Historical counts overlap and must not be added as unique cases. The original
proposal PDF is absent here; the R01-R24 transcription is not fresh authoritative
proposal verification. W5's failed drain/routine targets, H5's failed preservation
guard, provisional read capacity, full D2 limits and owner-review requirements
remain explicit. Verify Actions on the actual submitted commit.

### Live-request SLO telemetry

Set finite positive `CLOUD_LIVE_SLO_MS` to enable rolling 60-second live SLO
measurements. Authenticated live samples supply nearest-rank `live_p95_latency_ms`,
`live_slo_violation_count` and `live_slo_violation_rate`; unavailable populations
or a disabled threshold remain null where defined. `executor_queue_depth` reports
observed worker waiting. Use [the metrics contract](reference/METAGUARD_METRICS_CONTRACT.md)
for exact definitions. The retained [workload guide](../evaluation/workloads/README.md)
describes the authenticated synthetic read generator used in the studies.

Under default `throughput` health, live p95/SLO and executor queue measurements
are observational. Gated `pressure-experimental` health uses general/live p95,
SLO fractions and fresh executor pressure, with
`CRISISGUARD_EXPERIMENTAL_POLICIES=1`. See [configuration](CONFIGURATION.md)
and the [experimental decision rule](CONFIGURATION.md#monitoring-only-signals).
This does not change defaults or establish genuine-overload protection.
