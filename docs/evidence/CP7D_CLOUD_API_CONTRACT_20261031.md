# Checkpoint 7D Cloud API contract review

**Date:** 31 October 2026 (Asia/Singapore)  
**Scope:** Patient, Emergency and Admin Cloud APIs; source and focused mocked/local-fixture tests only  
**Result:** PASS for the documented source contract

## Findings corrected

- Added the implemented `GET /events/{event_id}` contract, including 200, 404
  and database-unavailable 503 response shapes.
- Distinguished legacy write admission from `METAGUARD_REQUIRED=1`. Protected
  writes require a hospital Bearer credential, matching `site_id`, a UUID grant
  header and an unexpired acknowledged grant bound to the canonical event.
- Documented the actual 401/403/409/422/503 protected-admission boundaries and
  that telemetry provenance credentials do not authorize writes.
- Replaced obsolete statements that grant enforcement, adaptive admission,
  automatic replay/reconciliation, multi-service routing and controller
  monitoring were unimplemented.
- Recorded current Pydantic limitations: compatible integer coercion, ignored
  extra fields, and no non-empty/unique validation for individual prerequisite
  IDs. API behavior was not changed.
- Updated the telemetry contract with current live/replay provenance counts,
  live p95, executor-worker gauges and sampling timestamps. Clarified which
  compatible fields MetaGuard consumes and which additive fields remain
  observe-only.
- Documented that event lookup, health, metrics and API documentation routes are
  not hospital-authenticated and therefore depend on network-level restriction.
- Recorded the accepted-ID/lookup gap: Cloud writes accept reserved URL
  characters, while the current lookup route/client cannot reliably reconcile
  such IDs. This was not changed because established workloads use URL-safe IDs
  and the task requested documentation correction unless a critical defect was
  confirmed.

## Focused tests

```text
python -m pytest tests/integration/test_cloud_apis.py -q
42 passed

python -m pytest tests/integration/test_cloud_telemetry.py -q
18 passed

python -m pytest tests/unit/test_cloud_live_metrics.py \
  tests/unit/test_metaguard_reliability_security.py \
  tests/unit/test_edge_routing_and_recovery.py \
  tests/unit/test_metaguard_replay.py -q -p no:cacheprovider \
  --basetemp .pytest-tmp-cp7d-cloud-contract-20261031
99 passed
```

Total: **159 passed**. The only test warning was the existing Starlette
`TestClient`/httpx deprecation.

The first run of the four unit files produced 22 setup errors because pytest
could not traverse the host's default `%TEMP%\pytest-of-USER` directory. A
workspace-local base directory still required running the same source-only test
command outside the filesystem sandbox. The corrected run passed all 99 tests;
the initial setup errors were not application failures.

## Safety and limitations

No Docker command, live PostgreSQL connection, service start/stop, event write,
credential change, AWS action, commit or push was performed. Existing Docker
containers and volumes, stopped hospitals, `cp7c-inspector-demo` and
`cp7c-recovery-demo` were outside the test path.

The review verifies current routes, schemas, response mapping, mocked database
boundaries, protected-admission preflight, telemetry arithmetic/provenance, Edge
routing and reconciliation behavior. It did not rerun the real-PostgreSQL
concurrency, grant-fence transaction races, database outage or AWS checks.
Those remain separately recorded integration evidence and belong to the later
final Docker/security verification tasks.

The Cloud request models remain coercive and the accepted event-ID grammar is
broader than the lookup URL can safely represent. Tightening either behavior is
a compatibility decision for the later security/API-contract task, not an
undocumented change in this review.
