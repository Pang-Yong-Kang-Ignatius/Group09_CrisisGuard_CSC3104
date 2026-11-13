# MetaGuard Cloud Telemetry API Contract

**Project:** CrisisGuard
**Owner:** Ignatius (Cloud Backend)
**Consumer:** Wayne (MetaGuard Controller)
**Updated:** 31 October 2026 (Checkpoint 7D contract review; historical results below retained)
**Status:** All three services expose the compatible legacy fields plus mixed/live p95, provenance sample counts, active-request and executor-worker gauges. MetaGuard consumes the legacy mean/error/rate/health fields; additive fields remain observe-only.

## 1. Purpose

This document defines how MetaGuard retrieves health and performance metrics from CrisisGuard's Cloud Backend.

The Cloud Backend is responsible for collecting and exposing measurements. MetaGuard is responsible for interpreting these measurements and making recovery-control decisions.

## 2. Available Endpoints

| Service | Local Endpoint | Method |
|---|---|---|
| Patient API | `http://localhost:8001/metrics` | GET |
| Emergency API | `http://localhost:8003/metrics` | GET |
| Admin API | `http://localhost:8004/metrics` | GET |

These URLs are for local development. Containerized services must use reachable Docker network addresses instead of assuming `localhost` refers to another container.

No request body is required.

## 3. Example Response

```json
{
  "live_p95_latency_ms": 10.2,
  "live_latency_sample_count": 4,
  "replay_latency_sample_count": 7,
  "unclassified_latency_sample_count": 1,
  "p95_latency_ms": 12.4,
  "latency_sample_count": 12,
  "observation_seconds": 60.0,
  "in_flight_requests": 0,
  "executor_queue_depth": 0,
  "executor_active_workers": 1,
  "executor_worker_limit": 40,
  "executor_queue_pressure": 0.0,
  "executor_queue_sampled_at": "2026-10-31T00:00:00+00:00",
  "average_latency_ms": 6.53,
  "error_rate": 0.0,
  "throughput_rps": 0.2,
  "window_requests": 12,
  "total_requests": 407,
  "failed_requests": 10,
  "window_seconds": 60,
  "uptime_seconds": 1876.34,
  "sampled_at": "2026-10-31T00:00:00+00:00",
  "service": "patient",
  "database_healthy": true
}
```

Illustrative response schema only: these values do not represent one measured response or guaranteed performance. Actual Checkpoint 7A samples are in the linked Docker evidence.

## 4. Metrics Definitions

| Field | Type | Description |
|---|---|---|
| `live_p95_latency_ms` | Float or null | Nearest-rank p95 for requests with authenticated live provenance; null when the live population is empty |
| `live_latency_sample_count` | Integer | Authenticated live samples in the same rolling window |
| `replay_latency_sample_count` | Integer | Authenticated replay samples in the same rolling window |
| `unclassified_latency_sample_count` | Integer | Samples without valid unambiguous provenance |
| `p95_latency_ms` | Float or null | Nearest-rank 95th percentile of completed monitored request durations in the rolling 60-second window, milliseconds; null when empty |
| `latency_sample_count` | Integer | Samples used by mean and p95; identical to `window_requests` |
| `observation_seconds` | Float | Effective throughput denominator in seconds: min(max(process uptime, 0.001), 60), reported to 6 decimals |
| `in_flight_requests` | Integer | Instantaneous monitored requests inside application middleware whose response headers/exception have not yet returned; not queue depth |
| `executor_queue_depth` | Integer | Tasks waiting for the process's shared AnyIO default thread-worker limiter at sampling time |
| `executor_active_workers` | Integer | Borrowed tokens from that limiter |
| `executor_worker_limit` | Integer | Total limiter capacity |
| `executor_queue_pressure` | Float | Waiting tasks divided by worker limit; dimensionless and not bounded to 1 |
| `executor_queue_sampled_at` | UTC ISO timestamp | Observation time for the executor gauges |
| `sampled_at` | UTC ISO timestamp | Sampling response timestamp; existing MetaGuard freshness input |
| `average_latency_ms` | Float | Average monitored request duration during the rolling window, in milliseconds |
| `error_rate` | Float | Fraction of monitored requests returning HTTP 5xx during the rolling window, between 0 and 1 |
| `throughput_rps` | Float | Number of monitored requests in the rolling window divided by the effective observation period |
| `window_requests` | Integer | Number of monitored requests in the current window |
| `total_requests` | Integer | Total monitored requests since service process startup |
| `failed_requests` | Integer | Total monitored HTTP 5xx responses since service process startup |
| `window_seconds` | Integer | Configured rolling observation window, currently 60 seconds |
| `uptime_seconds` | Float | Time elapsed since telemetry initialization |
| `service` | String | Service identifier: `patient`, `emergency`, or `admin` |
| `database_healthy` | Boolean | Result of the service's current database connectivity check |

HTTP `/health` and `/metrics` requests are excluded from performance counters.

