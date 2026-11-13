# Checkpoint 7B: representative telemetry calibration

29 October 2026. This study measures the existing live-only p95 and executor-pressure producers in disposable Docker projects. It does not alter MetaGuard logic, application telemetry definitions or production settings.

## Method and boundaries

There are three fresh-project replicas per phase on the measured 16-CPU, approximately 31-GiB Docker host. Each uses real Patient, Emergency, Admin, controller, Hospital A/B and PostgreSQL services, plus a test-only upstream-delay proxy. The first group completed all three representative traffic phases before a Windows async-driver incompatibility interrupted the first diagnostic. Two projects in the second group recreated the 24-event dataset through real hospital replay and completed the diagnostics; its third project hit the preparation replay deadline before diagnostics began. One replacement project completed only that replica's diagnostics, with the same 24 logical read fixtures inserted directly into its fresh database. That fixture preparation makes no admission or replay claim.

Completed traffic phases and successful diagnostic replicas were not repeated. Failed raw results are retained. Analysis selects only validated complete phases and records their source project/file in an evidence index. This yields 18 completed phase-replicas across seven disposable projects, with three replicas for each phase.

All application Python source hashes are checked against the built image and match across the runs. Controllers use the unchanged source-default AIMD settings, including 5-second polling and 2-second metrics timeout; all real Cloud writes retain grant enforcement. Diagnostics start in fresh projects, so controller state history is not treated as one continuous trajectory across all six phases. Some diagnostic replicas overlap while the replacement later runs alone; host contention and fixture preparation are documented sources of variability, not independent-host replication.

| Phase | Duration | Traffic / bound | Purpose |
|---|---:|---|---|
| Live low | 75 s | 6 live reads/s/service; concurrency cap 4/service | Sustained baseline |
| Live higher | 75 s | 24 live reads/s/service; cap 16/service | Higher sustained load |
| Mixed | 75 s of generated reads, then replay drain | 12 live + 6 replay reads/s/service; cap 16/service; 16 real Hospital A/B replay writes/project across workload/drain | Classification and realistic read/write coexistence |
| DB wait, low concurrency | 45 s | 2 live reads/s/service; cap 4/service | Real database waiting without deliberately filling 40 workers |
| Executor waiting | 45 s | Patient target 50 reads/s, cap 64; others 2 reads/s, cap 4 | Bounded pressure at the unchanged 40-worker limiter |
| Upstream delay | 45 s | 6 live reads/s/service; cap 8/service; proxy adds 300 ms before Cloud entry | Demonstrate an excluded latency boundary |

The last three phases are explicitly synthetic diagnostics. Both DB diagnostics hold an access-exclusive lock on the disposable `events` table for two seconds every five seconds. The lock session has a four-second idle-transaction timeout to release it if the harness stops. No original database is mounted, queried or locked. A 61-second idle interval between phases verifies natural expiry and executor drain; monitoring and health endpoints remain excluded from latency populations.

Live requests are authenticated existing-event status GETs. Replay requests include authenticated GETs and actual Hospital automatic replay POSTs with grants. The current provenance contract classifies a purported live request carrying a replay grant as unclassified, while the Dashboard direct-submit guard blocks live ingestion. This study preserves that security boundary and does not claim to calibrate an unavailable authenticated live-write path.

`live_p95_latency_ms` is the exact nearest-rank percentile over authenticated live completions in a process-local rolling 60-second window. Middleware duration begins after request entry and ends at response-header availability. Client duration covers the full HTTP request/response, including the proxy when used. Analysis uses the last actual periodic middleware snapshot at or before the final generated client completion, then selects client completions approximately aligned by UTC with that window. This avoids comparing an expired post-replay-drain window with earlier client traffic. Populations can differ slightly at boundaries, and mixed middleware mean/throughput include replay writes whose client timings are not part of the GET generator.

Executor queue depth counts tasks waiting for the actual shared AnyIO thread limiter; capacity remains 40. Pressure is waiting divided by capacity. It is an instantaneous process-local observation, not aggregate Cloud backlog. Metrics are sampled every 0.5 seconds, or 0.25 seconds during DB diagnostics, with one outstanding sampler per service. Fetch latency and source-sample gaps are recorded because the metrics handler's own database probe can wait after the queue snapshot is taken.

