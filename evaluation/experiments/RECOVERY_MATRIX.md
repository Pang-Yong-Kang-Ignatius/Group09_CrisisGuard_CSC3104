# Controlled replay scheduling screen

## Subsequent W5 validation (2 November measurements, 3 November documentation)

The final experimental pressure/bounded-deferral comparison is recorded in
[W5 validation](../../docs/reports/W5_AIMD_RECOVERY_VALIDATION_20261102.md).
Five valid pairs per load show drain +75.82%/+54.43% versus compatible constant
admission, failing the +20% target. Routine p95 also fails. Original adverse
results, infrastructure-rejected attempts and the generator-invalid original
are preserved; one disclosed replacement supplies the final selected arm.
Full D2/D3 and genuine-overload protection remain unestablished.

The W5 driver always uses the new `--retain-test-databases` option. It removes
only owned containers/networks and retains every PostgreSQL/SQLite volume.
Preservation requires original resources unchanged and only the exact recorded
owned volumes added. The historical default cleanup behavior is unchanged.
Database-retention tests guard against deleting volumes or touching baseline
volumes; no database cleanup is authorized by W5.

## Historical screen

Owner Haris. See [the dated review and results](../../docs/archive/evaluation/CP8_EVALUATION_20261030.md).
This screen extends the existing generator and 7B Compose/fairness helpers.
It runs real authenticated replay, not the inspector's comparison-only switch.
It does not change deployed settings, grant contracts or observe-only telemetry.

