# H5 final-policy holdout assessment — 2 November 2026

H5's outstanding matched-comparison assessment is complete: **40 new functional trials,
40 generator/functional-valid, 0 strict accepted, 3520 original events**.
This completes an assessment task, not universal performance or proposal acceptance.
No historical scheduling, cost-stress, reduced-admission, calibration or diagnosis
study was rerun or overwritten. No production/AWS/default/telemetry change occurred.

## Subsequent W5 evidence review, 3 November 2026

[H5 review after W5](H5_W5_REVIEW_20261103.md) adds Wayne's separately acquired
admission evidence and replaces the pending W5-execution dependency below. His
selected study fails drain and routine guardrails; it does not clear this H5
run's preservation failure. The measured H5 results and frozen specification
below retain their original acquisition scope.

## Frozen methods and source

[Specification](H5_EXPERIMENT_SPEC_20261102.md) and [configuration/source freeze](../evidence/h5-configuration-freeze-20261102.json)
predate all holdout outcomes. The separate checkout is based on `a631e6ea8ab80d549e09b7b80fcf16af816d09e6`
plus the frozen evaluation-harness changes. All 44 source hashes stayed
unchanged during execution; all 29 application files match the built image
`sha256:f24503de1d32422f7ef768b4dcc394fc0abf134cfc039e9b73fee16e2f9b3575`. [Execution verification](../evidence/h5-execution-verification-20261102.json)
records source, functional checks, runtime and preservation.

Five reserved seeds 60101–60105 were acquired sequentially at each of 36/84 RPS,
with predetermined counterbalanced arm order and randomized blocks. The two rates
are 30%/70% of the **provisional 120-RPS read-only reference**, not measured maximum
sustainable mixed capacity. Four question/load cells are not full D2 coverage.
Each pair shares its 88-event graph, image, resources, automatic-outage/readiness
procedure, 250 ms SLO, 128 live workers, 300 s timeout and .25 s observer setting.
Both modes use `pressure-experimental` and `bounded-deferral`; production defaults
remain unchanged. Admission compares AIMD 2–20 RPS with constant 20 RPS under the
same caps/health/retry. Scheduler compares bounded B=1 with RR+inheritance under
identical AIMD and retry. Differences are not attributed to retry changes or to
an isolated production-controller improvement.

## Primary acceptance assessment

**The original overall run is FAILED, and strict acceptance is NOT ESTABLISHED.**
Its exact-network-set guard failed: baseline network ID `f2f3dea13f38` disappeared
and a new built-in `bridge` ID `1ae77d1a7cd7` was observed; the other four network
IDs remained. The original 17 container identities/images/mounts/states and all
volumes were unchanged, with no state changes. The cause was not measured.
Original source and all 40 per-trial integrity/cleanup checks passed. This does
not authorize silently waiving the prespecified network check.

The [strict primary assessment](../evidence/h5-holdout-strict-assessment-20261102.json)
retains zero accepted pairs and unestablished targets. Numerical effects below
are an explicitly **post-hoc resource-guard sensitivity**, disregarding only the
network-ID failure to describe the measured outcomes. Their diagnostic threshold
labels are not primary acceptance. Owner/infrastructure review must resolve the
network deviation before any acceptance claim; no favorable sensitivity replaces
the original failure, and no trials were repeated to clear the guard.

Diagnostic effects are means of per-seed relative changes, or percentage-point differences
for whole-run SLO. Intervals below are paired percentile-bootstrap intervals, not
observer intervals. The JSON/CSV retains every seed, observer bounds, exclusions,
timeouts/incompletes and zero-skip/99.99% sensitivity. Five pairs cannot yield a
two-sided exact sign-flip p below .0625; no significance claim is made. Achieved
means the recorded scoped point-estimate guardrail, not full D2/D3 acceptance.

