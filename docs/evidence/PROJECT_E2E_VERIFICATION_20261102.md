# CrisisGuard isolated functional E2E verification — 2 November 2026

The fresh eight-event smoke and all 12 controlled-continuation checks passed on
Windows with Python 3.11.9, Node 24.22.1 and local Docker Desktop Linux/WSL2.
The continuation checks include one configuration step and 11 functional checks;
they are not 12 distinct fault scenarios. Only synthetic data was used. Source
base: `a631e6ea8ab80d549e09b7b80fcf16af816d09e6`, with the new test framework
present in the working tree. Production source hashes were unchanged.

Session: `e2e-c0bb4282cba64ab6ab54b9c505325df4`. Disposable project:
`cg-e2e-c0bb4282cba64ab6ab54b9c5` (eight services plus the test-only proxy,
two internal networks and three private volumes). The stack remains healthy and
retained for review; cleanup was deliberately **SKIP**. No AWS was accessed.

| Executed check | Result | Supporting observation |
|---|---|---|
| Fresh smoke | PASS | Eight events, four types per hospital; exact SQLite/SQL/Cloud/Dashboard content, grants and duplicate replay |
| Enable automatic protected replay | PASS | Only the two owned hospital containers reconfigured; pinned images and SQLite volumes retained |
| Hospital A partition | PASS | Four-event chain durable locally and absent from SQL while offline; Hospital B progressed; automatic ordered recovery |
| Hospital B partition | PASS | Mirrored durability, Hospital A progress and automatic ordered recovery |
| Both hospitals partitioned | PASS | Eight pending events absent from Cloud; both chains applied after reconnection |
| Cloud APIs stopped/restored | PASS | Both captures remained pending; Dashboard surfaced outage; automatic protected replay after recovery |
| Controller stopped/restored | PASS | Pending captures did not bypass grants; Dashboard surfaced controller unavailability; recovery completed |
| Hospital A restart | PASS | Pending SQLite event unchanged across restart while disconnected; automatically applied after reconnect |
| Hospital B restart | PASS | Same persistence and automatic recovery check on its distinct SQLite store |
| Successful grant ACK response dropped | PASS | Actual proxy TCP response loss exercised; reconciliation recovered with one Cloud POST and one application |
| Successful Cloud commit response dropped | PASS | Committed event reconciled after lost response; one Cloud POST and one application |
| Repeated protected replay | PASS | Exact 34 event IDs, statuses and application timestamps unchanged |
| Dashboard consistency | PASS | History exactly matched SQL; queue IDs/statuses matched hospitals; zero backlogs and matching controller state |

Final ledger: **34 captured/applied events**, **34 completed grants**, one terminal
expired grant from ACK-loss recovery, and **zero unresolved events/grants**. The
34 include the initial eight smoke events. Dependency/application order and the
acknowledged-to-completed audit transitions were checked for recovered events.
The scoped driver also checked that no foreign container consumed disposable
volumes before disruptive operations.

All **63 recorded preservation/ownership gates passed**. The original **72
containers, 32 volumes and 13 live-baseline networks** remained unchanged,
including `crisisguard-local`. Direct daemon identity and boot/PID/start-time
epoch matched the baseline. Final read-only review found all nine owned services
healthy, expected network attachments restored, proxy controls cleared, and no
active injected faults. Historical evidence and the pre-continuation snapshot
were unchanged. Generated-credential scanning passed for 160 public artifacts.

## Historical preservation failure remains a failure

The earlier session `e2e-21baf415c7124dd58b929d8783f68a7c` sampled cached Docker
Desktop inventory before engine startup. Docker's built-in bridge ID changed
during wake-up; the existing `crisisguard-local_default` network did not change.
That failure was investigated and **never relabeled as PASS or rebased**.
After separate reviewed ownership checks, the approved cleanup procedure removed
only its nine containers, two networks and three disposable volumes. Its evidence
and failure records were retained byte-for-byte. Normal runner resume still
rejects its legacy baseline. The corrected fresh session uses schema-2 live
snapshots and fail-closed restart/ownership/preservation guards.

## Evidence provenance and limits

Reviewed local inputs, retained under ignored `.project-test-results/`:

- Fresh session: `SUMMARY.md`, `report.json`, `isolation.json` and sanitized logs.
- `controlled-c0bb4282-20261102/`: `results.json`, `final-review.json`, 63-entry
  `guard-checks.jsonl`, per-scenario snapshots/logs, the scoped driver and the
  immutable pre-continuation snapshot.
- `network-investigation-20261102/` and `legacy-close-20261102/`: original network
  investigation, database observations and separately reviewed cleanup receipt.

The retained continuation used a local scoped driver through the guarded Stack
adapter, with cleanup forbidden. The standard confirmed CLI continuation runs
the broader suite and then owned cleanup; there is no retain-after-faults flag.
The complete raw sessions contain private generated configuration and are not
published. This concise summary does not claim an independently available raw
archive or a clean-machine installation certification.

**NOT VERIFIED:** exact original PostgreSQL content equality against its initial
pre-run state (no original logical/content baseline), full original PostgreSQL
integrity, real browser/accessibility/visual behavior, complete per-event Cloud
access-log correlation, exhaustive API fault coverage, live execution of the
broader admission/policy/quarantine/SIGKILL plans, sustained fairness/capacity,
proposal acceptance, urgent p95 improvement against RR, and full D2. Earlier
AIMD performance/regression limitations and owner-sign-off requirements remain.
Historical hospital SQLite integrity checks passed; they do not establish the
missing PostgreSQL guarantees. Metadata preservation is not content equality.

## Final safe automated validation

`.\test.project.ps1 -Full` passed **1,359 Python cases and 86 JavaScript cases**
(including one legacy queue file-level test). API/security subsets overlap the
Python total. Python coverage generation and the current audit passed; complete
JavaScript coverage and proposal acceptance remain NOT VERIFIED. PostgreSQL
integration and Docker health were SKIP because this final run deliberately
performed no Docker operations. The local run is
`.project-test-results/20261102T145907Z-3702f94b`.

Final results and source/evidence hashes are recorded in the companion
[`PROJECT_FINAL_VALIDATION_20261102.json`](PROJECT_FINAL_VALIDATION_20261102.json).
These tests did not rerun Docker faults or mutate the retained stack. CLI help
was executed; setup/start/stop/cleanup instructions were checked against their
implementations without executing those resource-changing commands. CI for the
eventual pushed commit is a separate verification requirement.
