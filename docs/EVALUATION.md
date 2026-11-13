# Local inheritance evaluation

Updated 3 November 2026. Owner Haris.

## W5 evidence reviewed in H5, 3 November 2026

The [H5 review of Wayne W5](reports/H5_W5_REVIEW_20261103.md) incorporates his
separate selected five-pair/load admission study. Recalculated compact CSV/JSON
effects confirm drain +75.82%/+54.43% and both hospitals' routine p95 above +20%:
these targets failed. Whole-run SLO met the scoped guardrail, with unequal-run
exposure and replacement limitations retained. W5 execution and this evidence
review are complete; raw-archive reproduction and component acceptance are not
implied. H5's own failed network guard, zero strict accepted trials and scheduler
limitations remain unchanged. The studies share seed numbers and are not pooled
as additional independent workload seeds. No new experiment was executed.


## H5 final-policy assessment, 2 November 2026

The [new H5 assessment](reports/H5_FINAL_POLICY_HOLDOUT_20261102.md) records
40 new frozen final-policy holdouts (40 generator/functional-valid,
0 strict accepted due to the failed network-identity guard), 3520 original events,
per-seed effects, both hospitals' trade-offs, original whole-run SLO and the
predefined ten-second readiness-aligned diagnostic. It closes H5's assessment
work, even where targets fail or remain unestablished. Strict acceptance remains
unestablished; descriptive effects are a separately labelled post-hoc guard
sensitivity, not a replacement for the original failed run. These are four scoped
question/load cells at 36/84 RPS against a provisional 120-RPS read-only reference,
not full D2 or maximum sustainable mixed capacity. Genuine-overload protection,
full correctness/fairness coverage and component performance approvals remain open.

Historical H1–H4/H6, the 280 scheduling/35 cost-stress trials and all original
negative results are unchanged. The following dated records retain their scope.

The [final reduced H1/H2 report](archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md) records
20 valid trials and five paired seeds per read load. AIMD failed drain/routine
recovery guardrails while reducing end-to-end live SLO violations. The full D2
matrix is intentionally unexecuted; maximum mixed-workload capacity and urgent
improvement against RR remain unestablished. Earlier study records follow.

The [checkpoint 8A/8B report](archive/evaluation/CP8_EVALUATION_20261030.md) supersedes the review
status below: the historical summary audit and figure review have now passed,
and retained fixed/AIMD, fairness and telemetry statistics were recalculated
without rerunning those experiments. The original inheritance event traces
remain missing: the Downloads backup contains only the five matching summaries.
Real replay inheritance and controller baseline switches are now implemented,
opt-in and tested; the new bounded scheduling screen and its limits are tracked
in that report and [reproduction guide](../evaluation/experiments/RECOVERY_MATRIX.md).


The executed follow-up now includes 280/280 real scheduling trials, 35/35
service-wall-cost stress trials, three separate cost replicas and functional
automatic/constant-rate pilots. Compact configurations, per-seed tables,
statistics, graphs, adverse records and backup/test evidence are linked from
the dated report. These are bounded uncalibrated screening measurements;
full calibrated live-write/D2/D3 acceptance and independent link partitions
remain open. Historical raw inheritance traces remain missing.

Historical local-study record follows. Owner Haris. This records completed local evaluation work,
not completion of the project's system-level recovery study.

## Implemented and verified

- Deterministic synthetic two-hospital workload generator with unique IDs,
  configurable backlog ratios, dependency depth, urgency distribution and seeds.
- Inheritance OFF/ON runner using the existing scheduler on identical inputs.
- Matrix runner preserving workloads, source fingerprints, per-seed paired
  differences, raw timing samples, event/workflow traces and aggregate CSVs.
- Haris reported 132 passing unit tests on his pre-sync evaluation branch and
  completed all 40 cases of `haris-matrix01`, with zero unresolved events in either
  mode. This test count is historical; rerun after merging newer main changes.

## Result analysis and selected evidence

The analysis tool is implemented, and Haris supplied a file listing containing
the report, audit JSON and all three figures in PNG/SVG/PDF formats. Confirm
`audit_status` in the generated audit is `passed` and review the figures before
using them in the presentation. A file listing alone does not establish audit
success. Commands and detailed interpretation are in
the retained [CP8 evaluation](archive/evaluation/CP8_EVALUATION_20261030.md)
and the `evaluation.experiments.analyze_inheritance_results` source.

```powershell
.\.venv311\Scripts\python.exe -m pytest tests/unit/test_inheritance_analysis.py -q
.\.venv311\Scripts\python.exe -m evaluation.experiments.analyze_inheritance_results --matrix-dir evaluation/results/inheritance-matrix01 --audit-only
.\.venv311\Scripts\python.exe -m evaluation.experiments.analyze_inheritance_results --matrix-dir evaluation/results/inheritance-matrix01 --output-dir evaluation/results/inheritance-matrix01-figures
```

Choose a new output directory if the figures already exist; the analyzer does
not overwrite existing output. It reads the five summaries and does not need
Docker, AWS, a controller or the per-case raw run folders.

