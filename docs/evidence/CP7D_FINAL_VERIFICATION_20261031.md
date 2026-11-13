# Checkpoint 7D: Windows Fresh-Copy and Docker Integration Verification

**Date:** 31 October 2026 (Singapore time)  
**Operator:** Ignatius  
**Scope:** Manual verification on Windows using a newly extracted repository and isolated Docker Compose project `cp7d-fresh-test`. Results below were reported from PowerShell command outputs. This report is not an automatically collected full-system trace.

## Environment and isolation

- Fresh extraction: `CrisisGuard-7D-Test/CrisisGuard-main` on Windows; Python 3.11 virtual environment and requirements installed.
- Original eight-service deployment: `crisisguard-local`, preserved separately. Its PostgreSQL instance was restored after an unrelated Docker interruption and showed **191** events with all eight original services healthy.
- Isolated project: `cp7d-fresh-test`, separate named volumes and published ports `18000`, `18001`, `18002`, `18003`, `18004`, `18005`, `18080` and `15433`.
- Initial isolated hospital configuration used `AUTO_REPLAY=0`; controlled hospital-only recreation later set `AUTO_REPLAY=1` while retaining SQLite volumes.
- Reused application image tags were rebuilt for the isolated startup; original running containers were not recreated by these commands. Image-tag isolation was not established.

## Observed results

| Check | Observation | Result |
|---|---|---|
| Fresh configuration | Missing `.env`, Python 3.11 environment and dependency imports verified. Initial initializer rejected populated token fields in `.env.example`; after local example correction it created private `.env` successfully. | **Initial first-run failure; corrected rerun passed** |
| Isolated Compose | Protected MetaGuard overlay rendered with `config --quiet`; eight containers started healthy. | Pass |
| HTTP readiness | Hospital A/B roots and Patient, Emergency, Admin, Controller and Dashboard health endpoints each returned HTTP 200. | 7/7 pass |
| Fresh Cloud ledger | PostgreSQL `events` count was 0 before synthetic captures. | Pass |
| Offline capture | `CP7D_A_082907d4` (Hospital A, priority 5) and `CP7D_B_082907d4` (Hospital B, priority 2) returned `pending`; Cloud count stayed 0 with replay disabled. | Pass |
| Restart persistence | Both hospital containers restarted; both events remained `pending` in SQLite and Cloud remained empty. | Pass |
| Protected replay | After enabling automatic replay for both test hospitals, both events became `applied` and were present once each in Cloud PostgreSQL. | Pass for these two events |
| Capture duplicate | Repeating Hospital A capture returned HTTP 409. | Pass |
| Hospital site isolation | Hospital A capture claiming Hospital B returned HTTP 403. | Pass |
| Manual replay disabled | Hospital A `POST /replay` returned HTTP 404. | Pass |
| Dependency-aware scheduling | `CP7D_DEPENDENT_7019925f` (priority 5) was ineligible while waiting on `CP7D_PREREQ_7019925f` (priority 1). Inspector selected prerequisite and showed inherited effective priority 5. | Pass |
| Dependency execution | PostgreSQL recorded prerequisite applied at `02:46:54.121217+00` and dependent at `02:46:59.385141+00`, both on 31 October. | Pass for observed pair |
| Direct Cloud admission | Unauthenticated Patient `POST /events` returned HTTP 401; PostgreSQL count remained 4 before and after. | Pass |
| Focused regression | MetaGuard security, manual replay authorization and scheduler replay-policy tests: 51 passed, one deprecation warning. | Pass |
| Broad local regression | Unit suite plus mocked Cloud APIs and manual-replay security: **959 passed**, zero failures, one Starlette/httpx deprecation warning, 13.95 seconds. | Pass |

The 51-case focused suite overlaps the broader regression scope; **do not add 51 and 959 as unique tests**. The broad run's machine-readable record is [`cp7d-final-local-regression-20261031.xml`](cp7d-final-local-regression-20261031.xml).

## Limits and remaining acceptance

- This was an isolated fresh-copy setup on a Windows machine that already had Docker. The **unmodified** `start.project.bat` was not run end to end, and the installation was not performed on a genuinely clean Windows machine. Task 1 remains open.
- The source `.env.example` in the supplied ZIP blocked first-time initialization until corrected in the disposable test copy. No private token values are recorded here.
- The paired capture/recovery tests prove functional delivery under their tested conditions, **not** sustained fairness, large-scale performance, exhaustive exactly-once concurrency or complete MetaGuard state-transition coverage.
- No intentional Cloud outage, controller restart/reconciliation, sustained-load comparison or AWS fault-transition test was performed during this 7D session. Earlier dated evidence must be cited separately if relevant.
- Security checks were selected cases, not a penetration test or complete production security audit. API contract source/local-fixture review has separate dated evidence under `CP7D_CLOUD_API_CONTRACT_20261031.md`.
- GitHub Actions **has not yet been checked on the final merged commit**. Final team review and release evidence closeout remain pending.
- Manual live observations above came from operator terminal output. For final report reproducibility, retain console transcripts or rerun with a dedicated, sanitized evidence capture if required; do not fabricate missing machine-generated artifacts.

## Next actions

1. Confirm documented first-installation expectations and resolve the initializer example mismatch before declaring Task 1 complete.
2. Review final security/API contract coverage and independently recorded Docker reliability tests; do not silently upgrade partial coverage to full acceptance.
3. Transfer this report and the JUnit XML to the original Git repository, review diffs and commit with teammates' coordination.
4. Verify all GitHub Actions jobs on the actual final merged commit and preserve the run URL/status.
5. Historical follow-up: Update the project handoff documentation with the final accepted scope and remaining limitations. The referenced handoff documents are not included in this submission.