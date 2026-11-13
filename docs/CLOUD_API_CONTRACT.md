# CrisisGuard Cloud Backend API Contract
### Cloud Backend verification checkpoint - 31 October 2026

| Evidence | Recorded result and scope |
|---|---|
| Current focused contract verification | Patient, Emergency and Admin route/schema tests, telemetry/provenance tests, protected-admission security tests and Edge routing/reconciliation tests passed on 31 October 2026. See [Checkpoint 7D contract evidence](evidence/CP7D_CLOUD_API_CONTRACT_20261031.md). |
| Same-ID duplicate concurrency | **10 concurrent identical submissions** recorded in the backend handoff. Acceptance invariant for a fresh ID: one new application (201), nine duplicate acknowledgements (200), and one stored row; retain the raw run output with the repository evidence. |
| Cross-service concurrency | **30 independent submissions across Patient, Emergency and Admin**; successful application across the shared PostgreSQL ledger. This is a correctness check, not a throughput benchmark or a simultaneous dependent-chain test. |
| Dependency and transaction safety | Same-site prerequisites, cross-service dependency chains, cross-hospital rejection, conflicting IDs and rollback behavior verified locally. |
| PostgreSQL outage and recovery | All three health/event endpoints report 503 while the database is unavailable; health returns to 200 after recovery and an identical event can be manually retried. This does not establish automatic recovery or real network-partition tolerance. |
| Telemetry | All three `GET /metrics` endpoints available; rolling-window counters, latency/throughput reporting and exclusion of `/health` and `/metrics` from counters checked locally. |

The concurrency and outage rows above are retained historical results. They were
not rerun for the source-only 7D review and are not extra pytest pass counts.

Cloud telemetry, controller consumption, adaptive admission, event-type routing,
automatic Edge replay and protected grant reconciliation are implemented. Their
performance and failure limits remain separate from this API-shape verification.
See [MetaGuard metrics contract](reference/METAGUARD_METRICS_CONTRACT.md).

**Owner:** Ignatius (Cloud Backend)
**Updated:** 31 October 2026
**Status:** Event, lookup, health, telemetry and optional protected-admission contracts verified against current source and focused tests.

## 1. Services and Endpoints

| Service | Port | Write endpoint | Lookup endpoint | Accepted `event_type` | Monitoring |
|---|---:|---|---|---|---|
| Patient | 8001 | `POST /events` | `GET /events/{event_id}` | `patient_registration`, `encounter_creation` | `GET /health`, `GET /metrics` |
| Emergency | 8003 | `POST /events` | `GET /events/{event_id}` | `emergency_record` | `GET /health`, `GET /metrics` |
| Admin | 8004 | `POST /events` | `GET /events/{event_id}` | `patient_discharge` | `GET /health`, `GET /metrics` |

**Additive lookup update (7 November 2026):** all three services also support
`GET /events?event_id=...`. Supply the ID through the HTTP client's query-parameter
API to preserve reserved characters, spaces and Unicode exactly. The existing
path routes, response shapes and read-access rules remain unchanged. This source
update does not revise the dated verification results above.

Local development addresses:

- Patient: `http://127.0.0.1:8001/events`
- Emergency: `http://127.0.0.1:8003/events`
- Admin: `http://127.0.0.1:8004/events`
- Swagger: replace `/events` with `/docs`.

All three APIs write to the same PostgreSQL `events` table. This is an **event storage and acknowledgment API**, not yet a full patient/encounter/discharge business database.

## 2. Event Request Schema

Example for the Patient Service:

```json
{
  "event_id": "HOSPITAL_A_PATIENT_001",
  "site_id": "Hospital_A",
  "event_type": "patient_registration",
  "base_priority": 3,
  "prerequisite_ids": [],
  "payload": {"patient_id": "SYNTHETIC_001"}
}
```

| Field | Validation | Required in current Pydantic model? |
|---|---|---|
| `event_id` | String with at least one non-whitespace character, globally unique across services and hospitals | Yes |
| `site_id` | Exactly `Hospital_A` or `Hospital_B` | Yes |
| `event_type` | One of the event types supported by the receiving service | Yes |
| `base_priority` | Integer from 1 through 5 inclusive | Yes |
| `prerequisite_ids` | List of string event IDs | No, defaults to `[]` |
| `payload` | JSON object / dictionary | No, defaults to `{}` |

To ensure deterministic duplicate comparisons, **send all six fields explicitly** and resend exactly the same values on retries. Array order in `prerequisite_ids` should remain unchanged. Per the group proposal, **larger `base_priority` values mean greater urgency (5 highest, 1 lowest)**. Confirm priority ageing/inheritance and tie-breaking implementation details with Bettina, and exact payload fields with Afiq; do not silently change the shared event contract.