| Comparison / load | Metric / population | Effect | 95% paired interval | Pairs | Scoped decision |
|---|---|---|---|---:|---|
| Admission, balanced / 36 RPS | Routine p95 proxy, Hospital_A | +36.41 percent | [+9.05, +56.84] | 5 | **not established**; diagnostic threshold failed |
| Admission, balanced / 36 RPS | Original drain, global | +34.04 percent | [+9.31, +63.44] | 5 | **not established**; diagnostic threshold failed |
| Admission, balanced / 36 RPS | Whole-run SLO, global | -1.58 percentage points | [-2.87, -0.39] | 5 | **not established**; diagnostic threshold achieved |
| Admission, balanced / 36 RPS | Routine p95 proxy, Hospital_B | +45.73 percent | [+10.92, +81.37] | 5 | **not established**; diagnostic threshold failed |
| Admission, balanced / 84 RPS | Routine p95 proxy, Hospital_A | +22.01 percent | [+7.35, +41.99] | 5 | **not established**; diagnostic threshold failed |
| Admission, balanced / 84 RPS | Original drain, global | +14.77 percent | [+4.22, +26.01] | 5 | **not established**; diagnostic threshold achieved |
| Admission, balanced / 84 RPS | Whole-run SLO, global | -0.91 percentage points | [-1.44, -0.38] | 5 | **not established**; diagnostic threshold achieved |
| Admission, balanced / 84 RPS | Routine p95 proxy, Hospital_B | +23.61 percent | [-0.48, +54.46] | 5 | **not established**; diagnostic threshold failed |
| Scheduler, skewed A-only / 36 RPS | Urgent p95, Hospital_A | +0.38 percent | [-5.63, +7.74] | 5 | **not established**; diagnostic threshold failed |
| Scheduler, skewed A-only / 36 RPS | Routine p95 proxy, Hospital_A | -1.51 percent | [-3.57, +1.38] | 5 | **not established**; diagnostic threshold achieved |
| Scheduler, skewed A-only / 36 RPS | Original drain, global | -0.74 percent | [-2.45, +1.72] | 5 | **not established**; diagnostic threshold achieved |
| Scheduler, skewed A-only / 36 RPS | Whole-run SLO, global | +0.24 percentage points | [-0.21, +0.87] | 5 | **not established**; diagnostic threshold achieved |
| Scheduler, skewed A-only / 36 RPS | Routine p95 proxy, Hospital_B | +15.64 percent | [+3.36, +29.66] | 5 | **not established**; diagnostic threshold achieved |
| Scheduler, skewed A-only / 84 RPS | Urgent p95, Hospital_A | +3.32 percent | [-15.39, +27.47] | 5 | **not established**; diagnostic threshold failed |
| Scheduler, skewed A-only / 84 RPS | Routine p95 proxy, Hospital_A | -6.90 percent | [-20.69, +9.79] | 5 | **not established**; diagnostic threshold achieved |
| Scheduler, skewed A-only / 84 RPS | Original drain, global | -13.08 percent | [-31.90, +4.45] | 5 | **not established**; diagnostic threshold achieved |
| Scheduler, skewed A-only / 84 RPS | Whole-run SLO, global | +0.71 percentage points | [+0.16, +1.60] | 5 | **not established**; diagnostic threshold achieved |
| Scheduler, skewed A-only / 84 RPS | Routine p95 proxy, Hospital_B | -22.57 percent | [-61.04, +24.81] | 5 | **not established**; diagnostic threshold achieved |

Both hospitals' routine outcomes are assessed. A-only urgency has no B urgent
population; it remains undefined rather than being treated as zero. Routine p95
is the existing workflow-completion waiting proxy, not exact queue-only waiting.
The urgent target applies to A in the prespecified skewed scheduler comparison;
balanced admission urgent observations are descriptive and retained in the data.

## Whole-run SLO and equal-duration diagnostic

The original primary metric counts non-200 responses, transport failures or client
latency >250 ms once per issued request, over each original whole live-run window.
Generator misses stay separate. It is not replaced by a favorable steady window.
The secondary window was fixed before outcomes: request starts in the first ten
seconds after recorded all-Cloud readiness, incomplete if exposure is too short.