Offered rates are generator targets, not measured capacity. Arrivals that encounter a full client concurrency cap are skipped and counted separately, rather than queued indefinitely. Throughput is the producer's completed-request window density, not the offered rate; the 45-second diagnostics start with empty windows and therefore include idle time in their 60-second throughput denominator.

## Measured results

All values in the next table are **Patient means across three trials**. The p95 columns use the approximately aligned, selected 60-second windows; the middleware mean mixes all classifications, although the pure-live phases contain only live requests. The throughput denominator is always the producer's 60-second observation window. The raw per-service/trial rows and between-trial standard deviations are in the linked CSV and statistics JSON.

| Phase | Middleware live p95 (ms) | Client live GET p95 (ms) | Middleware mean (ms) | Throughput (requests/s) | Live / replay samples | Peak executor wait / 40 | Skipped offered arrivals |
|---|---:|---:|---:|---:|---:|---:|---:|
| Live 6/s | 9.18 | 22.62 | 7.61 | 5.99 | 360 / 0 | 0 | 0 |
| Live 24/s | 9.23 | 26.17 | 7.25 | 23.99 | 1,440 / 0 | 0 | 0 |
| Mixed live/replay | 9.15 | 23.64 | 7.58 | 18.03 | 720 / 362 | 0 | 0 |
| DB lock, low concurrency | 2,000.17 | 2,011.52 | 520.90 | 1.39 | 83 / 0 | 0 | 6 |
| DB lock, concurrency cap 64 | 1,871.56 | 2,438.51 | 440.29 | 28.01 | 1,681 / 0 | 16.7 (pressure 0.42) | 563 |
| Upstream 300 ms delay | 7.70 | 318.11 | 6.76 | 4.47 | 268 / 0 | 0 | 0 |

Rounded sample counts and skipped opportunities are trial means, not single observations. For Patient in the bounded-concurrency phase, peak sampled waiting was **8, 16 and 26** tasks, with corresponding peak queue/capacity pressure **0.20, 0.40 and 0.65**. Active workers reached the unchanged limit of **40** in all three trials. Client live-window p95 varied from **1,877.03 to 2,742.87 ms** in this phase, despite middleware live p95 staying between **1,854.60 and 1,887.14 ms**. The largest measured Patient metrics fetch took **2,490.33 ms**, and the largest source-sample gap was **2.52 s**; instantaneous queue samples can consequently miss peaks. These are observed maxima, not calibrated overload cutoffs.

All three Cloud services showed the same qualitative boundaries. At 24 live GET/s/service, their middleware live p95 means were **9.19–9.43 ms**, their client live GET p95 means **22.58–26.17 ms**, and their reported throughput approximately **24 requests/s**. In the mixed phase, selected-window live/replay sample means were approximately **720/361–362 per service**, with zero unclassified samples. The 16 real hospital replay writes in each of the three representative projects contributed **48 additional replay requests** beyond generated GETs, and all admitted events were preserved. Under low-concurrency DB locks, Emergency and Admin live p95 means were **2,000.04 and 1,999.74 ms**, with no observed executor waiting. Under upstream delay, all three middleware live p95 means were **7.37–7.81 ms** while client GET p95 means were **317.88–318.11 ms**.

Across the 18 completed phase-replicas, **41,144** generated client requests yielded **zero transport errors and zero non-200 responses**; **8,737** metrics observations were retained. The **1,786** skipped offered arrivals occurred when the generator hit its stated concurrency caps, chiefly during the bounded diagnostic; they are not HTTP errors or evidence of a measured server rejection rate. Every completed phase's 61-second expiry check found zero live samples, null live p95 and zero executor queue. The two replay-prepared diagnostic projects each preserved 24 exact PostgreSQL events, dependency order and 24 completed grants with no tickets; the replacement read-only diagnostic used the same 24 logical fixtures and issued no grants.

