# Hospital Edge: Capture and Protected Recovery

**Owner:** Afiq

**Project:** CrisisGuard, CSC3104 Group 09

**Reviewed:** 31 October 2026

The Edge services represent **Hospital A** and **Hospital B**. Each hospital has a separate FastAPI application and persistent SQLite database. Events can be captured while Cloud services are unavailable and remain pending until their application is confirmed. The dependency-aware scheduler orders eligible events; MetaGuard protects replay with controller grants when enabled.

This is an academic prototype using synthetic records, not a clinical system.

## Architecture and responsibilities

| Component | Responsibility |
|---|---|
| `capture_api/main.py` | Hospital HTTP endpoints, validation, site identity and recovery-loop startup |
| `capture_api/cloud_client.py` | Routes event types to Patient, Emergency or Admin Cloud APIs |
| `capture_api/auto_replay.py` | Automatic replay polling, connectivity observations and recovery status |
| `capture_api/scheduled_replay.py` | Dependency-aware selection, protected transmission and retry/quarantine handling |
| `capture_api/grant_client.py` | MetaGuard candidate, grant, acknowledgement and reconciliation interactions |
| `database/db.py` | Durable SQLite events, claims, retry deadlines and quarantine state |
| `scheduler/` | Dependency eligibility, priority ageing, inheritance and read-only Inspector |

Hospital A and B use **distinct SQLite volumes**. Applied events are held in the central Cloud PostgreSQL database. Do not substitute one hospital's database or identity for the other.

## HTTP endpoints

| Endpoint | Purpose |
|---|---|
| `POST /capture` | Validate and persist a hospital event locally; a successful capture is initially `pending` |
| `GET /events` | Read local event records and their current statuses |
| `GET /scheduler/inspect` | Read-only snapshot of the scheduler's current decision; a preview is not proof of executed replay |
| `GET /recovery/status` | Observe replay configuration, connectivity, backlog and recorded recovery state |
| `POST /replay` | Optional manual replay; disabled by default and protected when enabled |
| `GET /` | Basic hospital service information; used by local health checks |

The Dashboard provides higher-level monitoring and guided controls. Use the API responses and authoritative Cloud history to establish whether a specific event was actually applied.

## Capture and routing

An event contains an `event_id`, event type, priority, prerequisite IDs and payload. Priority is in the range **1–5**, where 5 is most urgent. An event whose prerequisites are not yet confirmed applied is not eligible to complete ahead of them.

| Event type | Cloud owner |
|---|---|
| `patient_registration` | Patient API |
| `encounter_creation` | Patient API |
| `emergency_record` | Emergency API |
| `patient_discharge` | Admin API |

Unknown event types and invalid capture fields are rejected rather than silently sent to another service. The configured `SITE_ID` binds each hospital to its own identity; attempting to capture under the other hospital's ID is rejected.

During a Cloud outage, successful local capture does **not** imply Cloud application. Check the event's local status and eventual authoritative Cloud record separately.

## Automatic and protected replay

The hospital recovery loop is enabled with `AUTO_REPLAY=1` and checks service reachability periodically. When work is eligible, the scheduler considers dependencies, priorities and retry exclusions. Priority inheritance can raise a prerequisite's effective scheduling priority without changing its stored base priority.

In **protected MetaGuard mode**, hospital replay additionally depends on controller-side authorization. Grants, acknowledgements and uncertain attempts are tracked so an ambiguous transmission cannot simply be sent again. A Controller or Cloud outage must not cause locally captured events to be discarded.

Normal project configuration uses the current Compose files and MetaGuard overlay. Experimental scheduling or admission settings are separate, explicitly opt-in configurations; they must not be assumed active merely because their code exists.

The `replayable_backlog` field represents events currently eligible for the replay loop's backlog accounting. It is **not** necessarily the total number of pending events: retry delays, outstanding claims and quarantined events can affect eligibility.

