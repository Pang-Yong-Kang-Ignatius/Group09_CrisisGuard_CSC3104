# Bettina scheduler review: 2 November 2026

This review covers Bettina's scheduler responsibilities against the local
checkout after fast-forwarding `bettina` to merged `origin/main` commit `58f987b`.
It records the approvals and the limits of the available proposal-performance
evidence; it does not claim that the full calibrated D2/D3 matrix passed.

## Snapshot and proposal references

- The supplied `CrisisGuard-main (4).zip` had SHA-256
  `34b45dff89ce21b05d7c4f83d6f2e63327c36b257f816f4e0fb2927aadb2a70b`.
- The local Git checkout was clean on branch `bettina` at `b0d5a3c`. After
  fetching `origin/main`, Bettina was fast-forwarded to `58f987b` (`Update TODO.md`)
  with no Bettina-only commits to preserve. The focused regression approval was
  rerun against this merged source revision.
- The locally available `GROUP09.pdf` describes the Appendix B1 local selector
  and Appendix D3 thresholds reviewed below. Its SHA-256 is
  `7605bb32d9554634f74324e495272d7889c1982ae814939e28efb6f8306c07f6`.
  The CP8 evaluation report refers to a different submitted-PDF SHA-256
  (`ed4f6d8703aeb86ca2d324d1524c556ab968dd0437b44aefe3cb16a0506e5847`), which
  was not available in the checkout. Confirm the submission copy before making a
  document-version claim.

## B1: local scheduler specification

Reviewed `edge/scheduler/README.md`, `dag.py`, `priority.py`, `scheduler.py`,
and their focused tests against Appendix B1 in the locally available proposal.

- Eligibility requires pending work, centrally acknowledged prerequisites, no
  active attempt, an elapsed retry deadline, and no applicable cycle or permanent
  blockage exclusion.
- Ageing follows `base + floor(wait / interval)`; missing timestamps do not age.
- Inheritance uses the maximum aged priority of the event and healthy unfinished
  descendants. Applied, quarantined, cyclic, permanently blocked, or unresolved
  missing-prerequisite workflows do not contribute urgency.
- Ties use the oldest creation timestamp and then event ID. Acknowledged
  prerequisites are supplied through `applied_ids`; local capture or submission
  is not enough.
- Added explicit fairness and starvation assumptions to the scheduler README.
  Ageing does not promise eventual completion without continued eligibility,
  finite retry delays, eventual prerequisite ACKs, service/controller progress,
  and arrivals within service capacity. Inter-site fairness belongs to the
  controller and its conditional grant bound is not a time or completion bound.

**Review result:** no mismatch or scheduler defect found in the reviewed
specification. No runtime behavior was changed for this review.

## B2: experimental scheduling integration

Reviewed the opt-in FIFO, priority, and DRR controller variants and the replay
policy boundary at commit `58f987b`. The experimental switches require explicit
opt-in; production defaults remain bounded controller allocation and priority
replay with inheritance enabled. Focused replay and policy tests confirm that
dependency eligibility remains enforced, uncertain attempts remain pinned, and
failed-attempt safety is not bypassed by the policy choice.

**Review result:** approve the experimental scheduling integration at merged
commit `58f987b`, within the test/evidence scope below.

## B3: inheritance ON/OFF and Inspector

The active replay path and Inspector share the validated scheduler settings.
Inheritance OFF continues to apply each event's own aged priority and still
enforces dependencies; Inspector previews are read-only and do not change active
replay settings. The focused replay tests observed the expected ON/OFF selection
orders. Eight targeted configuration/replay/Inspector tests passed.

**Review result:** approve ON/OFF behavior and the active-policy/Inspector
consistency at merged commit `58f987b`.

## B4: regression approval for final merged commit 58f987b

The following scheduler and policy regression groups passed against commit
`58f987b` under the workspace Python 3.12.14 environment:

- 185 scheduler, DAG, priority, replay-policy, and experimental-policy tests.
- 8 targeted active-policy, Inspector-preview, ageing, and replay/log consistency
  tests.
- **193 passed** across the selected runs; one existing Starlette/httpx
  deprecation warning was reported.
