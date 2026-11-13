# H5 frozen experiment specification — 2 November 2026

Base: supplied ZIP/main `a631e6ea8ab80d549e09b7b80fcf16af816d09e6`.
ZIP SHA-256 `e9ecc86afa8879bd7c7d85dd57b6f38d8ceca63d189fab78c415648934d8498e`;
all 523 files match Git after CRLF normalization. Work is in a separate detached
checkout; the original checkout and historical evidence are retained.

## Frozen candidate and controls

- Health: explicitly opted-in `pressure-experimental`, unchanged decision rule.
- Candidate admission: AIMD floor/ceiling 2/20 RPS; additive step 2 after two
  healthy polls, poll 1 s, dwell 3 s, freshness 5 s, timeout 1 s. Existing pressure
  decision thresholds remain; no numeric threshold is selected from holdout.
- Constant comparator: 20 RPS in every state. Both modes retain stale denial,
  identical pressure-health classification and state-dependent in-flight caps
  4/2/1. Thus the contrast is the admission-rate policy under this experimental
  health/retry configuration, not a production-policy improvement.
- Both arms use explicitly opted-in `bounded-deferral` retry. Authenticated,
  definite fresh admission hints follow the existing bounded deadline; unknown,
  ambiguous, transport and real event failures retain the existing exponential
  equal-jitter failure backoff (50–100% of capped delay, cap 30 s).
- Scheduler candidate: bounded controller, B=1, priority local replay,
  inheritance ON, ageing 600 s. RR control: controller FIFO/B=0 with the same
  priority local replay, inheritance and ageing. Admission/retry are identical
  across scheduler arms. No new DRR or cost-calibration experiment is needed.
- Actual full admission settings, source fingerprints, review/report hashes,
  and all workloads/arm orders are recorded before execution in the freeze JSON
  and saved plan. One immutable image is built from these application sources.

## Missing comparisons only

| Question | Shape | Arms | Loads | Seeds | Trials |
|---|---|---|---|---|---|
| Final-policy admission | A:B 1:1, depth 3, balanced urgency | bounded+inheritance AIMD vs compatible constant | 36/84 RPS | 60101–60105 | 20 |
| Final-policy scheduler | A:B 10:1, depth 3, urgency at A | bounded+inheritance vs RR+inheritance, both AIMD | 36/84 RPS | 60101–60105 | 20 |

These seeds were reserved in the diagnosis; original 3104–3108, calibration,
60201/60202 diagnosis and 9191 pilots are not evaluation holdouts. Each paired
graph has 88 original events and 50% urgent workflows. Identical graph hashes and
arrival schedules are required within pairs. All case blocks and arm order are
predetermined: alternate first arm by seed/load (3:2 within cell, 5:5 across
loads), then shuffle blocks using the fixed H5 ordering salt. Sequential execution.
No outcome-based replacements, tuning, optional stopping or significance-driven
sample expansion. Failed/incomplete trials and generator sensitivity stay visible.

Resources, source image, readiness, retry and observation procedures match across
arms. Use isolated unique projects, loopback ports and fresh per-trial databases;
128 live workers, 250 ms SLO, 300 s drain timeout, .25 s observer pause, real
automatic outage/reconnection. Preserve existing Docker identities, images,
mounts, states, networks and volumes. Stop expansion on unsafe cleanup or integrity.
Run PostgreSQL policy/deferral regressions once against the first disposable DB.

## Measurement and decisions

Primary SLO remains the original whole-run end-to-end fraction: non-200 OR
transport failure OR client latency >250 ms, divided by issued requests. Generator
misses remain separate; exact ledger reconciliation and >=99.9% issuance are the
frozen primary validity rule, with 99.99% and zero-skip sensitivity. Report start
lags, actual offered/issued/failed populations, client and server measurement
boundaries; do not average rolling-window p95s into a whole-run percentile.

Secondary diagnostic is exactly the first **10 seconds after recorded all-Cloud
readiness**, based on request start time. If exposure is shorter, mark incomplete;
do not shorten the window or replace primary negative outcomes with this result.

For each seed report observer intervals, urgent final-required-event completion
p95 after reconnection, routine workflow-completion waiting proxy, original-ID
drain, full-run SLO, shares, retries/deferrals, sampled ticket witnesses and sampled
project CPU/memory. Routine waiting proxy and project resources do not establish
exact per-event routine waiting or isolated scheduler CPU/memory.

Mean per-seed relative changes, not ratios of pooled means: urgent <=−10% only
for the prescribed skewed scheduler case; routine <=+20% at **both** hospitals;
drain <=+20%; primary live SLO <=+1 percentage point. In A-only urgency B has no
urgent population; retain B routine results. Report paired bootstrap uncertainty,
observer uncertainty and exact sign-flip tests separately. Five pairs cannot
yield a two-sided exact p below .0625. Targets use measured point estimates,
not a promised significance result. Record achieved, failed and unestablished
separately. Correctness and fairness claims stay within their actual witnesses.

## Separate pressure probe and review dependencies

Pressure health has healthy-demand evidence, **not genuine-overload calibration**.
After holdouts, where feasible, run one new bounded 160-RPS readiness-first probe
in a separate fresh Cloud/controller project using the same image: fixed 60 s
warmup and 60 s measurement, existing calibrator safety/early-stop rules, seed
70301. This is an untested probe rate, not a tuned cutoff. Do not repeat the
completed 120/140-RPS sessions, force an overload label, disable safety, or select
a new candidate from this probe. If healthy, generator-invalid, stale or stopped,
overload protection remains unestablished. This read-only probe is not mixed-load
capacity or evidence about replay protection under genuine overload.

36/84 RPS are 30%/70% of the **provisional 120-RPS read-only reference**. Four
question/load cells are not full D2's 16 combinations or its 490-trial saved plan.
The 20 reduced admission trials and all original negative findings remain.
Bettina B5 screening is reused, including +105.33% B routine regression in the
concentrated-A screen and the mixed balanced skew case; no hospital is selected
post hoc to hide a regression. Historical raw diagnosis summaries are not present
in this checkout, so a new constant arm cannot be claimed to match seed60202's
historical image/configuration. The independent holdout is separate evidence.

Existing Wayne W2–W4 and Bettina B1–B4 reviews are recorded at their stated
revisions. Runtime scheduler code at `58f987b` and the supplied main matches;
Bettina's later change is documentation. These approvals do not automatically
approve newer pressure-health/bounded-deferral performance claims. Wayne W5 must
review pressure/overload assumptions and matched admission results; Bettina must
review the final-policy scheduler comparison and both-site trade-offs; Afiq A5
must review failure/eligibility, uncertain-attempt and duplicate-domain-effect
coverage beyond recorded functional checks. No owner approval is assumed or
message sent. H5 is the completed assessment task even when targets fail; W5/A5
and final component acceptance remain independent.