This dated screen retains its original settings. Later explicit opt-in AIMD
health/retry pilots are separate evidence: see the [submission evaluation and
single-pair figure](../../docs/archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md#subsequent-experimental-retry-pilot-separate-evidence).
The seed60202 retry result is experimental, has adverse routine/SLO outcomes and
does not establish D3 acceptance or alter production defaults.

From the repository root, using its existing Python environment and Docker:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m evaluation.experiments.run_recovery_matrix --mode pilot --output evaluation/results/NEW-pilot
.\.venv\Scripts\python.exe -m evaluation.experiments.run_recovery_matrix --mode pilot --pilot-policy drr_on --output evaluation/results/NEW-drr-pilot
.\.venv\Scripts\python.exe -m evaluation.experiments.run_recovery_matrix --mode matrix --plan-only --output evaluation/results/NEW-plan
.\.venv\Scripts\python.exe -m evaluation.experiments.run_recovery_matrix --mode matrix --workers 2 --output evaluation/results/NEW-matrix
.\.venv\Scripts\python.exe -m evaluation.experiments.analyze_recovery_matrix evaluation/results/NEW-matrix/summary.json --output evaluation/results/NEW-analysis
```

Use new output directories. A pilot checks one 44-event condition and runs the
real PostgreSQL regression tests in isolated schemas within its own disposable
database. Matrix mode has eight conditions x five seeds x seven policies = 280
trials; fresh storage per trial. The plan records exact workloads, IDs, source
SHA-256 fingerprints, execution parameters and randomized policy order before
building/running. `git_head` is optional provenance: a Git checkout records its
owned HEAD, while a downloaded ZIP records `null` and `git_head_status: NOT VERIFIED`.
The source SHA-256 manifest remains available in both cases; a ZIP does not
establish Git revision provenance. `--plan-only` runs no experiment. Source defaults are 44 events,
50% urgent workflows, maximum depth 0/3, 180-s drain bound, .25-s observer polling, 2 live read RPS
and common AIMD Normal/Caution/Recovery ceilings 20/5/2 RPS. These uncalibrated
screening parameters are distinct from the proposal's calibrated acceptance run.

Start with one project. The local completed pilots justified two projects on
12 Docker CPUs / 7.67 GiB Docker RAM; one project sampled about 335 MiB. CLI
supports one, two or four workers with Docker-capacity checks, but host memory,
other workloads and measured resource use must also justify any increase.
The 30 October execution uses two. Original services remain outside every
experiment Compose command.

The first failure stops further policy/case expansion after already active
trials finish their bounded run and cleanup. For a controlled early stop:

```powershell
New-Item -ItemType File evaluation/results/NEW-matrix/STOP
```

This does not interrupt a transaction or remove active data; running trials
finish and clean up before exit. All unexecuted planned trials remain NOT TESTED.
If a process is externally killed, retain its private directory and snapshot
unfinished data before cleaning only resources bearing that exact project's
label. Never issue a global prune or target the original project.

The private backup directory is printed/recorded in `summary.json` under
`raw_backup_directory` and lives in ignored `.venv/cp8-<unique-id>`. It holds
one `raw.json` and command log per trial; the resolved Compose file contains
private credentials and is removed only after successful cleanup. Failed
capture attempts, SQL/local snapshots, original denominators, completion
intervals, grant/audit history, local recovery journals, read outcomes and
sampled resources stay in the ignored raw record. Failed cleanup retains the
config for diagnosis. Raw traces and duplicate figures stay ignored; retain
only compact configuration, final summaries and selected evidence in Git.
Back up this directory separately before deleting a virtual environment; its
recorded path is a local retention location, not an off-machine backup claim.

The standalone switches require explicit experiment opt-in:

| Setting | Default | Supported experimental values |
|---|---|---|
| `CRISISGUARD_EXPERIMENTAL_POLICIES` | unset | `1` to permit nondefaults |
| `REPLAY_INHERITANCE` | shared scheduler setting | optional `0`, `1` experimental override |
| `REPLAY_SELECTION_POLICY` | `priority` | `priority`, `fifo` |
| `CONTROLLER_SCHEDULING_POLICY` | `bounded` | `bounded`, `fifo`, `priority`, `drr` |
| `CONTROLLER_BYPASS_LIMIT` | `1` | nonnegative integer, meaningful for bounded policy |
| `CONTROLLER_DRR_QUANTUM` | `1` | integer 1..1000 |
| `CONTROLLER_DRR_EVENT_COSTS` | `{}` | JSON map from event IDs to integer estimated units 1..1000; absent ID costs one |

Replay resolves these through `edge.scheduler.config.load_scheduler_settings`,
shared with Inspector and policy logs. Without `REPLAY_INHERITANCE`, it follows
`SCHEDULER_ENABLE_INHERITANCE`; the explicit opt-in alias takes precedence.
The runner freezes base inheritance ON and ageing at 600 seconds before Compose
resolution, so ambient host scheduler values cannot change the comparison. The
policy-specific inheritance/FIFO override is applied afterward. Each raw trial
records the effective scheduler settings for both hospitals in
`configuration.scheduler`. Invalid base settings always fail validation.

The entry point injects these only into its resolved private Compose config;
normal Compose defaults are unchanged. DRR state is durable and allocated in
the grant transaction; a failed grant rolls back credit and selection together.
The read-only inspector still offers its own inheritance comparison and is not
used to prove an executed baseline. Normal replay code/tests continue to enforce
candidate replacement, pending-attempt pinning and no blind resend.

Elapsed metrics use one host's monotonic clock. Each PostgreSQL query records
start and end; first visibility is bounded by the previous query start and the
current query end. Startup, capture, Cloud/controller readiness and local ACK
drain are separate fields. Actual gaps include monitoring overhead; .25 s is a
requested polling pause, not guaranteed uncertainty. No database UTC timestamp
is subtracted from a host clock to calculate new recovery durations. The main matrix manually activates replay after Cloud readiness. The separate
automatic pilot below observes a genuine unreachable-to-reachable transition;
independent two-link partitions remain NOT TESTED.

Original IDs and workflow populations never shrink on errors/timeouts. The
analyzer reports unfinished counts and null latency for incomplete populations;
paired effects exclude and count incomplete/missing pairs. Trial/seed is the
statistical unit. Exact sign-flip p-values and paired bootstrap intervals are
separate from observer intervals; five pairs have a minimum two-sided exact
p-value of .0625. The graph shows individual trial points, not confidence bars.
Do not pool conditions to manufacture sample size or significance.

Live reads use separate authenticated provenance, fixed rotating original IDs,
client latency/status records and explicit generator-cap skips. Before an ID
is applied its 404 is retained; this is not an existing-record live SLO workload.
Replay and unclassified middleware counts remain separate. No successful live
write, sustainable 30%/70% capacity, CPU-cost share, calibrated DRR performance,
whole-run resource peak or full D3 acceptance claim is available from this screen.

To repeat the historical **analysis**, not the experiments:

```powershell
New-Item -ItemType Directory evaluation/results/NEW-fixed-analysis
.\.venv\Scripts\python.exe docs/evidence/cp7b-fixed-vs-aimd-analysis-20261029.py docs/evidence/cp7b-fixed-vs-aimd-raw-final-20261029.json --summary-csv evaluation/results/NEW-fixed-analysis/summary.csv --timeseries-csv evaluation/results/NEW-fixed-analysis/timeseries.csv --statistics evaluation/results/NEW-fixed-analysis/statistics.json --figures evaluation/results/NEW-fixed-analysis/figures
New-Item -ItemType Directory evaluation/results/NEW-fairness-analysis
.\.venv\Scripts\python.exe docs/evidence/cp7b-sustained-fairness-analysis-20261029.py docs/evidence/cp7b-sustained-fairness-raw-20261029.json --outdir evaluation/results/NEW-fairness-analysis
New-Item -ItemType Directory evaluation/results/NEW-telemetry-analysis
.\.venv\Scripts\python.exe docs/evidence/cp7b-representative-telemetry-analysis-20261029.py docs/evidence/cp7b-representative-telemetry-raw-20261029.json --supplement docs/evidence/cp7b-representative-telemetry-diagnostics-20261029.json --supplement docs/evidence/cp7b-representative-telemetry-diagnostic-recovery-20261029.json --outdir evaluation/results/NEW-telemetry-analysis
```

The review self-check expects the dated recalculations in `cp8-prior-fixed`,
`cp8-prior-fairness` and `cp8-prior-telemetry`, compares them with retained
statistics and checks the actual five-file backup:

```powershell
.\.venv\Scripts\python.exe -m docs.evidence.review_cp8_evidence --backup 'C:\Users\muhdh\Downloads\CrisisGuard-inheritance-matrix01-backup.zip' --output evaluation/results/NEW-review.json
```

The historical raw inheritance traces remain missing. Never regenerate new
timings and label them `haris-matrix01` evidence.

At 44 events the balanced maximum-depth-3 case has ten depth-3 workflows and
two truncated depth-1 workflows; the skewed case has eleven full depth-3 chains.
Keep the actual manifest depth counts visible. A larger strict-depth experiment
should use a total divisible by 88 (for example 440), and remains distinct from
this bounded screen.

## Automatic recovery, constant-rate check and cost sensitivity

```powershell
.\.venv\Scripts\python.exe -m evaluation.experiments.run_recovery_matrix --mode pilot --activation automatic --output evaluation/results/NEW-auto
.\.venv\Scripts\python.exe -m evaluation.experiments.run_recovery_matrix --mode pilot --admission-mode constant --output evaluation/results/NEW-constant
.\.venv\Scripts\python.exe -m evaluation.experiments.calibrate_event_costs --image crisisguard-cp8:8369ec74324e --output evaluation/results/NEW-cost-calibration
.\.venv\Scripts\python.exe -m evaluation.experiments.run_recovery_matrix --mode cost-stress --cost-profile evaluation/results/NEW-cost-calibration/cost_profile.json --workers 2 --output evaluation/results/NEW-cost-stress
.\.venv\Scripts\python.exe -m evaluation.experiments.analyze_recovery_matrix evaluation/results/NEW-cost-stress/summary.json --output evaluation/results/NEW-cost-analysis
```

The retained image tag above exists on the execution machine and its 26 Edge/Cloud
Python files were verified against the archived main-study source. Elsewhere,
build the Dockerfile from the matching archived source and substitute its tag;
do not assume that an identically named image has identical contents.

New pilots use seed 9191. `--activation automatic` first captures all 44 events,
warms Cloud/controller, then stops only its three disposable Cloud services.
It enables the Hospitals' real automatic loop and verifies both have observed
all services unreachable and zero Cloud applications before starting the clock.
At T0 it restores Cloud while observing reconnection and original-ID application
on one monotonic clock. Hospital restart, outage preparation and Cloud restoration
readiness are separate timings. The application defines reachable as **any**
healthy service: the retained service-health map distinguishes that from all
services ready. Controller freshness/admission still gates replay. This checks a
shared Cloud outage, not two independent network partitions. Disabled manual
replay must return the application's existing 404 contract.

`--admission-mode constant` selects the existing fixed engine with all three
state rates equal to the ceiling, retaining freshness denial and state-dependent
in-flight caps. `state-fixed` uses 20/5/2 RPS by default. `aimd` is the default.
Issue and ACK phases have separate cooldowns; neither the configured RPS nor
the allowed-reservation counter is itself unique applied-event throughput.
The single constant pilot is a functional check, not a calibrated comparison.
The main matrix stays Normal at its ceiling, where adding identical constant
trials would not resolve the missing calibrated-load admission question.

Cost calibration runs three sequential fresh Cloud/controller databases with
seeds 9191/9192/9193. Each verifies 20 warmup, 80 measured normal and five measured
expensive events (105 per replica) through a direct authenticated replay-grant
client. It does not use Hospital capture or count requests as independent trial
replicas. Admission pacing is disabled only for these serial calibration
projects; Cloud grant enforcement, content digests and Hospital authentication
remain enabled. Validated changes in rounded middleware mean-times and exact
sample populations give per-type service wall-time estimates, with explicit
rounding intervals and window-expiry/contamination rejection. They include DB
and grant work and are not CPU cost or end-to-end recovery latency.

The expensive-root database delay is nine times the separate normal-root mean,
applied by a trigger only in each disposable database. The measured heavy cost
and median normal cost define integer units and quantum 10. The profile is
written only after all three replicas pass. It validates all four types,
finite bounded delay and bounded integer estimates before resource creation.

Cost-stress mode uses one 10:1, maximum-depth-3, balanced-urgency condition with
five paired seeds and seven randomized policies: `rr_on` (B=0), `bounded_on`
(B=1), `bounded_b2`, `bounded_b4`, `drr_on`, `drr_under`, `drr_over`. Five of 44
prerequisite roots are expensive, selected before execution; under/over factors
are 0.5/2 on their estimates. The nominal profile remains the reference cost
map when comparing shares. All policies keep the same AIMD admission settings.
This is 35 trials in one condition, not the full 16-condition D2/D3 acceptance
matrix, a sustained CPU fairness benchmark or an elapsed waiting guarantee.

Analyzer outputs separate all allocations, completed grants and superseded
unsent grants. Cost shares weight those populations using the frozen reference
service-wall estimates, and separately show scheduler-estimated allocation
cost. Superseded allocations consume credit but do not imply Cloud service.
The finite-prefix measures use the first sampled half-global-drain observation:
each site's completed/original event fraction, Jain's index of those fractions,
and completed/original reference-cost fraction. Final completion fractions are
trivially one when the backlog drains. Sampled ticket witnesses test allocation
overtaking conditional on retained eligibility; they are not continuous coverage
or time bounds. DRR credit is discarded when a site withdraws eligibility.

Global drain, live traffic and project resource fields repeat in each site's
CSV row. Use one site row per trial for totals, or the retained per-trial CSV;
never add both sites' copies. Workflow p95 is nearest-rank over that site's
original complete workflows (often only a few); empty groups stay null.
Five-seed paired effects, bootstrap intervals, exact sign-flip p-values and
observer intervals are retained separately; no multiplicity correction or
preregistered acceptance decision is claimed.

The older main matrix driver retained one skip without enough timing to identify
cap versus driver lag and did not retain an exact generator stop clock. Its
operational offered count and ideal offer bounds are labelled accordingly.
Follow-ups record the stop clock, exact event/service sequence, cap skips and
driver-lag skips separately. Neither missing opportunities nor transport errors
are silently counted as server rejections. Completed server middleware samples
are reconciled to client HTTP responses separately from replay/unclassified.

See `docs/evidence/cp8-backup-20261030.json` for the separately retained local ZIP,
its verified SHA-256 and raw directories. Private credentials, Compose configs,
command logs and database binaries are excluded. Off-machine recovery has not
been verified. The ZIP also preserves the main run's exact frozen source, the
final follow-up source and adverse trials; source changes between phases must
not be mistaken for a freshly reproduced historical run.
## Exploratory calibrated existing-record read load

`calibrate_live_reads.py` provides a **read-only** preparatory calibration for
30%/70% load targets. It is separate from the historical trial matrix and does
not implement a live-write role or establish proposal D2/D3 acceptance.

Use an isolated local Cloud deployment, first seed a **synthetic applied** event,
and set `CLOUD_LIVE_TELEMETRY_TOKEN` in the shell to match the deployed Cloud
service. The tool never prints or writes the token. For example, using a known
Patient API event on port `18101`:

```powershell
.\.venv311\Scripts\python.exe -m pytest -q tests/unit/test_live_read_calibration.py
.\.venv311\Scripts\python.exe -m evaluation.experiments.calibrate_live_reads `
  --url http://127.0.0.1:18101/events/REPLACE_WITH_APPLIED_SYNTHETIC_ID `
  --rates 2,5,10,20 --duration 30 --workers 8 --slo-ms 250 `
  --output evaluation/results/live-read-calibration-unique.json
```

The listed rates and 250 ms SLO are **illustrative**, not calibrated or
proposal-approved values. Confirm the actual Cloud SLO and test envelope with
Wayne and Haris before execution. The tool checks that the record already
returns HTTP 200, offers asynchronous requests at fixed intervals, records
delivered 2xx, non-2xx, transport failures, start lag and generator skips, and
selects the highest tested rate with >=99% successful delivery, <=1% failures,
**zero generator-cap skips**, and client p95 within the configured SLO. Its
30%/70% values are targets based on that **tested passing
read rate**, not measurements of saturation capacity. Multiple independent
calibration runs and correct cross-policy comparisons remain necessary.

## Reduced H1/H2 admission reproduction

The submitted reduced study used one balanced, maximum-depth3 88-event graph
per seed 3104–3108, bounded scheduling with inheritance, constant and AIMD
admission, and 36/84 authenticated existing-record reads/s. The 120-RPS
read-only reference is provisional. One compatible AIMD arm was reused across
sessions, and one invalid constant arm was replaced after a stop-boundary
issuance fix. The [final assessment](../../docs/archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md)
records that historical selection and its missing ignored raw-summary paths.
The commands below start a **new** 20-arm study; they do not recreate the old
measurements or silently substitute for the unavailable historical summaries.
Run only in an approved isolated Docker environment with a new output path.

```powershell
$py = '.\.venv311\Scripts\python.exe'
$root = 'evaluation/results/h1-h2-new-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
$common = @('--mode','admission','--capacity-rps','120','--total-events','88',
  '--activation','automatic','--live-read-target','preseed','--live-workers','128',
  '--live-slo-ms','250','--admission-ceiling','20','--timeout','300','--workers','1',
  '--cost-profile','docs/evidence/cp8-cost-profile-20261030.json')
& $py -m evaluation.experiments.run_recovery_matrix @common --plan-only --output "$root/plan"
if ($LASTEXITCODE -ne 0) { throw 'Plan failed; do not run trials' }
foreach ($condition in @('ratio1-1_depth3_balanced_load30','ratio1-1_depth3_balanced_load70')) {
  foreach ($seed in 3104..3108) {
    $out = "$root/$condition-seed$seed"
    & $py -m evaluation.experiments.run_recovery_matrix @common --acceptance-condition $condition --acceptance-seed $seed --output $out
    if ($LASTEXITCODE -ne 0) { throw "Trial block failed: $out; preserve partial evidence" }
  }
}
$summaries = @(Get-ChildItem $root -Filter summary.json -Recurse | Select-Object -ExpandProperty FullName)
& $py -m evaluation.experiments.assess_acceptance @summaries --policy bounded_on --max-generator-skip-fraction 0.001 --output "$root/paired-assessment.json"
if ($LASTEXITCODE -ne 0) { throw 'Assessment failed; retain all original outputs' }
```

Inspect every trial-validity result before using the combined assessment. The
historical primary validity rule required at least
99.9% of scheduled opportunities to become issued attempts, exact opportunity
ledger reconciliation, and functional/source/container-preservation checks.
Report zero-skip and 99.99% issuance sensitivities separately. Non-200,
transport failure and late response are outcomes of issued attempts, not
generator misses or successful reads. The original 70% load result retains
only three pairs under the stricter rules, so its five-pair primary conclusion
depends on the disclosed protocol amendment.