- Retained run output: `docs/evidence/bettina-scheduler-regressions-20261102.txt`.

Broader hospital API/TestClient cases were not counted; an earlier attempt to
include those cases stalled in this local environment.

**Review result:** Bettina approves the scheduler regression evidence for merged
commit `58f987b`. If later changes modify scheduler source or its tests, review and
rerun the focused regressions against the resulting final commit.

## B5: matched Round Robin comparison, routine p95, and fairness

The existing 30 October matrix already contains matched `bounded_on` and `rr_on`
trials. It used the same generated graphs per seed and common AIMD settings across
policies, with five seeds per condition. The 10:1 backlog, depth-3,
urgent-concentrated-at-A case (`ratio10-1_depth3_a_only`) is the closest recorded
skewed-dependency case to the proposal target. This is the existing matched matrix
reported in Haris's CP8 evaluation; no experiment was repeated. The paired
results below are recorded for Bettina's B5 review and Haris's H5 assessment.

In that case, `bounded_on` versus `rr_on` showed:

| Measure | Paired result | Interpretation |
|---|---:|---|
| Hospital A urgent workflow p95 | -29.49% relative change; mean difference -3.712 s; 95% paired bootstrap interval [-5.481, -1.787] s; exact two-sided sign-flip p=.0625 | Exceeds the 10% improvement threshold in this screening case; only five pairs |
| Hospital A routine workflow p95 | -3.41% | Improved in this case |
| Hospital B routine workflow p95 | +105.33% | Fails the proposal's no-more-than-20% routine regression guardrail |
| Global backlog drain | -9.72% | Faster in this case |

The other depth-3 10:1 balanced condition was mixed: A urgent p95 was +17.10%
(slower), B urgent p95 was -30.79% (faster), and A routine p95 was +4.05%.
These per-site results do not support a general urgent-latency claim.

Fairness evidence in the 280-trial screen recorded 1,584 sampled conditional
ticket witnesses and zero observed bypass-bound violations. With two sites and
`B=1`, the controller's conditional bound is two grants ahead for a continuously
eligible ticket while allocation progresses. Samples do not cover every
eligibility interval and do not establish a sustained fairness, resource-share,
or elapsed-wait guarantee.

**Acceptance result:** The available matched screen supports an urgent-p95
improvement in the concentrated-A case, but the B routine-p95 regression exceeds
the proposal guardrail. The study used 44-event workloads and an uncalibrated
2-RPS read load; it did not complete the calibrated 30%/70% matrix or establish a
preregistered full D2/D3 acceptance result. B5 is assessed with these results and
limits, not as proposal-wide acceptance.

Evidence: `docs/archive/evaluation/CP8_EVALUATION_20261030.md` and
`docs/evidence/cp8-matrix02-20261030-statistics.json` (matched paired results),
`docs/evidence/cp8-matrix02-20261030-configuration.json` (workloads/settings),
and `docs/archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md` (remaining calibration and
acceptance limits).

## Owner decision

Approve the local scheduler specification, experimental integration,
inheritance ON/OFF behavior, and focused scheduler regressions at merged commit
`58f987b`. Record the B5 comparison as mixed: the concentrated-A urgent-p95
screen improves, while Hospital B routine p95 exceeds the proposal guardrail.
Proposal-wide calibrated D2/D3 acceptance remains unestablished.

---

## Final-Policy Scheduler Follow-Up — 4 November 2026

### Scope and functional verification

Bettina reviewed the [H5 final-policy holdout](H5_FINAL_POLICY_HOLDOUT_20261102.md),
[W5 admission validation](W5_AIMD_RECOVERY_VALIDATION_20261102.md) and
[H5/W5 reconciliation](H5_W5_REVIEW_20261103.md) for scheduler populations,
both-site trade-offs and proposal measurement definitions. This completes the
scheduler evidence-review task; it does not extend the historical `58f987b`
approval to controller, admission, retry or infrastructure performance.

The earlier local run was reported as **284 passed, 0 failed, one dependency
deprecation warning**, in 5.74 seconds on Python 3.14.3. Its exact invocation and
source revision were not retained in this report, so it is not assigned
retroactively to the current commit.

