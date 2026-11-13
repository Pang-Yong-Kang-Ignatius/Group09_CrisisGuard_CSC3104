# AIMD recovery diagnosis, 1 November 2026

## Finding

The reduced study's slower AIMD recovery is primarily a **configuration/proxy
problem with intentional hysteresis**, not a demonstrated error in AIMD arithmetic.
No production algorithm, default, authentication, telemetry definition, grant
fencing or AWS configuration was changed. Original H1/H2 results remain unchanged.

The recorded experiment uses AIMD min/max 2/20 replay RPS, additive step 2 after
two healthy observations, 1 s requested polling, 3 s dwell and 5 s freshness. Fixed
comparison rates are 20 RPS in every state. Both retain state-dependent in-flight
caps 4/2/1. These differ from the production defaults; they are not optimized values.

The throughput signal is **all completed application requests per service**, not
backlog throughput, CPU load or authenticated-live-only throughput. Its untouched
healthy/Caution/Recovery thresholds are 10/20/50 RPS. The workload sends all live
fixture reads to Patient. Thus 36 RPS exceeds Caution, 84 exceeds Recovery, and
neither meets the healthy criterion even with mean Cloud latency around 6-7 ms
and no server errors. Repeated unhealthy observations halve AIMD to 2 RPS; the
unhealthy gate prevents additive recovery. Constant admission stays 20 RPS.

All ten selected AIMD trials spend **100% of sampled fresh-observation time at
the 2 RPS floor** and 100% of observed recovery time in Recovery. This is sampled
interval coverage, not proof about unobserved moments or every controller poll.
Startup deliberately removes Cloud and yields missing telemetry: new grants
are denied. After fresh observations return, 30% load is only Caution by throughput,
but an existing Recovery state requires consecutive fully healthy observations
and dwell to leave it. That gate is never satisfied, so Recovery and its one-slot
cap persist. At 70%, throughput continues to demand Recovery directly.

Storage rejects stale/nonadvancing observations and old poll versions. Issue and
ACK phases have separate durable pacing; their counters are not unique applied
event throughput. Grant waiting and retry/backoff add delay, so the nominal 10:1
rate difference is not expected to equal the measured 3.8:1 drain ratio. Retry
journal totals are retained per trial in the diagnosis; no retry algorithm was
changed. Regression tests confirm that genuine healthy windows restore Normal
stepwise and that stale telemetry/database/latency/error signals remain unsafe.
Complete transactional safety remains covered by the existing PostgreSQL tests;
this investigation did not provision or mutate a live integration database.

## SLO exposure artifact

The original whole-run fractions are mathematically correct but do not isolate
steady-state protection: each run stops live traffic when its own backlog drains.
Similar startup failures are divided by far more requests in the slower AIMD run.

| Read RPS | Constant attempts / violations | AIMD attempts / violations |
|---|---:|---:|
|36 |5,635 /220 |20,868 /223 |
|84 |12,853 /514 |46,874 /510 |

All 1,467 failed-or-late outcomes precede **recorded all-Cloud readiness**. Across
71,383 post-readiness attempted requests, both modes have zero observed violations.
The diagnosis additionally compares equal-length post-ready windows within each
seed pair (10.19-24.08 s common exposure); both remain zero. These are retrospective
phase checks, not replacements for the original primary SLO metric. Readiness is
a sampled Compose completion boundary, not a precise cause label for each failure.
Do not conclude that all pre-ready failures were caused solely by the outage, or
that zero observed post-ready failures establishes an SLO guarantee.

Consequently, neither the -2.95/-3.01 pp fractions nor the current sample show that
throughput-triggered throttling protected live SLO better at steady state. At the
same time, this does **not** prove that removing the guard is safe under overload.
Client connection/queue latency and Cloud middleware latency are different signals.

## Smallest justified improvement

Calibrate the throughput guard separately before choosing new thresholds. Sustained
throughput alone can be healthy demand; the original B3/D1-D3 proposal explicitly
calls for separate tuning/freeze of thresholds and limits. Raising alpha cannot
help while healthy votes are suppressed. Raising the floor, bypassing freshness
or disabling the guard would hide rather than resolve the trade-off and is not
justified by this data. There is no evidence-based numeric replacement profile yet.

