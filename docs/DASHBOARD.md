# CrisisGuard local Dashboard

Updated 31 October 2026. The Dashboard is a loopback-published development and
demonstration console for the Cloud APIs, both hospitals, Scheduler Inspector,
automatic recovery and MetaGuard observation. It is not an AWS management
surface or a clinical interface. Use synthetic data only.

The protected Windows launcher now starts Dashboard as part of an eight-service
stack. The bare root Compose command remains a separate legacy seven-service
mode. Existing dated D1/D2/7C verification is preserved below and is not
presented as a test rerun during this documentation update.

## Audit and page map

The original sidebar linked to anchors in one long document. Existing controls already covered Cloud event scenarios, hospital capture/replay, queues, scheduler previews, PostgreSQL history and per-service telemetry. Missing dashboard integrations were hospital `/recovery/status` and controller `/admission/status`. Queue counts initially displayed zero before a response, and failed polling could retain stale counts. Both displays now show Unknown when observations are unavailable. Overlapping Cloud/hospital polling is suppressed while a previous refresh is pending.

| Workspace | Existing sections and additions |
|---|---|
| Overview (`#overview`) | Cloud health; hospital connection, pending/applied and backlog summary; per-service latency, throughput, error rate and database health |
| Events & Hospitals (`#events`) | Original hospital selector/capture and result, A/B queues, PostgreSQL history; expandable Cloud API scenarios and scheduler inspector |
| Recovery & MetaGuard (`#recovery`) | Original temporary agent authorization and outage/restore controls; read-only admission/recovery snapshots; compact Quick Event Generator for outage demonstrations; full capture stays in Events & Hospitals |
| Monitoring (`#monitoring`) | Original `/api/metrics` cards: latency, error rate, throughput, window/lifetime requests, database health |
| Settings & Diagnostics (`#settings`) | Connection summary, polling/configuration guidance, troubleshooting and existing HTTP response guide |

Only one workspace is visible. Desktop navigation remains fixed; below 740px a Menu button expands navigation. Routes support refresh and browser history; old section fragments map to their new workspace. Forms retain entered values across navigation. Advanced tools are collapsible to keep capture and history prominent.

## Run the current protected stack

Follow the prerequisites and private-token setup in [DOCKER.md](DOCKER.md), then
from the repository root run:

```powershell
.\start.project.bat
```

The launcher starts PostgreSQL, Patient, Emergency, Admin, Hospital A/B,
MetaGuard and Dashboard, waits for health, creates hospital SQLite snapshots,
starts the restricted host outage agent, and opens `http://localhost:8080`.
It also prepares private local telemetry credentials and enables Monitoring's
Generate Test Traffic control through the local-only Compose overlay.
Monitoring's **Prepare Demo Records** action captures a synthetic Patient,
Emergency and Admin dependency chain at Hospital A; wait for all three to apply
before generating read-only traffic. Unconfigured SLO rows are hidden.
Its fixed project is `crisisguard-local`; do not use it on retained pending data
merely to test startup because hospital automatic replay is enabled by default.

For a standalone Dashboard with services already available on their default
host ports:

```powershell
.\.venv311\Scripts\python.exe -m uvicorn dashboard.main:app --host 127.0.0.1 --port 8080
```

Standalone PostgreSQL history requires the correct server-side `DATABASE_URL`.
Service destinations use the `DASHBOARD_*_URL` variables. MetaGuard observation
requires server-side `DASHBOARD_CONTROLLER_URL` and
`DASHBOARD_CONTROLLER_TOKEN`; never place credentials in frontend files, URLs
or browser storage.

The bare `docker compose up -d --build --wait` command starts the legacy
seven-service mode without MetaGuard. In that mode the expandable direct Cloud
API scenarios are available. When `METAGUARD_REQUIRED=1`, the Dashboard hides
those scenarios and `/api/events/{service}` returns 403; create events through
Hospital A/B capture so grant-controlled replay can deliver them.

If the launcher did not start it, run the host agent manually:

