# W5: AIMD recovery-performance validation

Measured 2 November 2026; documented 3 November, Singapore time.
**W5 validation is complete; the proposal performance requirements do not pass.**
The final experimental policy still exceeds the drain and routine-p95 guardrails
against matched constant-rate admission. No production promotion or full D2/D3
acceptance follows.

## Final results

Twenty selected trials are valid under the prospectively frozen 99.9% issuance
rule: five pairs per load, with one explicitly disclosed generator-invalid arm
replaced once. The original invalid arm and both infrastructure-rejected attempts
remain preserved. In total, 23 trial executions completed their functional checks.

Effects below are mean paired changes; brackets are 95% paired bootstrap
intervals. Positive latency/drain values mean AIMD was slower.

| Measure | 36 RPS | 84 RPS | Guardrail decision |
|---|---:|---:|---|
| Backlog drain, % | +75.82 [43.68, 108.41] | +54.43 [25.86, 72.78] | Failed: maximum +20% |
| Routine p95 A, % | +69.65 [35.39, 103.91] | +59.25 [17.24, 101.26] | Failed: maximum +20% |
| Routine p95 B, % | +56.00 [32.30, 78.04] | +76.75 [44.17, 103.14] | Failed: maximum +20% |
| Full-run live SLO difference, pp | -2.236 [-2.885, -1.402] | -1.639 [-2.288, -0.977] | Achieved in this read-only scope: maximum +1 pp |
| Observed functional violations | 0 | 0 | Recorded checks passed; full coverage unestablished |

Exact two-sided sign-flip p-values are .0625 or .125, not below .05. Decisions
above use the specified point-estimate guardrails, not significance claims.
Observer-relative drain bounds are +74.63% to +77.95% at 36 RPS and +53.27% to
+56.29% at 84 RPS; these measurement bounds are not confidence intervals.
Mean absolute drain times are 23.94/41.97 seconds (constant/AIMD) at 36 RPS
and 24.61/37.17 seconds at 84 RPS. Ratios of those means are not the paired effects.

### Load validity and replacement

All 36 RPS trials have zero skips. At 84 RPS the selected trials retain five
genuine lag skips: four across constant arms and one AIMD skip; every ledger
reconciles. The least accurate selected trial issued 99.941% of opportunities.
There were **37,875 attempted requests and five unissued opportunities** across
the selected study. No skips enter the live SLO denominator.

The original 84 RPS seed60103 AIMD arm issued 2,736/2,854 opportunities
(95.865%): 117 cap skips and one lag skip. Startup transport timeouts filled
the fixed 128-request pool; cap misses occurred at approximately 1-5 seconds,
before recorded readiness at 17.020 seconds. It recorded 256 ConnectTimeouts
and 43 RemoteProtocolErrors, not server rejections. Its 1,424 requests started
after readiness had zero observed violations. No controller change follows
from this generator failure.

After all original 20 trials finished, a separate prospective replacement
protocol allowed **exactly one** new AIMD execution with identical settings,
source and graph, paired with the unchanged original constant arm. The
replacement issued 3,680/3,680 requests with zero skips and drained in 43.729
seconds. Only that generator-valid replacement is selected; the invalid
33.928-second original remains unchanged and excluded. This is a disclosed
retrospective pairing across acquisition times, not fully prospective random
assignment of the replacement. No performance-based policy tuning occurred.

At 36 RPS all five pairs survive both stricter 99.99% and zero-skip rules;
the failures remain. At 84 RPS those rules retain **only one pair**: their
five-pair acceptance is **unestablished**, not an alternative success claim.
The primary 84 RPS decisions depend on the disclosed 99.9% engineering rule.

### Live SLO exposure

| Load | Constant full-run violations / attempts | AIMD full-run violations / attempts |
|---|---:|---:|
| 36 RPS | 228 / 4,320 | 224 / 7,563 |
| 84 RPS | 533 / 10,355 | 553 / 15,637 |

These pooled counts describe exposure; headline effects use paired seed fractions.
Longer AIMD runs accumulate more successful requests after startup, reducing
the full-run violation fraction. There were **zero observed violations in
22,872 requests started after readiness**. Only three pairs at each load have
the entire predefined ten-second post-ready window; their paired SLO difference
is zero. Other fixed-window comparisons are unavailable. Neither the favorable
whole-run fraction nor post-ready zero proves genuine-overload protection or
replaces the primary metric.

## Regression evidence and acceptance limits

- Final local Python 3.14 unit suite: **1,190 passed**, one dependency
  deprecation warning. This includes the two new database-retention safety tests.
- Earlier focused admission/retry/evaluation suite: **131 passed**; included in
  the full-suite population, not additional distinct tests.
- Real PostgreSQL controller/recovery suite: **68 distinct cases passed per
  execution**, confined to each trial's private database and test schemas.
- All 20 selected raw records equal their summaries. Exact paired graph/config
  checks, identical measured-source fingerprints, original event contents,
  prerequisite/grant order and local applied state were rechecked by the audit.
  All **1,760 original selected events** applied with completed grants; duplicate
  completed-grant probes returned 409 without adding rows.