Other requests, including `/openapi.json`, are currently included.

When no monitored requests exist in the window, the existing mean latency, error rate and throughput remain zero for compatibility; p95 is null and its sample count is zero. This does not necessarily indicate a healthy or idle workload under all conditions.

## 5. MetaGuard Responsibilities

The MetaGuard controller currently:

1. Polls the three telemetry endpoints at its configured interval.
2. Evaluates Cloud health using latency, error rate, throughput and database availability.
3. Implements Normal, Caution and Recovery states with dwell/healthy-sample rules.
4. Selects admission rate and in-flight limits.
5. Coordinates grant-controlled recovery with Edge replay.

Thresholds, polling intervals, hysteresis and admission algorithms are not defined by the Cloud Backend.

## 6. Cloud Backend Responsibilities

Ignatius's Cloud Backend will:

1. Maintain the three telemetry endpoints.
2. Measure HTTP request latency and failures.
3. Maintain rolling-window metrics.
4. Report PostgreSQL connectivity.
5. Preserve the documented response field names and types.
6. Assist with integration and troubleshooting.

## 7. Integration Considerations

- Metrics are maintained independently by each service process.
- Counters reset when a service process restarts.
- The current implementation uses in-memory storage.
- Multiple workers or replicas require additional metric aggregation.
- Requests should be made through endpoints reachable from the MetaGuard environment.
- Temporary network failures, HTTP errors and stale measurements must be handled by the controller.
- Database unavailability is explicitly indicated by `database_healthy: false` when the endpoint remains reachable.
- The API exposes process executor wait depth, not HTTP socket backlog, PostgreSQL waits, hospital backlog or retry depth. Admission commands remain controller endpoints, not Cloud metrics fields.
- Additional metrics and control interfaces require agreement with the responsible teammates.

## 8. Verification Summary

The following Cloud Backend behavior has been tested locally:

- Patient, Emergency and Admin API health.
- PostgreSQL event insertion and persistence.
- Duplicate-safe event processing.
- Missing prerequisite rejection.
- Cross-service event dependencies.
- 30 concurrent independent submissions across Patient, Emergency and Admin.
- 10 concurrent identical submissions recorded in the backend handoff; the fresh-ID acceptance invariant is one 201, nine duplicate 200 ACKs and one stored row. Retain raw output to substantiate the exact distribution.
- PostgreSQL outage handling and successful manual retry.
- Telemetry endpoint availability.
- Rolling-window request-count behavior.
- Exclusion of health and metrics requests.
- Request throughput and latency reporting.

The 31 October 2026 focused API-contract run and exact commands are recorded in
[Checkpoint 7D contract evidence](../evidence/CP7D_CLOUD_API_CONTRACT_20261031.md).
The concurrency checks above remain separate historical evidence, not additional
pytest tests.

## 9. Remaining Work

- Decide whether cloud-wide metric aggregation is required.
- Calibrate controller thresholds against controlled workload evidence; current values are implemented defaults, not capacity guarantees.
- Decide whether additional server, PostgreSQL, hospital backlog and retry signals are required.
- Perform end-to-end recovery and overload experiments.

**Integration status:** MetaGuard polls all three Cloud metrics endpoints and uses
the compatible legacy inputs. Live-only p95 and executor gauges are implemented
but not controller decision inputs.

## 10. Dashboard and Measurement Boundaries