```powershell
.\.venv311\Scripts\python.exe -m infrastructure.scripts.local_outage_agent --allow-local-demo
```

1. Open Recovery & MetaGuard. Enter the temporary token from the agent terminal and authorize local controls.
2. Simulate Cloud Outage. The agent restricts operations to the
   `crisisguard-local` project's Patient, Emergency and Admin containers; it
   preserves PostgreSQL, both hospitals, Dashboard and MetaGuard.
3. Use Quick Event Generator: select Hospital A or B and 1–10 events, then generate. Each event is an independent patient registration with fixed priority 2 and no prerequisites. For encounters, custom priorities or dependencies, open the full form in Events & Hospitals. Pending events are durable in each hospital's SQLite queue.
4. Restore Cloud. Observe hospital recovery snapshots and last replay results. Open Events & Hospitals to verify queue states and corresponding PostgreSQL records. A sampled backlog or old drain timestamp alone does not prove a newly created event was applied.

Automatic replay is the default. Manual replay retains its existing explicit server-side enablement and authorization requirements. The outage token stays in the original closure and is cleared from the input after authorization. Never use volume deletion to demonstrate outages.

For optional Dashboard manual replay, enable `ENABLE_MANUAL_REPLAY=1` only with the existing server-side `MANUAL_REPLAY_TOKEN` and a separate, at least 32-character `DASHBOARD_REPLAY_OPERATOR_TOKEN` on the Dashboard. The operator enters that token for each replay; the input is cleared after use. The Dashboard checks it before forwarding the server-held hospital token. Missing, weak or reused operator tokens leave replay unavailable. Keep all three variables in private deployment configuration. The default deployment remains disabled; rollback is to set `ENABLE_MANUAL_REPLAY=0` or remove the Dashboard replay operator token.

## MetaGuard and recovery observation

`GET /api/recovery` reads each configured hospital's `/recovery/status`. With
`DASHBOARD_CONTROLLER_URL` and `DASHBOARD_CONTROLLER_TOKEN`, it also reads the
controller's `/admission/status` using `x-cloud-token`. The protected Compose
overlay configures the controller URL and passes the private operator token to
the Dashboard container. The bare seven-service stack has no controller.

The adapter only returns allowlisted observation fields, suppresses upstream error bodies and never returns its token or controller settings. There are no controller mutation controls. States are displayed only when supplied as Normal, Caution or Recovery by the existing API. Disabled adaptive admission is shown explicitly. Missing configuration and failed reads remain Not configured/Unavailable with Unknown state.

Limitations: the admission endpoint does not expose aggregate active grants or fairness. Hospital event queues expose pending/applied, not a durable failed-event category. The UI explicitly labels these missing fields instead of reporting zero or fabricating values. Recovery counts are sampled telemetry rather than per-event completion guarantees.

Monitoring cards show per-service mean and mixed-traffic p95 latency,
throughput, 5xx error rate, in-flight requests, request counts and database
health. Cloud metrics also expose authenticated live/replay provenance and
executor-worker gauges, but the current Dashboard does not present all of those
additive fields and MetaGuard does not use them as decision inputs. Do not call
in-flight requests queue pressure or mixed p95 live-only p95.

## Security boundaries

- Compose publishes Dashboard on `127.0.0.1:8080`; middleware validates the
  Host header and rejects cross-origin/cross-site requests. Do not publish it to
  untrusted networks.
- Controller and hospital replay credentials remain server-side. The recovery
  adapter returns allowlisted fields rather than upstream bodies or settings.
- The outage-agent token is temporary and held only in the current page's
  memory; reloading clears authorization. Agent operations are limited to the
  three identified Cloud API containers in `crisisguard-local`.
- Dashboard manual replay is disabled by default and requires both the hospital
  replay token and a distinct 32+ character Dashboard operator token when
  deliberately enabled.
- Protected mode blocks direct Dashboard Cloud writes. Cloud health, metrics and
  event lookup remain unauthenticated read endpoints, so Docker/network exposure
  is still part of the security boundary.

## Current verification boundary