The only execution change is **experiment-only**:
`run_recovery_matrix --admission-throughput-thresholds HEALTHY CAUTION RECOVERY`.
It validates finite positive ordered thresholds, records them in plan/effective
configuration, and forbids reuse of old trials when they change. Omit it to keep
all old behaviour/defaults. Authentication and every non-throughput admission
setting remain unchanged. No candidate was selected against seeds 3104-3108.
Do not promote a candidate to production until independent overload calibration
shows it preserves protection; dashboard telemetry is unchanged.

## Reproduction and minimal follow-up (approval before Docker runs)

Read-only diagnosis, no services required:

```powershell
.\.venv\Scripts\python.exe -m docs.evidence.analyze_h1_h2_admission_20261101 --output evaluation/results/NEW-admission-diagnosis
```

Machine-readable [diagnosis](../evidence/cp8-aimd-diagnosis-20261101/diagnosis.json)
contains all 20 trials, raw hashes, retries and common-window counts; the
[sampled admission trace](../evidence/cp8-aimd-diagnosis-20261101/admission-trace.csv)
records rate/state/freshness, all three services' metrics and recomputed decisions.
The script re-verifies the selected original assessment before diagnosis. It never
rewrites source evidence; output must be a new directory.

1. Separately calibrate on seeds 50101/50102, before freezing any new profile. A
   bounded initial screen at 60/120 read RPS with 176 events and constant admission
   gives four fresh disposable trials, roughly 8-12 minutes including setup/tests
   (a 300 s timeout can extend this). They collect controller metrics and original
   latency/retry outcomes at non-evaluation loads; do not count them as holdout
   evaluation. If they do not include a genuine overloaded condition or enough
   stable exposure, no new throughput profile is established: stop rather than
   extrapolate thresholds. Retained read-only calibration files can inform the
   range, but 30 s passing steps and 60 s 140 RPS failure are not mixed-workload capacity.
2. Review average latency, errors, freshness and client p95/failures by phase.
   If throughput cannot distinguish healthy and overloaded samples, document
   that proxy limitation rather than selecting a convenient high cutoff. Only
   then freeze one justified profile and its calibration hashes before holdout.
3. Independently evaluate that frozen profile versus constant using seeds 60101-
   60105 at 36/84 RPS, common 88-event graphs and unchanged timeouts/settings. This
   is 20 new trials, roughly 40-75 minutes, not the 490-trial matrix. Preserve the
   primary full-run metric and add fixed/common post-ready exposure; report drain,
   urgent/routine p95, client SLO, retries and rate/state traces. Five-pair exact
   two-sided p cannot be below .0625. No urgent-versus-RR claim follows from this
   admission-only comparison.

Plan-only preparation for the four calibration trials (no Docker execution):

```powershell
$py = '.\.venv\Scripts\python.exe'
$root = 'evaluation/results/aimd-independent-calibration-' + (Get-Date -Format yyyyMMdd-HHmmss)
$common = @('--mode','pilot','--pilot-policy','bounded_on','--pilot-seeds','50101,50102',
 '--total-events','176','--activation','automatic','--live-read-target','preseed',
 '--live-workers','128','--live-slo-ms','250','--admission-ceiling','20',
 '--timeout','300','--workers','1','--admission-mode','constant')
foreach ($rate in @(60,120)) {
 & $py -m evaluation.experiments.run_recovery_matrix @common --live-read-rps $rate --plan-only --output "$root/plan-$rate"
 if ($LASTEXITCODE -ne 0) { throw 'Calibration plan failed' }
}
```

After explicit approval, use the SAME arguments without `--plan-only` and with
new `run-60` / `run-120` output directories. Do not run concurrently. Stop on failure.
No Docker experiments were launched during this investigation. No new recovery
performance or D3 acceptance achievement is claimed.

## Independent calibration review: 60/120 read RPS

