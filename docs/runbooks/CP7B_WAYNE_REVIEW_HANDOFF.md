# Checkpoint 7B MetaGuard review handoff

**For:** Wayne  
**Purpose:** Review verified Checkpoint 7B controller behavior, evidence, and remaining decisions. This document does not mark the review complete.

## Verified implementation and safety

- AIMD is opt-in and durable; fixed-rate remains the default policy. Admission state/rate persistence uses two additive controller metadata columns.
- The controller has Normal, Caution, and Recovery states with adaptive pacing. Stale telemetry fails closed for admission. Grant allocation/reconciliation, restart persistence, and bounded bypass were verified in isolated and original-stack checks.
- The original desktop deployment was activated and verified without recreating its eight existing containers. Hospital A/B replay delivered the existing eight-event recovery backlog. Patient telemetry recovery, controller rate recovery, grant enforcement, direct-Cloud rejection, and historical database preservation are recorded in the main-stack evidence.
- Checkpoint 7B regression evidence records 891 cases plus 17 PostgreSQL reliability cases (908 total), all passing. The later main-stack verification records 814 unit cases passing.

**Evidence:** [main-stack report data](../evidence/cp7b-main-stack-final-20261028.json), [main-stack JUnit](../evidence/cp7b-main-stack-unit-final-20261028.xml), [7B regression JUnit](../evidence/cp7b-regressions-20261027.xml), [PostgreSQL reliability JUnit](../evidence/cp7b-reliability-20261027.xml), [live AIMD results](../evidence/cp7b-aimd-live-20261027.json).

## Policy comparison and fairness findings

- Five isolated fresh-volume trials per policy compared fixed-rate and AIMD under the same eight-event backlog and Cloud recovery conditions. Mean recovery was 39.573 s fixed-rate and 39.898 s AIMD. The +0.325 s AIMD difference was not significant (95% bootstrap interval -10.130 to +10.776 s; exact permutation p=0.579). Neither policy showed a measured advantage in the tested latency, throughput, retry, error, or completion measures.
- AIMD restored the Normal grant rate through gradual 0.2 RPS increases; fixed-rate returned directly to its Normal rate sooner. Both completed the same events exactly once, with eight completed grants and no Cloud 5xx responses per trial.
- Three 660-second arrival trials per policy tested sustained Hospital A/B recovery with priority reversals, burst and steady arrivals, and dependencies. Both sites progressed in all 132 hospital-minute intervals; all 3,600 events were preserved and applied with dependency order and idempotency intact. No trial left backlog. Maximum observed event wait was 581.383 s. This is finite single-host evidence, not an indefinite fairness or per-event deadline guarantee.

**Evidence:** [fixed-rate versus AIMD evaluation](../archive/CP7B_FIXED_VS_AIMD_EVALUATION_20261029.md), [comparison raw trials](../evidence/cp7b-fixed-vs-aimd-raw-final-20261029.json), [comparison statistics](../evidence/cp7b-fixed-vs-aimd-statistics-20261029.json), [sustained fairness evaluation](../archive/CP7B_SUSTAINED_FAIRNESS_20261029.md), [fairness raw trials](../evidence/cp7b-sustained-fairness-raw-20261029.json).

## Remaining risks and open decisions

- **Telemetry signals remain observe-only.** The bounded representative study found live-only p95 useful for in-handler and database delay, and executor pressure useful for actual shared-worker contention. A 300 ms pre-Cloud delay raised client p95 to about 318 ms while middleware p95 stayed below 9 ms and executor pressure stayed zero. Metrics are process-local and sampled; metrics delivery was delayed up to 2.49 s in one bounded-concurrency trial. Server/socket queues and a separate database queue depth are not measured.
- **Live workload coverage is incomplete for calibration.** The study used authenticated live status reads plus real authenticated replay traffic. It did not calibrate authenticated live writes or production numeric thresholds. Existing controller mean-latency thresholds must not be reused as live-p95 thresholds. The current controller still consumes legacy telemetry fields.
- **Fairness evidence is finite.** It does not cover multi-hour operation, prolonged telemetry/network failure, indefinite postponement, or all site counts and workload patterns.
- **AWS adaptive state transitions remain unverified.** Local Docker and original desktop evidence do not establish AWS behavior.

**Evidence:** [representative telemetry report](../archive/CP7B_REPRESENTATIVE_TELEMETRY_CALIBRATION_20261029.md), [telemetry statistics](../evidence/cp7b-representative-telemetry-statistics-20261029.json), [latency figure](../evidence/figures/cp7b-representative-telemetry-latency-boundaries-20261029.png), [executor figure](../evidence/figures/cp7b-representative-telemetry-executor-waiting-20261029.png).

## Wayne's review checklist

- [ ] Confirm the AIMD state, pacing, persistence, stale-telemetry denial, grant and bypass evidence is sufficient for the component review.
- [ ] Decide whether the original-stack AIMD activation is acceptable to retain, given that the bounded fixed-rate comparison found no statistically meaningful performance winner and AIMD ramps more gradually.
- [ ] Confirm whether live-only p95 and executor pressure should remain observe-only pending authenticated live-write coverage, service objectives and threshold validation, or whether a separately scoped adoption proposal should be prepared.
- [ ] Review whether the finite Hospital A/B fairness evidence and its stated limits are sufficient for this checkpoint.
- [ ] Confirm AWS adaptive-state verification is required before Checkpoint 7B can be considered complete.
- [ ] Record any requested changes or conditions for approval.

The `Coordinate final component review with Wayne` item in `docs/TODO.md` remains unchecked until Wayne completes this review. AWS verification and telemetry adoption/calibration also remain open.

## Final Integrated Controller Review

The integrated controller implementation was reviewed after Haris's
experimental scheduling changes were merged into main.

The review covered:

- FIFO, priority, bounded priority and DRR selection
- Experimental-policy opt-in and unchanged production defaults
- PostgreSQL-persisted DRR state
- Transactional rollback of failed grant issuance
- Grant acknowledgement, expiry and reconciliation
- Grant fencing and uncertain-attempt protection
- Admission compatibility with experimental scheduling

Verification:

- Tests:
  `tests/unit/test_cp8_policies.py`
  `tests/integration/test_cp8_postgres.py`
  `tests/integration/test_controller_postgres.py`
- Result: 83 passed, 0 failed, 0 skipped
- Environment: Python 3.11, isolated PostgreSQL test database
- Reviewer: Ignatius, integration coordinator

No blocking controller defect was identified during this review.

The tests establish correctness for their exercised scenarios.
They do not establish universal fairness, optimal admission thresholds,
or performance superiority of AIMD or DRR.

The remaining calibrated performance evaluation is tracked separately
under Haris's evaluation tasks.