The 31 October 7D isolated startup verified all
eight containers healthy, Dashboard health, all three Cloud service cards, both
empty hospital queues and an available MetaGuard adapter with fresh telemetry.
It used fresh volumes and `AUTO_REPLAY=0`; it did not exercise browser capture or
replay. The host was an existing Windows development machine, not a clean install;
the batch launcher was not run against its retained `crisisguard-local` project.
The checkpoint record remains in Git history.

The retained [7C deployment evidence](evidence/cp7c-dashboard-replay-auth-deploy-20261030.json)
records source/deployed asset parity, secret scanning, Dashboard-only deployment,
read-only services/metrics/recovery responses, and user-observed exact-ID
Pending-to-Applied, MetaGuard/controller agreement, outage/restore, refresh
progress and Cloud Unavailable-to-Healthy recovery. The explicit timeout message
passed automated tests but was not visually checked. These dated observations
do not establish performance targets, independent A/B link partitions, a
per-grant execution trace or global fairness.

## Historical verification on 27 October 2026

- Focused Python suite: **65 passed, 0 failed** (54 existing dashboard/offline/smoke/agent tests + 11 new recovery adapter/security cases). One existing FastAPI/Starlette test-client deprecation warning.
- JavaScript: **5 capture tests + 4 navigation tests + 1 queue regression script passed**, zero failures. Navigation tests execute the actual routing code against a minimal DOM model; they are not browser tests.
- JavaScript syntax checks, Python compilation, HTML nesting/81 unique IDs/five-workspace validation and Git whitespace check: passed.
- Direct HTTP checks against the standalone dashboard passed: HTML served, recovery sources reported Unavailable/Not configured and cross-origin monitoring requests returned 403. These checks did not verify upstream services.
- Initial sandbox runs hit Windows pytest temporary-directory permissions and Node child-process EPERM. Python rerun outside the sandbox and direct Node test-file execution resolved those execution issues.
- Docker status inspection succeeded outside the sandbox and reported **no running project services**. No stack was started and no databases were modified. Live Cloud health/metrics, A/B capture/replay, scheduler behavior, PostgreSQL history, agent authorization and outage/restore were **NOT TESTED end to end** in this session.
- Browser walkthrough attempt was blocked by a computer-use app approval timeout. Desktop/mobile visual rendering, responsive interaction and screenshots remain **NOT TESTED**. The existing opt-in original-outage browser test now navigates to `#recovery`, but was not run.
- AWS MetaGuard Caution/Recovery transitions remain **NOT TESTED**.