The unchanged controller moved **Normal to Caution** during the 24/s phase even though live p95 remained near 9 ms: its existing mixed throughput threshold is 20 requests/s. It also moved to Caution during the DB-lock diagnostics, when its existing mean-latency signal rose. Its observed rate across these experiments ranged from **0.2 to 2.0 grants/s**; no controller observation reported stale telemetry. These transitions reflect the **legacy** inputs, not adoption or validation of the new signals. Diagnostic groups began in fresh projects, so their state/rate traces are not a continuation of the earlier representative group. The upstream-delay phase left both producer signals low while client latency was high; its Caution-to-Normal trace came from the prior DB diagnostic in that project, not from detection of upstream delay.

## What the signals detect

| Condition tested | Live-only p95 | Executor waiting/pressure | Boundary exposed |
|---|---|---|---|
| Sustained authenticated live reads and mixed replay | Stable, with live population isolated from replay | Zero at tested rates | Confirms classification, population and expiry under this read-heavy mix; does not establish a capacity knee. |
| Database table-lock wait, client cap 4 | Rose to approximately 2 seconds | Zero | Live p95 captures completed handler/database delay; the executor gauge does not measure database lock queues directly. |
| Same locks, Patient client cap 64 | Rose to approximately 1.87 seconds | 40 workers occupied and 8–26 waiting at sampled peaks | The shared executor gauge detects its own saturation, but delayed metrics delivery and completed-request p95 can lag work still waiting. |
| 300 ms delay before Cloud entry | Stayed below 9 ms | Zero | Neither producer signal sees this upstream delay; client p95 was approximately 318 ms. |

Server/socket accept queues, upstream callers and network waits remain outside both producer measurements. Database waiting is visible indirectly through completed live latency and worker occupancy when enough blocking calls consume the executor; there is no separate database queue-depth metric. All values are local to one process and do not establish aggregate Cloud backlog. A low live sample count or absent live-write producer would make p95 less representative of admission demand; a transiently blocked metrics endpoint could make even a correct sample unavailable to the controller's two-second fetch timeout.

**Decision:** keep both signals observe-only. The experiments justify proposing them as *candidate supplementary* admission inputs for a controlled design review, especially live p95 for in-handler latency and executor waiting for actual worker contention. They do **not** justify wiring them into MetaGuard or choosing numeric production thresholds. Before that decision, a workload with authenticated live writes and realistic service/DB mix must establish producer coverage and a measured service objective; multiple replicas, server/upstream visibility, low-sample and stale-data handling, polling/fetch-delay behavior, and policy sensitivity need validation. The source-default **150/250/1,000 ms mean-latency thresholds** are for another field and cannot be transplanted to live p95. No performance superiority or sustainable capacity claim follows from this experiment.

## Figures, regressions and preservation

- [Latency boundaries](../evidence/figures/cp7b-representative-telemetry-latency-boundaries-20261029.png): means and between-trial standard deviations for middleware live p95, client live GET p95 and mixed middleware mean. The vertical split separates sustained traffic from deliberate diagnostic perturbations; logarithmic milliseconds make both normal and lock-delay regimes visible. These are different timing boundaries and populations.
- [Executor waiting](../evidence/figures/cp7b-representative-telemetry-executor-waiting-20261029.png): actual Patient queue and worker snapshots against elapsed phase time; grey intervals are measured disposable-database lock holds. The top row has database delay without executor waiting, while the bottom row reaches the 40-worker capacity and records waiting tasks. Points are sampled, not a continuous queue trace.
- [Mixed request populations](../evidence/figures/cp7b-representative-telemetry-mixed-populations-20261029.png): selected middleware-window completed live and replay counts by service and trial. Replay includes hospital writes during the selected window; the two bars have different provenance roles and must not be combined to compute live p95.

