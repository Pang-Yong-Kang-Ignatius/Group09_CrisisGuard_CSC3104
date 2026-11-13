# CrisisGuard Evidence Index

Use this index for the final submission's retained results and their limits.
Dates and source revisions belong to the recorded runs; this documentation
reorganisation did not execute tests, experiments or deployments.

## Current documentation

| Need | Guide |
|---|---|
| Windows setup and application startup | [README](../README.md#5-python-environment-setup), [Docker guide](DOCKER.md) |
| Automated and manual testing | [Testing guide](testing.md) |
| Event schemas and HTTP results | [Cloud API contract](CLOUD_API_CONTRACT.md) |
| Dashboard capture, monitoring and Inspector | [Dashboard guide](DASHBOARD.md) |
| Scheduler/admission configuration and metrics | [Configuration](CONFIGURATION.md), [metrics contract](reference/METAGUARD_METRICS_CONTRACT.md) |
| AWS deployment, health, backup and rollback | [AWS guide](../infrastructure/aws/README.md), [health checks](runbooks/AWS_HEALTH_CHECKS.md), [backups](runbooks/BACKUPS.md), [updates](runbooks/DEPLOYMENT_UPDATES.md) |
| Outstanding submission work | [Requirements audit](reports/CORE_REQUIREMENTS_AUDIT.md#requirements-to-implementation-matrix), [evidence availability](#availability-and-historical-records) |

Application startup needs Python 3.11 and Docker Desktop's Linux engine, not
Node.js. Quick/Full tests need Python 3.11, PowerShell and Node.js, but no Docker
engine by default. Node 24 is the documented reference; individual evidence
records retain the versions actually used.

## Final evaluation and research evidence

| Study | Primary report and retained data | Finding to preserve |
|---|---|---|
| Wayne W5, measured 2 November | [Report](reports/W5_AIMD_RECOVERY_VALIDATION_20261102.md), [assessment JSON](evidence/w5-holdout-20261102.json), [20 selected arms CSV](evidence/w5-holdout-20261102.csv) | Five pairs/load at 36/84 RPS. Drain +75.82%/+54.43% and both sites' routine p95 exceeded the 20% guardrail. Whole-run SLO -2.236/-1.639 pp retains unequal-exposure and replacement limits. |
| Haris H5, 2 November | [Frozen specification](reports/H5_EXPERIMENT_SPEC_20261102.md), [report](reports/H5_FINAL_POLICY_HOLDOUT_20261102.md), [strict assessment](evidence/h5-holdout-strict-assessment-20261102.json), [diagnostic assessment](evidence/h5-holdout-assessment-20261102.json), [effects CSV](evidence/h5-paired-effects-20261102.csv), [measurements CSV](evidence/h5-measurements-20261102.csv) | 40 functional trials, zero strict accepted after the network-preservation guard failed. Post-hoc sensitivity cannot replace that failure. |
| H5 review of W5, 3 November | [Reconciliation](reports/H5_W5_REVIEW_20261103.md), [review JSON](evidence/h5-w5-review-20261103.json) | Compact W5 evidence review is complete; independent raw reproduction and owner acceptance are separate. Do not pool the two acquisitions as extra independent seeds. |
| Bettina scheduler review, 2 November | [B1-B5 review](reports/BETTINA_SCHEDULER_REVIEW_20261102.md), [regression output](evidence/bettina-scheduler-regressions-20261102.txt), [screening statistics](evidence/cp8-matrix02-20261030-statistics.json), [configuration](evidence/cp8-matrix02-20261030-configuration.json) | Concentrated-A screening urgent p95 improved 29.49%, while B routine p95 regressed 105.33%. Approval at `58f987b` is revision-specific; full D2/D3 and sustained fairness remain unestablished. |
| Integrated scheduling/cost screen, 30 October | [CP8 report](archive/evaluation/CP8_EVALUATION_20261030.md), [reproduction guide](../evaluation/experiments/RECOVERY_MATRIX.md) | 280 scheduling trials/12,320 events and 35 cost-stress trials/1,540 events; bounded screening, not a calibrated full acceptance matrix. |
| Original reduced H1/H2, 1 November | [Final report](archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md), [reproduction guide](../evaluation/experiments/RECOVERY_MATRIX.md#reduced-h1h2-admission-reproduction) | 20 selected trials under the stated validity rule. Drain +278.52%/+276.34% and routine guardrails failed. The 120-RPS read-only reference is provisional; full D2 was not executed. |
| Experimental pressure/retry pilots, 1 November | [Diagnosis](reports/AIMD_RECOVERY_DIAGNOSIS_20261101.md), [retry-pair assessment](evidence/aimd-deferral-pilot-review-20261101.json), [experimental retry contract](../edge/README.md#automatic-and-protected-replay) | The single retry pair's 46.60% drain reduction accompanied B routine p95 +40.95% and full-run SLO +1.726 pp. No general performance or overload-protection acceptance follows. |

## Regression, recovery and deployment evidence

| Record | Scope and limits |
|---|---|
| [5 November Test 6/Test 7 verification](evidence/PROJECT_VERIFICATION_20261105.md) | Complete local E2E: 18 PASS, zero FAIL/SKIP, including owned cleanup. ZIP provenance fixes and fresh pre-push regressions are recorded separately. Browser, per-event Cloud log correlation and performance acceptance remain NOT VERIFIED; submitted-commit hosted CI is separate. |
| [Retained master-runner snapshot](evidence/PROJECT_TEST_COVERAGE.md) | Read the linked snapshot's own date, source revision, environment, counts and individual PASS/SKIP/NOT VERIFIED statuses. This mutable file is separate from hosted CI and proposal acceptance; changing this index does not execute new tests. |
| [2 November E2E verification](evidence/PROJECT_E2E_VERIFICATION_20261102.md), [validation/source hashes](evidence/PROJECT_FINAL_VALIDATION_20261102.json) | 34 applied events, 34 completed grants and one terminal expired grant. Twelve continuation checks include a configuration step, not twelve fault scenarios. Independent A/B partitions, restarts and lost responses were exercised; broader unexecuted plans and original PostgreSQL content-equality limits remain explicit. |
| [31 October merged integration](evidence/CP8_MERGED_INTEGRATION_20261031.md) | Recorded eight-service persistence/protected recovery on `459cb69`; not evidence for every later source revision. |
| [Cloud contract verification](evidence/CP7D_CLOUD_API_CONTRACT_20261031.md), [Windows startup boundary](DASHBOARD.md#current-verification-boundary) | Dated API/fixture and fresh-source Windows checks; not a genuinely clean-machine installation certification. |
| [Executed scheduler selection](evidence/scheduler-policy-running-20261027.json) | Historical C,A,B running-stack assertions and lost-response reconciliation, distinct from read-only Inspector predictions. This record predates the final article revision. |
| [TX final acceptance summary](../infrastructure/aws/README.md#historical-tx-acceptance-1-november-2026), [AWS evidence](evidence/tx-aws-review-20261101.json) | Historical release `16e74b8`: protected recovery, compatible policy backout, backup/restore and hosted CI. Private-HTTP and current-release power-cycle limitations remain. |
| [7A metrics summary](evidence/cp7a-summary-20261027.json), [regression JUnit](evidence/cp7a-regression-20261027.xml), [isolated Docker observations](evidence/cp7a-docker-final-20261027.json) | The dated mixed-traffic p95/in-flight extension passed 807 regression and 5 Docker cases, with overlap in focused runs. Original running images were not redeployed; live-only p95 and actual queue depth were still missing at that checkpoint. |

## Figures and reproducibility

| Figure family | Exports, data and interpretation |
|---|---|
| H1/H2 drain | [PNG](evidence/figures/cp8-h1-h2-drain-constant-vs-aimd-20261101.png), [PDF](evidence/figures/cp8-h1-h2-drain-constant-vs-aimd-20261101.pdf), [CSV](evidence/figures/cp8-h1-h2-drain-constant-vs-aimd-20261101-data.csv) |
| H1/H2 live SLO | [PNG](evidence/figures/cp8-h1-h2-slo-constant-vs-aimd-20261101.png), [PDF](evidence/figures/cp8-h1-h2-slo-constant-vs-aimd-20261101.pdf), [CSV](evidence/figures/cp8-h1-h2-slo-constant-vs-aimd-20261101-data.csv), [comparison provenance](evidence/figures/cp8-h1-h2-comparisons-20261101-provenance.json) |
| Single retry pair | [PNG](evidence/figures/cp8-aimd-bounded-deferral-pilot-20261101.png), [PDF](evidence/figures/cp8-aimd-bounded-deferral-pilot-20261101.pdf), [CSV](evidence/figures/cp8-aimd-bounded-deferral-pilot-20261101-data.csv), [caption](evidence/figures/cp8-aimd-bounded-deferral-pilot-20261101-caption.md), [provenance](evidence/figures/cp8-aimd-bounded-deferral-pilot-20261101-provenance.json) |

In the H1/H2 comparison figures, connected colors identify paired seeds and
diamonds mark arithmetic means. The effect panels show per-seed effects and a
95% paired bootstrap interval, not marginal confidence intervals for those
diamonds. Dashed lines are the unchanged guardrails. Drain uses the upper
monotonic observer endpoint; SLO fractions count non-200, transport failure or
client latency above 250 ms once per issued request. The [final H1/H2 report](archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md)
retains exact results, invalid-arm replacement, five-pair p-value limits and
stricter generator-validity sensitivities.

Other original figures, raw measurements, scripts and manifests remain under
[evidence/](evidence/). Some earlier narrative checkpoints and failed-attempt
notes were removed before this submission snapshot. Their original development
history is not included in this submission repository; those records cannot be
recovered from its Git history.
The [evaluation overview](EVALUATION.md) links the broader study history. Retained
[workload generation](../evaluation/workloads/README.md),
[recovery matrix](../evaluation/experiments/RECOVERY_MATRIX.md),
[W5 acquisition](evidence/run_w5_holdout_20261102.py) and
[W5 analysis](evidence/analyze_w5_holdout_20261102.py) support reproduction;
their presence does not authorize rerunning expensive or disruptive experiments.

## Availability and historical records

The former `docs/TODO.md` and `docs/reports/MISSING_EVIDENCE_INVENTORY.md` are
unavailable in this checkout; neither historical document has been reconstructed.
The [requirements audit](reports/CORE_REQUIREMENTS_AUDIT.md#requirements-to-implementation-matrix) records implementation and evidence gaps;
its [follow-up notes](reports/CORE_REQUIREMENTS_AUDIT.md#documentation-discrepancies-to-reconcile-after-review) retain component-owner review requirements.

The [reduced H1/H2 report](archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md#source-assessment-and-earlier-combined-figure)
records selected summary paths under ignored `evaluation/results/` directories;
those summaries are not included in the tracked source. The
[W5 report](reports/W5_AIMD_RECOVERY_VALIDATION_20261102.md#evidence-and-reproduction)
records a full audit at `evaluation/results/w5-holdout-20261102-r3/assessment.json`
and an archive at `evaluation/results/w5-evidence-20261102.zip`; both are absent
from this checkout. Local copies elsewhere have not been verified by this cleanup.
The linked reports retain the original paths, provenance and limitations.

Compact evidence does not replace original raw traces. Do not fabricate missing
results or overwrite failed/invalid attempts. The separately supplied final article
was checked during this cleanup; it cites this repository and evidence index.
Its important final report paths remain stable.

The [CP8 scheduling screen](archive/evaluation/CP8_EVALUATION_20261030.md) and
[reduced H1/H2 assessment](archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md)
remain for the final evaluation. The H1/H2 validity rule, protocol amendment,
replacement and original invalid result are retained in the final report. The
Wayne review handoff remains at its
[original path](runbooks/CP7B_WAYNE_REVIEW_HANDOFF.md) because H5's frozen manifest
records its hash; its linked CP7B reports also remain. These dated records do not
imply current approval, a waiver or a new performance claim.

The earlier grant-enforced telemetry study found no successful live-classified
event write: a replay grant plus a live label was deliberately classified as
unclassified, while ungranted live writes were rejected. The unchanged
[Cloud API contract](CLOUD_API_CONTRACT.md#live-provenance-and-executor-queue-extension-27-october-2026),
[metrics contract](reference/METAGUARD_METRICS_CONTRACT.md) and retained raw
[`cp7b-live-write-boundary-20261029.json`](evidence/cp7b-live-write-boundary-20261029.json)
record this limitation. The old study cannot establish production live-write
latency or genuine-overload protection.