`event_id` is validated as non-empty/non-whitespace, but the current implementation does not normalize leading/trailing spaces. Items inside `prerequisite_ids` have no additional non-empty/unique validation, and list order is significant for duplicate comparison. The current Pydantic models are not strict: for example, compatible strings and booleans can be coerced to integers, and unknown object fields are ignored. Clients must still send the documented JSON types and must not rely on coercion or ignored fields. There are no enforced domain-specific payload rules.

`POST /events` accepts non-whitespace strings containing reserved URL characters.
Use `GET /events` with an `event_id` query parameter for IDs containing `/`, `?`,
`#`, `%`, spaces or Unicode, and for the dot-segment IDs `.` and `..`. Do not
interpolate these IDs into a URL path. The 7D review recorded the earlier lookup
encoding gap; the additive 7 November query route addresses it without tightening
accepted-ID validation or normalizing stored IDs.

Site ownership depends on deployment mode. Legacy mode (`METAGUARD_REQUIRED` not
equal to `1`) trusts the validated `site_id`. Protected mode authenticates the
hospital and requires its identity to match `site_id`, as described below.

### Example payloads (illustrative only)

```json
{"event_type": "encounter_creation", "payload": {"patient_id": "SYNTHETIC_001", "encounter_id": "ENC001"}}
```

```json
{"event_type": "emergency_record", "payload": {"patient_id": "SYNTHETIC_001", "emergency_type": "critical"}}
```

```json
{"event_type": "patient_discharge", "payload": {"patient_id": "SYNTHETIC_001", "discharge_status": "completed"}}
```

These examples are not enforced relational patient workflows. A discharge event can currently be inserted without verifying that a patient actually exists, unless its `prerequisite_ids` explicitly reference an applied event.

## 3. HTTP Results

### 201 Created: New event inserted and applied

```json
{
  "status": "applied",
  "event_id": "HOSPITAL_A_PATIENT_001",
  "site_id": "Hospital_A",
  "result": "applied"
}
```

### 200 OK: Identical event already applied

```json
{
  "status": "applied",
  "event_id": "HOSPITAL_A_PATIENT_001",
  "site_id": "Hospital_A",
  "result": "already_applied"
}
```

Both 200 and 201 confirm that **the matching event row** has `status = 'applied'` in PostgreSQL. In legacy mode, a client can resubmit the identical request and accept 200. In protected mode every write attempt is fenced first: a retry receives 200 only while its supplied grant is still valid and acknowledged. Lost/uncertain protected acknowledgements are resolved through controller reconciliation and `GET /events/{event_id}`, rather than by an unauthorised resend.

### 409 Conflict: Prerequisites missing

```json
{
  "detail": {
    "code": "MISSING_PREREQUISITES",
    "missing_ids": ["HOSPITAL_A_PRIOR_001"]
  }
}
```

The dependency must be applied in PostgreSQL for the **same** `site_id` before resubmitting. A missing prerequisite is not automatically queued by this service.

### 409 Conflict: Event ID reused with conflicting data

```json
{
  "detail": {
    "code": "EVENT_CONFLICT",
    "message": "Event ID already exists with different data or status"
  }
}
```

Changing the hospital, type, priority, dependency list or payload on a previously applied event ID causes a conflict. `event_id` is a global primary key, not hospital-scoped.

### 422 Unprocessable Entity: Invalid request

Common cases:

- Unsupported `event_type` on the selected service.
- Empty or whitespace-only `event_id`.
- Invalid `site_id`.
- `base_priority` outside 1-5 or a value Pydantic cannot coerce to an integer.
- Invalid field types, such as non-object `payload`.
- Missing a required field.

FastAPI/Pydantic returns field-level errors in `detail`. This represents request-schema validation, not all possible clinical/domain validation.

### 503 Service Unavailable: PostgreSQL not reachable

```json
{
  "detail": {
    "code": "DATABASE_UNAVAILABLE",
    "message": "PostgreSQL is not reachable"
  }
}
```

The endpoint catches psycopg `OperationalError` and returns 503. Database connection attempts specify `connect_timeout=3`; total request duration is not guaranteed to be exactly 3 seconds. Only retry after connectivity returns. **A 503 is not proof that every conceivable write failure rolled back**; clients should use the identical event ID and query/retry according to the eventual reconciliation policy.

### Protected MetaGuard admission errors

When `METAGUARD_REQUIRED=1`, `POST /events` additionally requires
`Authorization: Bearer <hospital token>` and `X-MetaGuard-Grant: <UUID>`.