| Comparison / load | Complete paired windows | Secondary SLO difference |
|---|---:|---:|
| Admission, balanced / 36 RPS | 5 | +0.00 pp |
| Admission, balanced / 84 RPS | 5 | +0.00 pp |
| Scheduler, skewed A-only / 36 RPS | 5 | +0.00 pp |
| Scheduler, skewed A-only / 84 RPS | 5 | +0.00 pp |

Unequal original drain/run durations change whole-run denominators. These diagnostics
describe that boundary; neither low server latency nor zero post-ready violations
proves that the candidate protects live traffic under genuine overload.

## Correctness, fairness and remaining claims

- Recorded event-set/content, dependency/grant order, completed grants, local ACK
  state, SQLite integrity, disabled manual replay and repeat-grant fencing checks
  are preserved per trial. These are observed functional checks, not a universal
  duplicate-domain-effect proof. See each outcome in the assessment.
- 689 sampled conditional ticket witnesses recorded 0 violations.
  Full eligibility-interval coverage, indefinite fairness and CPU/resource-share
  guarantees remain unestablished. Per-site shares/retries and sampled project
  CPU/memory are retained; project resources are not isolated scheduler costs.
- The independent bounded 160-RPS probe is recorded in
  [pressure evidence](../evidence/h5-pressure-probe-20261102.json). Its decision was
  `stopped: unsafe or generator-invalid; no profile justified`. It supplies no calibrated genuine-overload protection,
  representative mixed capacity, revised threshold or production approval.
- Existing B5 evidence remains unchanged: concentrated-A screening urgent −29.49%
  accompanied B routine +105.33%; the balanced skew case was mixed. Original
  H1/H2 drain/routine failures and seed60202's +1.726 pp primary SLO result remain.
  Seed60202's historical raw files were unavailable here, so no new arm is called
  a matched completion of that old pilot. New holdouts are separate evidence.
- Full 16-condition/490-trial D2 acceptance, exact routine queue waiting, sustained
  fairness, isolated scheduler cost and complete failure/eligibility witnesses
  remain unestablished. Historical TX link verification is separate functional
  evidence and was not repeated as an H5 performance experiment.

## Review material and task status

**H5: assessment complete.** Failed and unestablished targets remain visible.
**Wayne W5:** execution/validation and H5's subsequent compact-evidence review
are complete; see the [3 November reconciliation](H5_W5_REVIEW_20261103.md).
Review of H5's own diagnostic effects, experimental pressure thresholds,
freshness/caps and genuine-overload protection approval remains separate.
**Bettina:** review both-site scheduler trade-offs, urgency population and observer
definitions; B1–B4 approval at 58f987b/B5 screening does not approve new pressure
or retry performance claims. **Afiq A5:** review the remaining whole failure/
eligibility coverage and duplicate-domain-effect mapping. The 71 PostgreSQL cases
include transactional hint/fairness/staleness tests and a two-event deferral/restart/
dependency/exact-application scenario, but are not the complete proposed fault matrix.
TX/infrastructure and component owners must also review the failed network
continuity guard; accepting a descriptive sensitivity is not assumed.
No teammate message was sent or owner approval assumed. W5/A5 remain independent.

## Evidence and checks

- [Assessment JSON](../evidence/h5-holdout-assessment-20261102.json),
  [per-seed effects](../evidence/h5-paired-effects-20261102.csv),
  [per-site absolute measurements](../evidence/h5-measurements-20261102.csv),
  [comparison table](../evidence/h5-comparisons-20261102.csv).
- **1,193 unit tests** and **71 real PostgreSQL cases** passed. One existing
  dependency deprecation warning remains. The initial focused invocation's
  missing temporary-directory setup failure was retained; creating its parent
  fixed the setup before the complete successful suite. No application fix was needed.
- Original container/image/mount/state and volume checks passed; the exact-network
  identity guard failed and remains failed. Private resolved credentials/logs are
  excluded from the evidence archive.
- [Verified separate raw/source backup](../evidence/h5-backup-20261102.json).
  Local retention is not off-machine recovery or independent teammate reproduction.
  No commit, push or merge was performed. Final hosted CI and owner review remain.