| Figure | Interpretation |
|---|---|
| `urgent_position_reduction` | Mean paired OFF-minus-ON urgent p95 position; positive means earlier completion |
| `routine_position_change` | Mean paired percentage increase in routine p95 position; positive means later completion |
| `scheduler_cpu_ratio` | Median per-seed ON/OFF scheduler-call CPU ratio; above 1 means more computation |

Whiskers show observed seed minimum and maximum, not confidence intervals.
Empty urgent populations stay undefined; incomplete populations are counted and
excluded from paired effects. The audit checks summary consistency and depth-0
controls, not raw traces or deployed execution.

The essentials-only Git policy retains these files from `inheritance-matrix01`:

- `manifest.json`
- `matrix_summary.json`
- `per_seed_metrics.csv`
- `paired_differences.csv`
- `condition_summary.csv`

Keep `runs/`, demo results, generated workloads, figures, reports, audit outputs
and `validation_example/` ignored. Preserve the original full run in a separate
backup. The located backup is `C:\Users\muhdh\Downloads\CrisisGuard-inheritance-matrix01-backup.zip`; it contains only these five summaries, not raw traces. The five retained files
support summary auditing and regeneration of figures, but do not replace raw
event evidence. Fresh runs can regenerate synthetic workloads and traces from
the recorded configuration/source; their timing samples will differ. Never
label regenerated measurements as the historical run.

## Reproduce from the repository root

```powershell
.\.venv311\Scripts\python.exe -m pytest tests/unit -q
.\.venv311\Scripts\python.exe -m evaluation.experiments.run_inheritance_matrix --run-id haris-matrix02 --output-dir evaluation/results/inheritance-matrix02
```

Use a new output directory. The completed `inheritance-matrix01` folder should be
retained with its workload inputs, manifests, summaries and raw traces. Source
revision at Haris's reported run: `e227296` (before newer main changes).

The default local matrix is 8 conditions x 5 seeds (3104-3108), 440 total events
per workload, ratios 1:1/10:1, depths 0/3 edges, balanced/A-only urgency and three
timing repetitions. Repetitions are not independent workload seeds. This is not
the proposal's full system matrix with live-load levels and baseline policies.

See [generator instructions](../evaluation/workloads/README.md) and the
[CP8 evaluation](archive/evaluation/CP8_EVALUATION_20261030.md) for the
historical matrix method and its evidence limits.

## Reported results from Haris's condition summary

For depth-3 chains, the table shows mean paired percentage change in per-site p95
workflow completion position across five seeds. Positive urgent reduction means
earlier completion; positive routine increase means later completion.

| Backlog A:B | Urgency distribution | Hospital | Urgent p95 position reduction | Routine p95 position increase |
|---|---|---|---:|---:|
| 1:1 | A only | A | 57.86% | 1.21% |
| 1:1 | Balanced | A | 78.01% | 1.78% |
| 1:1 | Balanced | B | 78.12% | 1.70% |
| 10:1 | A only | A | 77.40% | 2.01% |
| 10:1 | Balanced | A | 79.36% | 2.34% |
| 10:1 | Balanced | B | 64.12% | 0.00% |

All 30 applicable seed/site comparisons with dependencies and urgent workflows
improved urgent p95 position. Depth-0 position metrics matched between modes.
A-only conditions have no urgent B population, so urgent B values are undefined.
Across all groups, mean routine p95 position increases ranged from 0% to 2.34%;
this is not a maximum per-workflow delay or per-seed bound. No unresolved events
or incomplete workflow pairs were reported in the supplied condition summary.

## Meaning and limits

Completion position counts local selections from a fixed snapshot. Each selected
event is immediately acknowledged in simulation. Hospitals drain independently;
there is no shared-cloud competition, controller, live traffic, HTTP or database
processing. Ageing time does not advance during a drain. Seed variation changes
routine priorities and urgency placement within the chain workload family.
Balanced urgency means approximately equal fractions per site's workflows.
Depth changes workflow and urgent-terminal counts because urgency is defined as
a fraction of workflows, not a fixed urgent-event count.

The results support local prerequisite-urgency propagation for these tested
synthetic inputs. They do not establish recovery-time speedup, inter-site fairness,
admission bounds, statistical significance, clinical suitability or satisfaction
of the proposal's final acceptance targets. Routine trade-offs must be retained.

## Remaining work

- Review the generated local figures and confirm the summary audit succeeds;
  retain routine trade-offs, undefined populations and observed seed spread.
- Back up the original full run and record its location before discarding any files.
- Prepare a small recovery measurement pilot using existing routing and automatic
  replay; independently observe cloud application of the original event IDs.
- Coordinate a real replay inheritance OFF/ON switch with Afiq/Bettina and
  controller grants with Wayne before making those system comparisons.
- Implement and evaluate the proposal's round-robin, bounded-override, unrestricted
  priority and DRR comparisons under common admission rules.
- Compare fixed/adaptive admission under live traffic; measure actual urgent p95
  recovery time, routine waits, backlog drain, shares, faults, CPU and memory.
- Run paired system experiments, report incomplete work and uncertainty, and
  complete the article, testing PDF and demonstration video.