- **503**: hospital token configuration is absent, malformed, weak, duplicated,
  or reuses the operator token.
- **401**: the Bearer credential is absent or invalid.
- **403**: the authenticated hospital does not match `site_id`, or the grant
  header is absent/not a UUID.
- **409** with string detail `Grant cannot authorize this event attempt`: the
  UUID is not an unexpired acknowledged grant bound to the same hospital,
  `event_id` and canonical event digest.
- **422** with string detail `Invalid event content`: protected digest creation
  rejects unsupported/non-finite content before persistence.

These authentication errors use FastAPI's ordinary `{"detail": "..."}` string
shape, unlike the structured event/database errors above. Telemetry provenance
headers are independent and never authorize a write.

### Event status lookup

`GET /events/{event_id}` and `GET /events?event_id=...` are read-only and return
**200** with:

```json
{
  "event_id": "HOSPITAL_A_PATIENT_001",
  "site_id": "Hospital_A",
  "event_type": "patient_registration",
  "status": "applied",
  "applied_at": "2026-10-31T00:00:00+00:00"
}
```

An unknown ID returns **404** with
`{"detail":{"code":"EVENT_NOT_FOUND","event_id":"..."}}`; a PostgreSQL
connection failure returns the structured **503 DATABASE_UNAVAILABLE** response.
The lookup currently has no grant/Bearer authorization in either mode. Replay
telemetry credentials classify trusted calls but do not provide access control.
Treat event IDs/status as private deployment data and restrict network exposure.
The query route requires `event_id`; omitting it returns **422**. It performs an
exact-ID lookup, not an event-history collection query. The Edge status client
keeps the original path request for ordinary URL-safe IDs and uses query
parameters for reserved characters or dot segments. Ordinary IDs therefore
remain compatible with older Cloud services. Special-ID lookup requires the
additive Cloud route; an older service's **405** remains an unknown outcome,
not evidence that the event was absent. Upgrade the lookup client and Cloud
services together before relying on special-ID reconciliation. Existing clients
may continue using the unchanged path route for ordinary IDs.

## 4. Dependency and Duplicate Rules

1. PostgreSQL stores globally unique `event_id` values.
2. For a new event, all `prerequisite_ids` must refer to rows with `status='applied'` and the same `site_id`.
3. Prerequisites can originate from any of the three cloud services.
4. An existing identical event returns `already_applied` even if its prerequisites are no longer available to the current request path.
5. An existing changed event returns `EVENT_CONFLICT`.
6. PostgreSQL transaction-scoped advisory locking serializes requests for the same event ID; the primary-key constraint is the final uniqueness guard.
7. The Cloud APIs do **not** choose scheduling order, implement priority inheritance or allocate grants. Hospital scheduling and the separate MetaGuard controller perform those roles; protected Cloud writes only validate/fence a supplied grant.

**Earlier manual verification (retained history):** Cross-service Patient -> Emergency -> Admin chain; cross-hospital prerequisite rejection; cross-service event ID conflict; duplicate retry; concurrent same-ID submission; transaction rollback.

## 5. Health Check Contract

All three services implement `GET /health`.

Healthy Patient Service example (`HTTP 200`):

```json
{
  "status": "healthy",
  "service": "patient",
  "database": "crisisguard"
}
```

For Emergency and Admin, `service` is `emergency` and `admin` respectively. During a PostgreSQL connection outage, `/health` returns **503** with the same `DATABASE_UNAVAILABLE` detail shown above. The root endpoint `GET /` only indicates that the web service is responding; use `/health` for database connectivity.

## 6. Routing and Team Integration

| Responsibility | Owner | Contract needed |
|---|---|---|
| Hospital event capture, offline persistence and replay | Afiq | Route events by `event_type`, preserve `event_id` and `site_id`, handle responses safely |
| Cloud ingestion and database consistency | Ignatius | Keep request/response shape stable, coordinate any schema changes |
| Local prerequisite ordering and inherited priority | Bettina | Use consistent applied/pending and dependency semantics |
| Admission grants and adaptive control | Wayne | Agree on grant enforcement location, admission feedback and telemetry |
| Network addresses, Compose and EC2 deployment | TX | Configure service discovery, database environment settings and ports |
| Workloads and measurements | Haris | Use unique synthetic IDs and record applied/blocked/retried outcomes |

Edge replay routes `patient_registration` and `encounter_creation` to Patient,
`emergency_record` to Emergency and `patient_discharge` to Admin. Hospitals can
run dependency-aware automatic replay; protected mode obtains and reconciles
MetaGuard grants. The Cloud APIs still do not implement patient-domain
consistency validation, dependency scheduling, generic public rate limiting or
domain projections.