The local development dashboard is implemented. The former `docs/TODO.md` is unavailable; see the [requirements audit's evidence boundaries](../reports/CORE_REQUIREMENTS_AUDIT.md#authoritative-requirement-source-and-evidence-boundaries) and [testing boundaries](../testing.md) for the retained verification limits. An initial display can consume the existing per-service fields without adding an API. It must show units, window, service and last successful poll, and distinguish stale/unreachable data from a legitimate zero-request window.

`throughput_rps` measures monitored HTTP requests, not uniquely applied events or completed workflows. Identical retries, validation/conflict responses and other included routes can affect request counts. `error_rate` is a fraction of 5xx responses; 409 and 422 responses are not counted as 5xx failures. `failed_requests` is cumulative since process startup, while `error_rate` is windowed. A healthy database does not establish end-to-end recovery health.

Hospital backlog, retry rate, controller state, grants and fairness/bypass measures
need separate producers/read contracts. `sampled_at` timestamps the metrics
snapshot and `executor_queue_sampled_at` timestamps worker gauges; neither is a
controller command. Per-process counters reset at restart and cannot be assumed
to be a cloud-wide total.

Do not infer throughput improvements, overload thresholds or research results from the illustrative JSON. Compare controlled experiments only after the relevant integration is implemented.

## Local Dashboard Status (21 October 2026)

The local dashboard displays each Cloud API's `/metrics` data and
service/database health. MetaGuard consumes the compatible legacy readings and
the Dashboard can display its read-only admission state through a separate
adapter. Cloud metrics still do not contain hospital backlog, retry rate, grants,
fairness or AWS-wide aggregation. Keep development controls off public AWS
interfaces.

## Local Dashboard and Offline Recovery Demo (21 October 2026)

**Status: Implemented and locally demonstrated; not an authenticated production interface.** Run the local dashboard with `python -m uvicorn dashboard.main:app --host 127.0.0.1 --port 8080 --reload` and open `http://127.0.0.1:8080`.

- **Cloud view:** Patient, Emergency and Admin health; per-process rolling telemetry (latency, 5xx error rate, request throughput and counters); synthetic event simulator; read-only PostgreSQL event history and event details.
- **Edge view:** Hospital A and B health, separate persistent SQLite event queues, local capture, pending/applied counts, pending-only queue, optional applied-event display, and in-place Refresh Queue button.
- **Manual replay:** Separate Replay Hospital A / Replay Hospital B controls invoke the existing dependency-aware scheduled replay through the hospital `/replay` endpoint. Successfully acknowledged events remain in SQLite with `applied` status, disappear from the *pending-only display*, and are visible in PostgreSQL history. They are **not deleted**.
- **Verified locally:** Dashboard 201 new and 200 duplicate request; missing-prerequisite 409; invalid-priority 422 **at the dashboard validation layer** (not proof of forwarding invalid input to Cloud API); database-unavailable 503; capture during PostgreSQL outage, Hospital A preserving three pending events and Hospital B preserving four across container restarts; manual patient-event replay to the cloud and queue refresh. Later screenshot showed additional applied records; counts above are the specific persistence checkpoint, not a permanent total.
- **Current scope:** Automatic dependency-aware replay and event-type routing are implemented; protected mode uses MetaGuard grants. The Dashboard has a read-only controller adapter. Hospital `/replay` remains disabled by default and separately authorized when enabled. Keep local development controls loopback-only and use synthetic data.

This dashboard is a visibility and manual testing tool, not evidence of completed controller, automated recovery, or end-to-end network partition resilience.

## Checkpoint 7A measurement details and compatibility

All additive fields are implemented once in `cloud/telemetry.py` and appear on Patient, Emergency and Admin `/metrics` responses. Existing field names, types and semantics are preserved. MetaGuard currently consumes `average_latency_ms`, `error_rate`, `throughput_rps`, `database_healthy`, `sampled_at`, `uptime_seconds`, `window_seconds` and request counters. It does **not** consume p95 or in-flight requests; no controller file or threshold changed.

Durations use `perf_counter()` around application `call_next`, in milliseconds. They include handler/database/dependency work to response-header availability, not client network delay, connection acceptance, upstream socket/server queues or complete streaming-body delivery. Sampling includes successful, 4xx, 5xx and uncaught-exception requests, including documentation and event-status endpoints. Exact `/health` and `/metrics` paths are excluded from latency, counters and the active gauge.

Completion samples use the process monotonic clock. At snapshot time `t`, retained records have completion timestamps in `[t-60,t]`; a record exactly on the cutoff remains until the next later sample. Timestamps and deque mutation are serialized under the same lock to preserve ordering with concurrent completions. Mean, p95, error rate and throughput all use this same recent population; lifetime counters do not decay. The current p95 uses all retained durations at full precision, sorted ascending: rank `ceil(0.95*n)`, one-based, with only the selected result rounded to two decimal places. A single observation produces its own latency; small sample counts are not evidence of a stable population tail. An empty p95 is undefined (`null`), never a fabricated measured zero. Idle expiry is checked even when no new request arrives.

The in-flight gauge is measured directly at middleware entry and released in the
same completion/failure recording path. It remains valid while completed-window
samples are empty. It measures active work after entry, including waits inside
application processing; it is **not** a queue length, remaining capacity,
saturation percentage or admission-grant count. The separate `executor_*`
gauges directly inspect the shared AnyIO thread-worker limiter's borrowed and
waiting tasks. They still do not represent the ASGI socket/server backlog,
event-loop waits, PostgreSQL waits or hospital backlog.

`p95_latency_ms` is **mixed monitored traffic**, not urgent-workflow recovery
latency. The later additive provenance fields separately report authenticated
live p95 and live/replay/unclassified sample counts using independent 32+
character role credentials. Missing, invalid, equal-role or contradictory
credentials remain unclassified and never authorize event admission. Executor
queue gauges measure the shared AnyIO worker limiter only. Adoption/calibration
of these additive fields by MetaGuard remains a separate decision.

Metrics are per process, in memory, reset on restart and are not aggregated across workers/replicas. Retaining exact samples costs O(n) space and sorting p95 costs O(n log n) per snapshot for `n` completed requests in the window. The 10,000-sample test verifies arithmetic/accounting, not sustainable production throughput. Database health remains a fresh connectivity probe on every metrics request; the probe's duration is not an application-latency sample. This checkpoint was verified in isolated Cloud-only Docker containers; existing running services were not updated.