A fresh focused run on 4 November 2026 against branch `bettina`, source revision
`9fbc918be76cda2148fb01970e6e21a4a3c09aa7` (only this report was modified at run
start), independently returned **285 passed, 0 failed, one existing
Starlette/httpx deprecation warning**, in 0.80 seconds on Python 3.14.3:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ../.venv/bin/python -m pytest -p no:cacheprovider -q tests/unit/test_dag.py tests/unit/test_priority.py tests/unit/test_scheduler*.py tests/unit/test_cp8_policies.py
```

This selection covers DAG eligibility/cycles, ageing and inheritance,
deterministic selection, scheduler configuration/validation, Inspector/session
behaviour and replay-policy integration. These runs overlap and are not added
together. Focused functional verification does not replace the outstanding
Python 3.11, final integration or hosted-CI verification tasks.

### Final-policy scheduler comparison

H5 compared bounded B=1 against RR with priority inheritance under identical
AIMD and retry settings, using five matched seed pairs per load and 88-event
A-only urgency-skewed graphs. The loads, 36/84 RPS, are 30%/70% of a provisional
120-RPS read-only reference, not measured sustainable mixed-workload capacity.

The following effects are means of per-seed relative changes versus RR;
negative latency/drain changes mean improvement:

| Measure | 36 RPS | 84 RPS |
|---|---:|---:|
| Hospital A urgent workflow p95 | +0.38% | +3.32% |
| Hospital A routine workflow p95 proxy | -1.51% | -6.90% |
| Hospital B routine workflow p95 proxy | +15.64% | -22.57% |
| Global original backlog drain | -0.74% | -13.08% |

Neither urgent point estimate meets the required improvement of at least 10%.
Hospital B urgent p95 is undefined because the A-only urgency population has
no B urgent events. Routine p95 measures final-required-event workflow
completion after observed reconnection, not exact queue-only waiting.

Both sites' routine point estimates satisfy the scoped maximum +20% regression
guardrail, but B regresses at 36 RPS and improves at 84 RPS. Uncertainty remains
material: B's 95% paired bootstrap intervals are [+3.36%, +29.66%] and
[-61.04%, +24.81%], respectively. These are statistical intervals, distinct from
observer timing bounds. Five pairs cannot yield an exact two-sided sign-flip
p-value below .0625; no general or statistically significant benefit is claimed.

### Evidence limits and W5 boundary

H5's original strict run failed its exact-network-set preservation guard.
All 40 trials passed generator/functional validity checks, but **zero were
strictly accepted**. Its numerical scheduler effects are post-hoc resource-guard
sensitivity evidence, not primary acceptance. No network waiver or infrastructure
owner approval is inferred by this scheduler review.

The historical B5 concentrated-A screen remains separate: Hospital A urgent p95
improved 29.49% while Hospital B routine p95 regressed 105.33%, exceeding the
routine guardrail. Different workloads/acquisition scopes prevent pooling that
screen with H5 or using it to clear the final-policy urgent target.

W5 contains no matched RR scheduler arms. Its separate selected admission study
failed the +20% drain and both-site routine guardrails at both loads: drain
+75.82%/+54.43%, A routine +69.65%/+59.25%, B routine +56.00%/+76.75%.
Its favorable whole-run SLO fractions retain unequal-exposure and replacement
limitations. W5 cannot replace an H5 arm, clear H5's network failure or establish
a scheduler benefit. This review used the checked-in reports/compact evidence;
it is not independent reproduction of either study's original raw archives.

Full D2/D3 performance acceptance, exact queue-only waiting, sustained fairness,
isolated scheduler cost, representative mixed-workload capacity and genuine
overload protection remain unestablished. No new experiment or implementation
change was made for this follow-up.

### Owner assessment

**Functional verification and final-policy scheduler evidence review: completed.**
Both-site routine outcomes, urgent-population coverage and measurement limits
have been reviewed. **Final-policy performance acceptance: not established.**
The diagnostic evidence does not demonstrate the required urgent-latency
improvement and cannot override H5's failed strict preservation guard.

Reviewed by: Bettina
Review date: 4 November 2026