**Decision: further calibration required; no threshold profile is justified.**
The subsequently completed runs are under
`evaluation/results/aimd-independent-calibration-20261101-185222/run-60` and
`run-120`. Both batches passed preservation, source stability and cleanup.
All four trials applied all 176 original events, retained complete observation
intervals, passed functional/integrity checks and left no controller tickets.
Raw trial JSON agrees with each summary's reads, samples and completion intervals.
They used constant admission, independent seeds 50101/50102 and the unchanged
throughput thresholds, not a newly tuned profile.

| Offered RPS | Seed | Issued / scheduled | Lag / cap skips | Post-ready exposure | Post-ready reads | Client p95 |
|---|---:|---:|---:|---:|---:|---:|
| 60 | 50101 | 3,480 / 3,480 | 0 / 0 | 45.64 s | 2,739 | 16 ms |
| 60 | 50102 | 2,412 / 2,412 | 0 / 0 | 27.80 s | 1,669 | 16 ms |
| 120 | 50101 | 6,572 / 6,572 | 0 / 0 | 42.36 s | 5,085 | 16 ms |
| 120 | 50102 | 5,160 / 5,162 | 2 / 0 | 30.59 s | 3,671 | 16 ms |

Every ledger reconciles. The two genuine lag skips give 99.9613% issuance in the
affected trial: valid under primary 99.9%, invalid under 99.99% and strict-zero.
The other three trials satisfy all three rules. No misses or failures were erased.

Post-ready client outcomes were **zero transport failures, zero HTTP failures and
zero >250 ms violations in all 13,164 attempted requests**. Startup had 250
transport failures and 197 late successes, with client p95 approximately 516-687 ms.
All 447 violation completions occurred before recorded all-Cloud readiness.
These are phase-specific observations, not proof of the cause of each startup
failure or of indefinite sustainable capacity. Per-trial drain upper endpoints
were 57.985/40.125 s at 60 RPS and 54.719/42.984 s at 120 RPS; unequal run lengths
must not be treated as a controlled rate-benefit comparison.

After readiness, Patient's rolling aggregate throughput was **62.96-65.55 RPS**
at the 60 RPS offer and **123.31-128.71 RPS** at the 120 RPS offer, including replay
requests. Its rolling mean latency was **6.08-6.89 ms / 7.23-10.79 ms**; general
p95 was **7.93-8.65 ms / 8.70-54.60 ms**. Sampled Cloud error rates were zero and
executor queue depths zero across all services. Emergency/Admin primarily served
replay; their missing live p95/SLO samples are not fabricated zero observations.
Rolling Cloud metrics include earlier requests and are not phase-pure windows.

All **536 post-readiness controller snapshots were fresh**, in Recovery at the
configured constant 20 RPS and one-slot cap. Recomputed severity was Recovery
solely from `patient:throughput_rps` in every snapshot. The current >50 RPS trigger
therefore misclassifies both observed healthy post-ready loads. No independent
sustained overloaded phase, persistent queue pressure, stale post-ready telemetry
or measured post-ready SLO degradation was observed. Snapshot counts are not
independent replicas; there are only two seeds per load. Post-ready exposures
are all shorter than the 60-second metric window.

This establishes **false positives in the current thresholds**, not an intrinsic
proof that throughput is unusable at every capacity or an estimate of an overload
boundary. Both calibration classes are healthy after readiness; sensitivity to
genuine overload cannot be measured. Do not invent higher cutoffs from the largest
healthy sample, remove the guard, change the floor, or claim that a new AIMD
profile has been validated.

**Smallest next action, only after approval:** a bounded readiness-first calibration
session with equal, fixed measurement windows: retain a 120 RPS healthy baseline
and test one higher offered load, rather than repeating these four backlogs.
For example, 140 RPS is a *probe load*, not a recommended threshold. Prespecify
the overload label from client latency/failures and independent Cloud pressure,
not throughput itself. Allow a complete rolling-window warm-up, then at least
60 seconds measurement per step, with a fixed safety/time ceiling and early stop
on unhealthy behaviour (roughly 4-6 minutes plus readiness). If that step is still
healthy or generator-limited, report no overloaded class; no profile follows.
Do not spend the 20 independent holdout trials until a profile is justified and
frozen. Mixed-workload capacity remains outside this read-only calibration.