- Original containers/images/mounts/states and networks passed each selected
  preservation check. All original volumes and all trial database volumes remain.

The load environment was Docker 29.9.2, 20 logical CPUs and 8,171,204,608 bytes
allocated RAM, with one experiment worker. Cloud/edge ran Python 3.11.18;
the local harness/tests used Python 3.14. Actual package freezes and image ID
are preserved. Requirements are unpinned, so a fresh future rebuild is not
guaranteed bit-identical. Historical absolute timings on other hosts are not
direct causal comparisons with this laptop's timings.

Unit fault injection continues to check pressure/error/DB degradation and stale
telemetry denial. It establishes implementation behavior, not calibrated
sensitivity to genuine service overload. Mixed-workload sustainable capacity,
full D2, urgent improvement against RR, complete failure/eligibility and
duplicate-domain-effect coverage remain unestablished. Haris's independent H5
review and component-owner acceptance remain pending; this report is the review
handoff, not an external sign-off. Original historical raw archives were not
available for independent readback on this laptop; their existing H3 limitation
remains. The new local archive is verified separately below.

## Question and acceptance rules

Can the existing experimental AIMD recovery policy meet the proposal guardrails
against compatible constant-rate admission? The repository's TODO and D3
assessment require backlog drain and routine recovery p95 regression at most
20%, live end-to-end SLO degradation at most one percentage point, and zero
observed correctness violations. Full proposal acceptance additionally needs
the separately specified capacity, urgent/RR, overload and coverage evidence.

W5's checklist permits completed validation to record a failed or unestablished
target. A completed evaluation is distinct from passing the proposal targets.
Haris's independent H5 review and owner approval must not be inferred from this
local execution.

## Historical evidence preserved

The original five-pair/load study (seeds 3104–3108) remains unchanged:

| Original outcome | 36 RPS | 84 RPS |
|---|---:|---:|
| Drain regression versus constant | +278.52% | +276.34% |
| Routine p95, Hospital A | +607.15% | +442.53% |
| Routine p95, Hospital B | +497.18% | +369.90% |
| Full-run SLO difference | −2.95 pp | −3.01 pp |

The later seed60202 retry pilot compared two AIMD retry policies, not AIMD
against constant admission. Drain improved from 63.343 to 33.828 seconds,
but B routine p95 worsened 40.95% and full-run SLO worsened 1.726 pp. Neither
that pair nor this new experiment revises the original negative results.
See [original assessment](../archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md) and
[diagnosis](AIMD_RECOVERY_DIAGNOSIS_20261101.md).

## Frozen matched experiment

Local source: `58f987b46b65bf6f214909dd0d696d469e8cec39`. Read-only remote
verification found `main` at `5db304de9829164d0e258866ea24b671b7a0c959` during
execution. Fetching that commit into the isolated copy showed additions to
test tooling/documentation and ignore/coverage configuration; Cloud, controller,
edge runtime, evaluation scripts, runtime requirements and deployment files
match this source (the only `edge` difference is its scheduler README).
The original laptop working tree remained clean at `58f987b` at this check.
No merge was performed, and unrelated remote changes were not copied over.
The experiment-only retention addition is captured by the source fingerprints.

Twenty sequential trials use seeds 60101–60105, separate from all historical
calibration and retry pilots, at each of 36 and 84 authenticated existing-record
reads/second. These are absolute loads, corresponding to 30%/70% of the earlier
**provisional 120 RPS read-only reference**, not measured mixed-workload capacity.
Each matched pair uses the same balanced 88-event depth-3 graph, automatic
outage/reconnection, bounded scheduling with inheritance, 600-second ageing,
128 live-request workers, 250 ms SLO, 300-second timeout and one experiment worker.
The first arm alternates by seed/load. Plans and their hashes were saved before
any outcome in the batch.

Both arms explicitly use `pressure-experimental` health and `bounded-deferral`
retry handling. AIMD retains its 2/20 RPS floor/ceiling, additive step 2 after
two healthy votes, multiplicative decrease, 1-second polling, 3-second dwell,
5-second freshness and existing state-dependent in-flight caps 4/2/1. The
constant comparator retains the same health/freshness/caps with 20 RPS in every
state. Only admission algorithm and its prescribed state rates differ; paired
graphs, all other effective settings and measured source fingerprints are audited.

The primary generator rule (at least 99.9% issuance with exact ledger) and
99.99%/zero-skip sensitivity were frozen before acquisition. Request failures
remain measured outcomes; skips are reported separately. Primary SLO counts
non-200, transport failure or latency >250 ms once per attempted request for the
entire run. A predefined first ten seconds after recorded all-Cloud readiness
is supplementary; missing exposure is unavailable, never an assumed zero.

Effects are means of per-seed AIMD-minus-constant changes, not pooled ratios.
The existing assessor supplies nearest-rank routine workflow-completion p95
after observed reconnection, observer endpoints, paired bootstrap intervals and
exact sign-flip tests. There are only 5–6 routine workflows/site/trial. Five
independent pairs cannot produce an exact two-sided p below .0625. Statistical
uncertainty and observer timing bounds are distinct; no p<.05 claim is justified.
Absolute workflow p95 is also retained in the CSV and raw measurements.