The optional `bounded-deferral` retry policy is experimental. It requires
`CRISISGUARD_EXPERIMENTAL_POLICIES=1` and `METAGUARD_ENABLED=1`; the default is
`failure-backoff`. Only a controller-confirmed fresh rate or in-flight-cap
denial can issue a 1–30 second advisory retry. A missing/stale telemetry result,
unknown grant, connection failure, lost ACK/send or other uncertain outcome
keeps the existing exponential backoff and authoritative reconciliation. The
opt-in path records a durable SQLite deferral/cooldown without treating it as a
grant or changing the event's prerequisite eligibility. A process-local lock
does not prove a multi-process polling bound. The single seed-60202 pilot
improved drain by 46.60% but worsened Hospital B routine p95 by 40.95% and
whole-run SLO violations by 1.726 percentage points; see the
[diagnosis](../docs/reports/AIMD_RECOVERY_DIAGNOSIS_20261101.md#independent-bounded-deferral-pilot-seed-60202)
and [raw assessment](../docs/evidence/aimd-deferral-pilot-review-20261101.json).

## Operator guide: event and recovery states

| Observation | Meaning | Safe response |
|---|---|---|
| `pending` | Locally stored; Cloud application not confirmed | Inspect connectivity, prerequisites, retry state and controller admission; leave durable state intact |
| `applied` | Successful authoritative application has been acknowledged locally | No retransmission needed |
| Retry delayed | Transient failure; attempt waits for a retry deadline | Wait for the retry deadline. Each failure doubles the delay (2, 4, 8, 16 seconds, then capped at 30 seconds). The actual wait is a random 50-100% of that value. Do not clear it manually. |
| Quarantined | Event is excluded from normal replay after a permanent or invalid outcome | Inspect the reason and dependent events; do not force an ACK |
| Uncertain attempt | Cloud may have received the request, but its outcome is not authoritative | Preserve claim/grant state and use protected reconciliation; **do not blindly retransmit** |
| Waiting for admission | Controller authorization or pacing is not yet available | Check MetaGuard state, telemetry freshness and grants; do not bypass authorization |

An event's *local* `pending`/`applied` status and its *recovery* state (claim, retry, quarantine or uncertainty) are related but not interchangeable. A read-only Scheduler Inspector preview does not guarantee the controller will admit the selected event.

### Safe restart and troubleshooting

1. Record the relevant hospital's `GET /events`, `GET /recovery/status` and Scheduler Inspector observations before changing anything.
2. Confirm which Cloud service or Controller is unavailable. A healthy hospital API does not guarantee replay can proceed.
3. Preserve the hospital's SQLite volume, the Cloud PostgreSQL volume and all recovery journals.
4. Restart **only the affected service** using the existing Compose project, without volume removal or database replacement.
5. Wait for service health, then inspect `GET /recovery/status` for outstanding work, reconciliation and errors.
6. Confirm completed event IDs against the owning Cloud API and PostgreSQL, including prerequisite order. Do not infer completion solely from a zero replayable backlog.
7. Escalate quarantined or unresolved attempts for investigation. Never manually mark them applied to make a test pass.

After a crash or restart, once the Cloud is reachable, the hospital checks each event that was left in flight. If the Cloud already applied it, it is marked applied locally. If the Cloud has no record, its claim is released and it is replayed later. If the outcome is unclear, it is left in place. The result appears in `reconciled_on_startup` in `GET /recovery/status`.

Never run `docker compose down -v` against a project whose hospital or Cloud data must be preserved. Do not delete SQLite files or reset grant state as a recovery shortcut.

## Configuration

Configuration is provided through `compose.yaml`, `infrastructure/docker/compose.metaguard.yaml` and private environment values. Consult the root README and Docker guide for the complete supported startup procedure.

| Setting | Purpose |
|---|---|
| `SITE_ID` | Hospital identity (`Hospital_A` or `Hospital_B`) |
| `HOSPITAL_DB_PATH` | Persistent SQLite database location |
| `AUTO_REPLAY` | Enables/disables the automatic replay loop |
| `AUTO_REPLAY_INTERVAL_SECONDS` | Recovery polling interval; normally 5 seconds |
| `ENABLE_MANUAL_REPLAY` | Explicitly enables the otherwise-disabled manual replay route |
| `SCHEDULER_ENABLE_INHERITANCE` | Priority inheritance on (`1`, default) or off (`0`) |
| `SCHEDULER_AGEING_INTERVAL_SECONDS` | Seconds per priority-ageing step; default 600 |
| `REPLAY_SELECTION_POLICY` | `priority` (default) or `fifo`; an invalid value stops startup |
| `REPLAY_INHERITANCE` | Optional override of the inheritance setting (`0` or `1`) |
| `METAGUARD_RETRY_POLICY` | `failure-backoff` by default; `bounded-deferral` only with the experimental and MetaGuard opt-ins above |
| `CRISISGUARD_EXPERIMENTAL_POLICIES` | Must be `1` to use `fifo` or to change inheritance through `REPLAY_INHERITANCE` |
| `CLOUD_URL`, `EMERGENCY_URL`, `ADMIN_URL` | Cloud endpoint routing |
| MetaGuard hospital/controller credentials | Protect controller and Cloud replay contracts; configure privately, not in tracked documentation |

The normal Docker Compose configuration sets `AUTO_REPLAY=1` by default, unless overridden for an isolated test. Different startup modes and earlier local scripts may use different defaults. Verify the **effective Compose configuration**, not only an example value.

Never commit private `.env` files or populated authentication tokens.

## Verification evidence

The current-main checks reported on **31 October 2026** include:

- **24 passing** focused retry-state and scheduled-replay tests after adding bounded equal jitter.
- **102 passing** focused edge reliability/security tests, including claim safety, protected replay and manual-replay security.
- **967 passing** Python unit/mocked-API regressions for the broader project.
- Historical isolated eight-service Checkpoint 7D evidence: both hospitals captured pending events, retained them through restarts and later reached authoritative Cloud application. Dependency ordering was observed in PostgreSQL.

These are scopes of separate, partly overlapping test runs; their counts must **not** be added together. See `docs/evidence/CP7D_FINAL_VERIFICATION_20261031.md` and the root testing guide for dated results and limitations. Final merged-code verification on commit `459cb69` (31 October 2026) passed 59 scheduling and protected-replay tests and 84 PostgreSQL controller tests. An isolated eight-service Docker run showed both hospitals keeping pending events across a restart and applying them to PostgreSQL through MetaGuard-protected recovery. See `docs/evidence/CP8_MERGED_INTEGRATION_20261031.md`.

For focused local verification from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_recovery_state.py tests/unit/test_metaguard_replay.py tests/unit/test_scheduled_replay.py tests/unit/test_metaguard_reliability_security.py tests/integration/test_manual_replay_security.py
```

## Known limits

- The recovery guarantee depends on durable storage, valid configuration, protected grants and the documented dependency/fairness assumptions.
- A missing, stale or invalid Cloud/Controller signal can defer replay. This is a safety outcome, not evidence of successful drain.
- Local tests do not establish correctness for every physical network partition or AWS fault mode.
- The evaluation branch's experimental scheduling policies require their own review and final-merge tests.
- This prototype uses synthetic data and is not suitable for real patient care.

**Owner sign-off:** Afiq's A4 handoff is complete only when this guide has been checked against the final merged source and accepted by the team.