Focused regression: **130 passed, 0 failures, 0 errors, 0 skips** across the live/legacy metrics and admission unit tests plus Cloud API/telemetry integration cases, using a disposable database. No full 821-case suite was repeated. The first raw run completed all representative phases but its diagnostic start failed because psycopg async I/O cannot use the Windows Proactor loop; the runner now selects a compatible loop. A later diagnostic project's replay preparation exceeded its bounded deadline before any diagnostic phase, so only that unfinished replica was replaced. Both failed attempts and their successful cleanup are preserved in raw evidence. The intermediate global inventory comparison recorded another authorized disposable project during overlap; the terminal audit after all cleanup confirmed the **same 33 original container identities/images/mounts and 16 volume identities**, unchanged application source hashes, and no remaining telemetry-project containers or volumes. A separate final read-only Docker inventory also counted 33 containers and 16 volumes with no telemetry-project containers. The original databases and credentials were not mounted or changed; only fresh disposable databases were populated. Uniquely tagged test images were retained.

The existing 7B calibration/adoption TODOs remain open. This is a bounded, read-heavy calibration study with three replicas, not verified production live-write coverage or a production threshold calibration. The remaining work in those TODOs is to agree on live workload/service objectives, complete real caller coverage and multi-replica representative validation, then review signal adoption and thresholds. AWS state transitions and Wayne's review are separate open 7B items.

## Evidence and reproduction

- [Raw requests, snapshots, controller observations and integrity checks](../evidence/cp7b-representative-telemetry-raw-20261029.json)
- [Completed diagnostic supplement and final integrity checks](../evidence/cp7b-representative-telemetry-diagnostics-20261029.json)
- [Replacement diagnostic replica and terminal preservation audit](../evidence/cp7b-representative-telemetry-diagnostic-recovery-20261029.json)
- [Evidence index with source SHA-256 hashes and phase lineage](../evidence/cp7b-representative-telemetry-evidence-index-20261029.json)
- [Per-trial/service phase summary](../evidence/cp7b-representative-telemetry-summary-20261029.csv)
- [Client request measurements](../evidence/cp7b-representative-telemetry-requests-20261029.csv)
- [Middleware/executor observations](../evidence/cp7b-representative-telemetry-metrics-20261029.csv)
- [Trial variability statistics](../evidence/cp7b-representative-telemetry-statistics-20261029.json)
- [Experiment runner](../evidence/cp7b-representative-telemetry-experiment-20261029.py)
- [Analysis and graph generator](../evidence/cp7b-representative-telemetry-analysis-20261029.py)
- [Test-only delay proxy](../evidence/cp7b-representative-telemetry-proxy-20261029.py)
- [Focused regression JUnit](../evidence/cp7b-representative-telemetry-focused-tests-20261029.xml)

The runner imports isolation helpers from the prior fixed/AIMD and sustained-fairness evidence scripts, and uses the preceding calibration's request-summary function. It does not call that script's original-stack driver or read its private credentials.

```powershell
python docs/evidence/cp7b-representative-telemetry-experiment-20261029.py --output docs/evidence/NEW-telemetry-raw.json
python docs/evidence/cp7b-representative-telemetry-analysis-20261029.py docs/evidence/NEW-telemetry-raw.json --outdir docs/evidence
```

For the preserved measurements from this interrupted/supplemental execution, use:

```powershell
python docs/evidence/cp7b-representative-telemetry-analysis-20261029.py docs/evidence/cp7b-representative-telemetry-raw-20261029.json --supplement docs/evidence/cp7b-representative-telemetry-diagnostics-20261029.json --supplement docs/evidence/cp7b-representative-telemetry-diagnostic-recovery-20261029.json --outdir docs/evidence
```

The corrected runner uses a per-thread selector event loop on Windows, as required by psycopg async I/O. A future complete run needs no supplement. `--diagnostics-only` resumes unfinished phases; `--fixture-seed --trial-ids 2` avoids repeating admission/replay preparation for the replacement read-only diagnostic replica. Original-result flags, including the intermediate global-inventory mismatch while another authorized disposable project overlapped, remain unchanged in the raw evidence. The terminal audit checks exact equality against the original 33-container/16-volume baseline.

Use a new output directory for analysis to retain earlier generated CSVs/figures. The runner refuses to overwrite raw evidence. Dependencies are the existing Docker/Python/httpx/psycopg environment and matplotlib/numpy for analysis.