## 7. Running and Testing

The Windows launcher now starts protected MetaGuard mode. Its Swagger pages are
still useful for schema inspection, but a direct `POST /events` requires a valid
hospital credential and acknowledged matching grant. For legacy-only API
examples use the explicitly unprotected root Compose mode in an isolated project.
Then visit:

- http://127.0.0.1:8001/docs
- http://127.0.0.1:8003/docs
- http://127.0.0.1:8004/docs

Swagger offers selectable scenarios for 201, 200, both 409 causes and 422. Run the 201 example *before* retry/conflict examples. Test IDs are persistent: subsequent runs with the same content may return 200 rather than 201.

See [testing guide](testing.md) for manual evidence and repeatable test instructions.

## 8. Security and Scope Limitations

- Legacy mode validates but does not authenticate Hospital A/B. Protected mode authenticates hospital identity and grant binding for writes.
- Both event-status lookup routes, `/health`, `/metrics`, `/docs` and OpenAPI are not protected by hospital credentials; enforce network-level access as appropriate.
- The default connection URL includes a **local development** account. Use environment-controlled credentials in any shared or cloud deployment.
- The cloud database currently contains an event ledger, not normalized medical-domain tables.
- Domain-specific patient/encounter/discharge rules and input sanitization beyond the current schema are pending.
- Focused current test commands and limitations are recorded in the 7D evidence linked above.
- Current measurements are local manual checks, not proof of AWS fault tolerance or research performance results.

**Status note:** This contract describes source and focused-test-confirmed Cloud
Backend behaviour as of 31 October 2026. The retained concurrency/outage rows are
historical evidence and were not rerun in this checkpoint.

## 9. Cloud Telemetry and Dashboard Integration

Patient, Emergency and Admin also expose `GET /metrics` on their respective API ports. See [METAGUARD_METRICS_CONTRACT.md](reference/METAGUARD_METRICS_CONTRACT.md) for the exact JSON response, rolling window and counter scope. `/health` remains the database-aware readiness check; `/metrics` supplies process-local performance observations and `database_healthy`.

Health/metrics requests are excluded from performance counters; other requests, including `/openapi.json`, are included. An event-request 409 or 422 is not a 5xx telemetry error. Throughput counts monitored requests, including duplicate attempts, rather than uniquely applied events.

The local development dashboard is implemented; see its checkpoint in this document. Existing metrics do not provide hospital backlog depth, retry counts, controller state, grants or a cloud-wide aggregate. Do not invent these fields or change event/ACK semantics to support a display without an agreed interface.

## Local Dashboard Consumer (21 October 2026)

The implemented local dashboard reads `/health` and `/metrics` for each Cloud API and displays a read-only view of cloud-applied PostgreSQL events through its **own** `/api/history` route. In legacy mode it can submit synthetic events through `POST /events`; protected mode blocks that direct path and uses hospital capture/grant-controlled replay instead. The Patient API does **not** expose a history collection endpoint. Edge replay now routes all four event types to their owning services. Local outage and opt-in replay controls have separate authorization; do not expose development controls on public AWS ports.

## Local Dashboard and Offline Recovery Demo (21 October 2026)

**Status: Implemented and locally demonstrated; not an authenticated production interface.** Run the local dashboard with `python -m uvicorn dashboard.main:app --host 127.0.0.1 --port 8080 --reload` and open `http://127.0.0.1:8080`.

- **Cloud view:** Patient, Emergency and Admin health; per-process rolling telemetry (latency, 5xx error rate, request throughput and counters); synthetic event simulator; read-only PostgreSQL event history and event details.
- **Edge view:** Hospital A and B health, separate persistent SQLite event queues, local capture, pending/applied counts, pending-only queue, optional applied-event display, and in-place Refresh Queue button.
- **Manual replay:** Separate Replay Hospital A / Replay Hospital B controls invoke the existing dependency-aware scheduled replay through the hospital `/replay` endpoint. Successfully acknowledged events remain in SQLite with `applied` status, disappear from the *pending-only display*, and are visible in PostgreSQL history. They are **not deleted**.
- **Verified locally:** Dashboard 201 new and 200 duplicate request; missing-prerequisite 409; invalid-priority 422 **at the dashboard validation layer** (not proof of forwarding invalid input to Cloud API); database-unavailable 503; capture during PostgreSQL outage, Hospital A preserving three pending events and Hospital B preserving four across container restarts; manual patient-event replay to the cloud and queue refresh. Later screenshot showed additional applied records; counts above are the specific persistence checkpoint, not a permanent total.
- **Current scope:** Automatic replay and multi-service routing are implemented. MetaGuard admission is visible through the Dashboard's read-only recovery adapter when configured. Hospital `/replay` remains disabled by default and requires a server-side bearer token when enabled. The local Dashboard/outage agent are development interfaces, not AWS management surfaces; keep them loopback-only and use synthetic data.