Reproduce focused tests (choose a fresh writable temporary directory if needed):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_offline.py tests/unit/test_smoke_dashboard.py tests/unit/test_local_outage_agent.py tests/unit/test_dashboard_recovery.py -q -p no:cacheprovider --basetemp=.venv/dashboard-verification-new
node tests/unit/test_dashboard_capture.cjs
node tests/unit/test_dashboard_queue.cjs
node tests/unit/test_dashboard_navigation.cjs
node tests/unit/test_dashboard_quick_events.cjs
node tests/unit/test_dashboard_sync.cjs
node --check dashboard/static/app.js
node --check dashboard/static/workspace.js
node --check dashboard/static/outage.js
```

This 27 October checkpoint predated the later 7C browser/deployment evidence
summarized above. Its unverified statements remain historical scope, not the
current aggregate status. Responsive narrow/mobile rendering, explicit timeout
text, and a live selected dependency example remain unverified. Do not treat an
Inspector preview as an executed grant/replay decision or a Dashboard admission
card as proof of controller enforcement.

## Quick Event Generator follow-up verification

The full capture form now remains exclusively in Events & Hospitals. Recovery offers hospital selection, a count bounded to 1–10, a Generate button, and per-event IDs/results with successful and failed-or-unconfirmed capture counts. It uses the existing ID helper and registration payload convention (`patient_id`, `test_source`) through `POST /api/hospitals/{hospital}/capture`. The existing dashboard proxy and hospital SQLite capture implementation are unchanged. The generator does not submit directly to Cloud or request replay; during an outage captured events remain pending until the existing replay process can synchronize them.

Each event is attempted once, sequentially, with a 15-second response timeout and no automatic retries. The controls are disabled during a batch, including repeated-click protection. Only an HTTP-success response wrapping hospital HTTP 201, pending status and the matching event ID counts as confirmed capture. Other responses count as failed or unconfirmed, and the event ID remains visible. A timeout or lost response may follow a successful SQLite write: inspect the queue using that ID before starting a new batch. A deliberate new batch creates fresh event IDs.

Verified in this follow-up: **17 generator tests, 4 navigation tests, 5 existing capture tests and 1 existing queue regression script passed; 0 failures**. Coverage includes both hospital identities, ten-event batches, valid registration fields, distinct IDs, invalid counts/hospital, partial failures, malformed/lost responses, unexpected acknowledgments and in-flight repeated clicks. Syntax and Git whitespace checks passed. These are JavaScript tests using simulated HTTP responses, not a browser or a live outage. Live SQLite persistence through outage/replay and visual browser acceptance remain NOT TESTED in this follow-up. No backend, outage authorization, schema, scheduler, MetaGuard or Compose changes were made for this follow-up.

## Automatic synchronization status

After hospital capture, the full form and quick-generator result show **Stored Locally (Pending Synchronization)**. A shared tracker refreshes existing `/api/hospitals` and `/api/history?limit=100` endpoints approximately two seconds after the previous refresh completes while any tracked event remains unconfirmed. It refreshes queue counts, the queue table and Cloud Event History without a browser reload. The history dropdown still controls how many fetched records appear in the table.

**Applied to Cloud** requires the exact captured event ID, site and event type in PostgreSQL history with status `applied` and an applied timestamp. A hospital applied count, empty pending queue or HTTP 201 alone is insufficient. During outages or temporary API failures, the tracker keeps the last confirmed state, leaves unconfirmed events pending, and reports unavailable observations separately. Capture HTTP 4xx responses show rejection; lost responses and other uncertain capture results remain unconfirmed. Existing queue/history APIs expose no durable failed-replay state, so no synchronization failure is invented.

Events & Hospitals and Recovery each have a shared synchronization status panel, a Last updated indicator and a Refresh synchronization fallback. The topbar Refresh also uses the same request loaders. Queue/history loaders coalesce concurrent calls, including manual refreshes, and reject obsolete history responses after display-limit changes. Queue/history requests time out after 15 seconds. There is one event-polling timer: two seconds while tracking pending events, otherwise five seconds on Events/Recovery. Hidden tabs and other workspaces have no event-polling timer; navigation/pagehide invalidates in-flight tracking results. Returning resumes observation. In-flight shared reads can finish after navigation, but cannot change tracked statuses using an obsolete workspace observation. Existing Cloud health, agent and MetaGuard monitoring cadences are unchanged.

Tracking is in memory and survives workspace navigation, but not a browser reload. The existing history endpoint returns at most 100 recent records and has no dashboard per-ID lookup. If a record leaves that window before observation, the tracker keeps it unconfirmed rather than guessing application or failure. Pending synchronization may also reflect automatic replay being disabled or prerequisites not yet applied. These limits are shown in the UI.

Verification for this change: **8 synchronization tests, 5 capture tests, 17 quick-generator tests, 4 navigation tests and 1 queue regression script passed; 0 failures**. The synchronization tests cover exact pending-to-applied confirmation, outages, API exceptions, mismatched identities, hidden/navigation/page lifecycle cleanup, in-flight duplicate refreshes, stale responses, result ownership and real loader request coalescing/history-limit changes. Capture tests verify tracker registration and accurate rejection displays; generator tests verify tracking of confirmed captures only. Four changed JavaScript files pass syntax checks; HTML nesting/unique IDs and Git whitespace checks pass. These tests use simulated HTTP responses and timers. Live PostgreSQL/replay/outage and browser visual testing were not performed for this change. No backend/API contracts, schema, replay, scheduler, MetaGuard or outage safety code changed.