## Isolation and rejected attempts

Tests run in `[REDACTED_LOCAL_PATH]`, a copy of the clean
working tree and Git metadata, not the shared deployment. Each trial creates a
unique `cg-cp8-*` Compose project, fresh PostgreSQL/SQLite volumes, synthetic
credentials and loopback ports. Integration fixtures explicitly target that
private PostgreSQL database and use their own schemas. No production/shared
database URL, AWS deployment, commit, push or merge is used.

`--retain-test-databases` keeps every trial's database volume. Label/baseline
fences precede cleanup, which removes only owned containers/networks. Final
checks require unchanged original container identities/images/mounts/states and
network IDs, all original volumes, and only the explicitly recorded owned
volumes added. Exact volume equality is intentionally replaced with this
auditable retention rule. No test database or volume is deleted.

Two earlier constant-only attempts are retained and excluded for infrastructure
preservation failure: Docker's built-in bridge ID changed during startup, then
pre-existing `crisisguard-local` services started concurrently. Both completed
88 events and passed 68 PostgreSQL regressions, but neither counts as a valid
matched trial. After the user confirmed other tests were paused, the complete
frozen batch restarted in `w5-holdout-20261102-r3`, without policy changes or
performance-based selection. Existing services were left in their observed state.

## Implementation decision

The original throughput guard treats healthy demand as overload, suppressing
healthy votes and holding AIMD at its floor. That documented configuration/proxy
limitation is not evidence of an arithmetic or grant-fencing defect. The existing
experimental pressure policy tests measured degradation instead; bounded deferral
avoids escalating failure backoff for definite fresh admission deferrals.
No controller or edge algorithm, production default, authentication, grant
contract, telemetry definition or AWS setting was changed for W5. The only runner
addition retains test databases to comply with the requested execution boundary.

The new trace confirms that the experimental policy leaves the original 2 RPS
floor, but conservative recovery/ramp and edge retry/reconciliation waits still
cost time. In the slowest selected AIMD run (36 RPS, seed60105), Normal/20 RPS
was observed at 28.878 seconds with 33 events remaining, yet final drain was
64.718 seconds. Its final retry counters total 65 and its bounded-deferral journal
records 18 rate-limit deferrals. The controller remained fresh/healthy at its
ceiling after that transition; rate-floor misclassification alone cannot explain
this tail. Final counters do not identify every per-attempt delay, so no complete
causal attribution or safe controller rewrite is justified. Raising the floor,
skipping freshness or weakening fencing merely to pass the timing limit would
need separate safety evidence and a new independently frozen experiment.

## Evidence and reproduction

The frozen driver is [run_w5_holdout_20261102.py](../evidence/run_w5_holdout_20261102.py).
The audit reuses the existing `assess_acceptance`, `trial_metrics`,
`generator_accuracy` and paired uncertainty routines; it adds explicit paired
health/retry/source checks, raw/summary hashes and fixed-window accounting.
Pilot condition names omit load, so the audit adds `_load30`/`_load70` only to
derived in-memory copies. Original plans, summaries and raw files remain unchanged.

Selected [compact assessment](../evidence/w5-holdout-20261102.json),
[per-trial CSV](../evidence/w5-holdout-20261102.csv) and
[archive manifest](../evidence/w5-archive-20261102.json) are retained here.
The historical full audit at `evaluation/results/w5-holdout-20261102-r3/assessment.json`
and local ZIP at `evaluation/results/w5-evidence-20261102.zip` are absent from this
checkout and are not supplied by the tracked source. The recorded ZIP included raw
snapshots for all 23 attempts, plans, source, dependency freezes and regression
reports; the historical verification read every entry back and checked SHA-256.
That archive verification has not been repeated by this cleanup. Private Compose files,
private command logs and database binaries are excluded; test volumes are retained
separately. The measured copy and its ignored `.venv/cp8-*` records remain at
`[REDACTED_LOCAL_PATH]`.

Privacy redaction (13 November 2026): `[REDACTED_LOCAL_PATH]` labels the
removed private workstation root. Any suffix retains the original relative
run/file location; the label is not a usable filesystem path. Measurements,
recorded hashes, timestamps and outcomes are unchanged. Historical checksums
refer to original artifacts, not these redacted copies. The
[redaction manifest](../evidence/w5-local-path-redaction.json) records the
pre-redaction and redacted file checksums and existing historical-hash limits.
No experiment or archive verification was repeated for this redaction.

From the prepared isolated repository root, with Docker running and other load
tests paused (a fresh copy first needs its own virtual environment/dependencies):

```powershell
.\.venv\Scripts\python.exe -m docs.evidence.run_w5_holdout_20261102 --output-name NEW-W5-RUN
.\.venv\Scripts\python.exe -m docs.evidence.analyze_w5_holdout_20261102 --output-name NEW-W5-RUN
```

Use a new directory; never overwrite historical evidence. Runs retain database
volumes and take tens of minutes. Dependency versions, host/container versions,
test reports, source hashes, raw snapshots and local archive provenance accompany
the final assessment. Local retention is not an off-machine backup claim.