This dashboard is a visibility and manual testing tool, not evidence of completed controller, automated recovery, or end-to-end network partition resilience.

## Checkpoint 7A: additive telemetry extension (27 October 2026)

Event schemas, event/health response contracts, dependency checks, idempotency and database behavior are unchanged. Each service's existing `GET /metrics` now additionally reports:

- `p95_latency_ms`: nearest-rank recent-window percentile in milliseconds, or null if there are no samples.
- `latency_sample_count`: completed monitored requests in the same 60-second window.
- `observation_seconds`: effective throughput denominator in seconds (uptime, capped at 60 and floored at 0.001).
- `in_flight_requests`: current requests inside application processing, excluding health/metrics probes. This is not queue depth.

All previous fields and their semantics remain. This is a mixed-traffic application response-header latency percentile, not live-only p95, transaction-commit timing or workflow recovery p95. MetaGuard continues to consume its existing mean/error/rate/health inputs; no controller logic changed. No Cloud queue or queue-pressure metric is invented. See [measurement definitions](reference/METAGUARD_METRICS_CONTRACT.md#checkpoint-7a-measurement-details-and-compatibility), the [7A summary](evidence/cp7a-summary-20261027.json) and [isolated Docker observations](evidence/cp7a-docker-final-20261027.json) for the original scope and remaining instrumentation gaps.


### Live provenance and executor queue extension (27 October 2026)

The original mixed-traffic fields above retain their definitions. All three APIs
also expose `live_p95_latency_ms` and `live_latency_sample_count`, plus
`replay_latency_sample_count` and `unclassified_latency_sample_count`. These use
completion timestamps in the same inclusive rolling `[t-60,t]` second window.
Live p95 is exact nearest rank `ceil(0.95*n)`, rounded to two decimal milliseconds,
and null when no authenticated live samples exist (including after idle expiry).
Errors and validation failures are included in the authenticated population;
health/metrics are excluded. The sample counts expose classification coverage.

Trusted producers send `X-CrisisGuard-Traffic: live` or `replay` and the matching
`X-CrisisGuard-Telemetry-Token`. Configure independent, private 32+ character
`CLOUD_LIVE_TELEMETRY_TOKEN` and `CLOUD_REPLAY_TELEMETRY_TOKEN` values. Cloud needs
both; dashboard direct Cloud submissions receive only the live credential;
hospitals receive only replay. Compose passes optional empty defaults without
changing existing event admission. Both legacy and grant-based edge sends are
replay, including immediate draining while connected; local capture is not a
Cloud live request. Dashboard direct Cloud event submissions are live. Other
live workload generators must explicitly authenticate their provenance.
Unmarked requests, invalid credentials, identical role tokens, and live markers
with replay grants are unclassified, never implicitly live. Event payloads,
authorization, grants and response contracts are unchanged. These credentials
attest trusted producer provenance, not the truthfulness of arbitrary clients:
strip markers at public ingress, keep secrets out of browsers/logs, and use TLS
outside trusted local networks. Do not reuse MetaGuard credentials.

`executor_queue_depth` is the instantaneous count of tasks actually waiting for
the process's shared AnyIO default thread-worker capacity limiter.
`executor_active_workers` is borrowed capacity, `executor_worker_limit` is total
capacity, and dimensionless `executor_queue_pressure` is waiting/total capacity
(not bounded to 1). `executor_queue_sampled_at` records its UTC observation time.
This includes sync handlers, dependencies and other users of that shared limiter,
including concurrent monitoring work. The async metrics handler samples before
its own database probe enters the limiter. The probe can itself wait under load;
`sampled_at` therefore describes the pre-probe request-window snapshot, not the
later response time. Database health still comes from the actual probe.
These are process-local gauges, not rolling averages or calibrated overload
thresholds. They exclude server/socket backlog, event-loop waits and PostgreSQL
lock/connection waits. Database connections are opened directly; no application
connection-pool queue exists here. Hospital SQLite and controller admission
queues remain separate signals. No aggregate Cloud queue depth is claimed.

MetaGuard admission continues consuming the compatible mean/error/rate/health
fields; the additive live-only and executor fields do not change its decision
inputs. The extension requires credentials to populate classified metrics;
unconfigured callers remain compatible but unclassified. Source/test
verification alone does not imply a running-service deployment.