The [machine-readable review](../evidence/aimd-independent-calibration-review-20261101/assessment.json)
and [phase data](../evidence/aimd-independent-calibration-review-20261101/phases.csv)
retain generator accuracy, service-specific metric ranges, state/freshness counts
and source hashes. Reproduce read-only into a new directory:

```powershell
.\.venv311\Scripts\python.exe -m docs.evidence.analyze_aimd_calibration_20261101 evaluation/results/aimd-independent-calibration-20261101-185222 --output evaluation/results/NEW-independent-calibration-review
```

No new Docker experiments, production-default changes or historical H1/H2 result
edits were performed for this review. Original primary outcome definitions remain.

## Subsequent experimental health policy

The completed readiness-first session 70101 also found only healthy classes at
120/140 RPS; no replacement throughput cutoffs are justified. Its two 60-second
measurement windows issued 7,199/7,200 and 8,398/8,400 requests, with zero measured
client failures/SLO violations and approximately 16 ms client p95. Genuine skips
remain recorded and both windows fail the stricter 99.99%/zero-skip rules.

An explicitly gated `pressure-experimental` policy now tests fresh measured tails,
SLO fractions, errors, executor queue pressure and DB health. Legacy throughput
policy, production defaults, fencing, freshness and AIMD/state-transition mechanics
remain. This addresses a demonstrated proxy false positive; sensitivity to genuine
overload and recovery performance remain unvalidated. No new load experiment was
run and no original outcome changed. See the [decision rule](../CONFIGURATION.md#monitoring-only-signals),
the retained raw pilot assessments, and this report's limitations. The original
pilot commands remain available in Git history.

## Independent bounded-deferral pilot: seed 60202

This is **subsequent experimental evidence**, not a revision of the original
20-trial negative results. Source:
`evaluation/results/aimd-deferral-pilot-20261101-202315/run-{failure-backoff,bounded-deferral}/summary.json`.
Both runs use AIMD, `pressure-experimental`, the same 88-event balanced graph and
84 RPS existing-record reads. Workload hashes, all 43 source hashes and all
non-retry/non-port configuration fields match. Both summaries equal their raw
archives; correctness, container/network/volume preservation, source stability,
SQLite integrity and cleanup passed. Each completed all 88 original events.

| Metric | Failure backoff | Bounded deferral | Change |
|---|---:|---:|---:|
| Drain upper endpoint | 63.343 s | 33.828 s | **−46.60%** |
| Hospital A urgent workflow p95 | 63.343 s | 28.093 s | −55.65% |
| Hospital B urgent workflow p95 | 53.562 s | 26.515 s | −50.50% |
| Hospital A routine workflow p95 | 34.828 s | 30.265 s | −13.10% |
| Hospital B routine workflow p95 | 24.000 s | 33.828 s | **+40.95%** |
| Full-run end-to-end SLO violations | 99/5,325 = 1.859% | 102/2,845 = 3.585% | **+1.726 pp** |
| First 10 s after recorded readiness | 0/840 | 0/840 | 0 pp |

Workflow p95 uses the existing nearest-rank observer upper endpoints, with only
5–6 workflows per site/population. Drain observation intervals are
[63.062,63.343] and [33.546,33.828] s: the resulting relative measurement bounds
are −47.04% to −46.36%, **not statistical confidence intervals**. There is only
one independent paired seed; uncontrolled retry jitter and service startup
variation preclude a significance claim or a general 46.6% improvement estimate.

Both generators reconcile exactly, with zero cap/lag skips and 100% issuance:
5,325/5,325 and 2,845/2,845. Both pass 99.9%, 99.99% and strict-zero sensitivity.
All-Cloud readiness was 12.375/12.359 s. All 189/81 post-readiness controller
snapshots were fresh, and all 4,286/1,808 post-readiness reads had zero measured
violations; successful client p95 was approximately 16 ms. All violations
completed within the first 1.468 s, before readiness. Server metrics sampled
after freshness showed zero errors/queue pressure and live latency SLO violations.

The full-run fraction **worsened**, principally because faster drain shortened
the denominator while startup violations changed from 99 to 102. Its +1.726 pp
exceeds the proposal's +1 pp guardrail numerically for this retry comparator.
Do not replace that outcome with the fixed-window zero to claim acceptance:
the prescribed admission comparator is constant-rate, which is absent here.
The fixed windows show no observed post-ready worsening, not proven protection
under genuine overload. The 120 RPS reference remains provisional and read-only.

Both arms left the 2 RPS floor and reached Normal at 6 RPS, then 20 RPS at
28.062/28.921 s. Thus the faster arm did not use a faster AIMD ramp. At the ceiling,
16/12 events remained; the tails were 35.281/4.907 s. The experimental journal
records **21 fresh rate-limit deferrals** (11 A, 10 B), each with a one-second
deadline; these do not increase failure counts. Remaining failure-backoff counters
were **90 versus 64** (A/B: 45/45 versus 28/36; maximum event counts 5 versus 3).
These counters still include unclassified no-grant/fairness waits and ambiguous
outcomes, so **genuine failure retries cannot be counted separately** from the
retained data. No recorded replay-loop errors is not proof of zero event failures.

Last sampled allowed/denied counters were 177/89 versus 187/69; these include
issue and ACK decisions and are not guaranteed final totals. Bounded retry had
99 allocations, 88 completed grants and 11 fenced superseded grants, versus
89/88/1 for failure backoff. Increased candidate churn is real; it produced no
observed duplicate application. Sampled ticket-bound violations were zero in
both arms, but a complete fairness/failure study remains outstanding.

**Supported:** a large descriptive drain/urgent-p95 improvement for this matched
retry pilot and successful handling of 21 explicit admission deferrals, with no
observed fixed-window SLO worsening. **Not supported:** uniform routine improvement,
genuine-overload protection, statistical significance or D3 acceptance.

Before any admission acceptance claim, a final-policy **matched constant-rate**
arm is necessary: same seed 60202, graph, 84 RPS, source revision, health policy,
bounded retry and measurement protocol. It can complete this pilot comparison
only if compatibility is verified; the seed-60201 constant arm cannot substitute.
One extra arm alone would not establish proposal acceptance. Freeze the final
policy before independent paired holdout at both 36/84 RPS (reserved seeds
60101–60105), with unchanged full-run SLO/drain/routine targets and fixed-window
diagnostics. Urgent ≥10% improvement still needs RR+inheritance in the specified
skewed dependency case; full D2 coverage remains unexecuted. No further experiment
was run for this review and no production default changed.

[Verified assessment and source hashes](../evidence/aimd-deferral-pilot-review-20261101.json).
Reproduce without contacting services, choosing a new output path:

```powershell
.\.venv311\Scripts\python.exe -m docs.evidence.analyze_aimd_deferral_pilot_20261101 evaluation/results/aimd-deferral-pilot-20261101-202315 --output evaluation/results/NEW-deferral-review.json
```

Review checks: 68 focused acceptance/retry tests passed; raw/hash/completeness and
recomputed-assessment checks passed. The previous pilot hashes and original
reduced-study assessment/source selection also verified unchanged. Analysis
compilation and `git diff --check` passed. No service or load session was started.

## CI unit-test isolation correction

The three service classification tests entered real FastAPI lifespans and called
`init_db()`; only event retrieval was mocked. The GitHub Actions unit job provides
no PostgreSQL. A running local PostgreSQL concealed the dependency. The tests now
mock each module's imported initializer and event read, and fail immediately if
any real `psycopg.connect` is attempted. The actual HTTP route, lifespan execution,
middleware, role-specific headers/token validation, 100 unmarked 200 responses,
live/replay requests and wrong-token assertions remain. Initialization and all 103
event reads are asserted. Production code and CI/AWS configuration are unchanged.
The pre-fix failure was reproduced for all three services with a refused DB stub;
the fixed cases pass under Python 3.11.9.

Verification: all 1,100 unit tests passed locally on Python 3.11.9 (one dependency
deprecation warning); 197 focused admission/controller/recovery tests and all
86 dashboard JavaScript tests passed. Both independent calibration plans generated
two trials without Docker. `git diff --check` passed. Hosted CI was not rerun.
