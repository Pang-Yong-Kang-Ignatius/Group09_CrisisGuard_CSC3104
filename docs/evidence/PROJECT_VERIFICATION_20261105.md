# Test 6 and complete Test 7 verification, 5 November 2026

This is a sanitized functional verification record. It preserves historical
negative performance results and does not establish proposal acceptance.
Raw local E2E directories contain generated credentials and remain ignored.

## Source and scope

Successful E2E run: `a017fd5f6083414eb5e73138255f4c9c`.
Local evidence: `.project-test-results/e2e-a017fd5f6083414eb5e73138255f4c9c/`.
The run started at 09:26:34 UTC and completed at 10:09:30 UTC on 5 November
(17:26-18:09 Singapore time). Its recorded HEAD was
`056961cc75f5944f77c606b8e35c55ddb1063ffe`, with uncommitted fixes present.
This is not a clean-commit or hosted-CI result.

During pre-push review, every retained application source hash and E2E runner
hash matched the current working files. The retained `report.json` SHA-256 is
`baa58b54adfee6ff8ec5df1a7ee468f0445d4c0b35cbda49d372513a3a281309`.
Selected source fingerprints:

| File | Run/working-file SHA-256 | LF-normalized SHA-256 |
|---|---|---|
| `cloud/controller/storage.py` | `3b917452cf8aed9e0a5e6ba19fd1b7a416db5750cdd9cc650c620c2b2ee0512d` | `68aa3794ec875e290ea77df9d1f2daeaa98d280b8d3bfac37e07e208ea9952c4` |
| `infrastructure/scripts/project_e2e.py` | `897f6a4f81fcb46b4b82d8260360b78098097507be1bcb98ad139c0d421a5049` | same |
| `infrastructure/scripts/project_e2e_scenarios.py` | `12e4cbf37e4f3f779d1b68c61eb71079b186fb2877e4d7e714e751b7a0e84be7` | same |

Git uses `* text=auto`; Windows checkouts may have CRLF while committed files
are LF. Byte hashes must be compared using the stated representation. No mass
line-ending conversion was required by the fixes.

## Test 6: ZIP portability

The recovery-matrix planner now obtains revision provenance through the shared
`repository_revision()` helper. An owned checkout records its HEAD. A source
ZIP without `.git` records `git_head: null` and `git_head_status: NOT VERIFIED`,
while retaining its source SHA-256 manifest. It does not fail merely because
Git metadata is absent or borrow a parent repository's revision.

Acceptance and admission plan tests cover both Git-checkout and GitHub-ZIP
provenance, exact randomized arm order and source fingerprints. Plan-only
verification runs no Docker experiment and does not imply performance acceptance.

Fresh pre-push Quick verification on Python 3.11.9 / Node 24.22.1:
`.project-test-results/20261105T104701Z-70f78e64/summary.json` records
**1,404 Python and 88 JavaScript passes, zero failed/skipped test cases**.
Coverage, optional PostgreSQL integration, Docker health and evaluation checks
were skipped in Quick mode; proposal acceptance remained NOT VERIFIED.
These totals include overlapping API/security subsets and must not be added to
earlier focused runs as independent tests.

The pre-push review also built and extracted a candidate source ZIP from HEAD
plus the 17 selectively proposed paths, with Git's LF normalization and the
coverage audit retained from HEAD. It contained no `.git`, private `.env`,
private Compose override or local test artifacts. Using the existing Python
environment through a local junction (not included in the ZIP):

| Extracted-source invocation | Local result directory | Result |
|---|---|---|
| `-Quick` | `.project-test-results/20261105T105043Z-abdaeedf` | 1,404 Python + 88 JavaScript passes; zero failed/skipped cases |
| Default-equivalent `-Full` | `.project-test-results/20261105T105134Z-e58298d5` | Same 1,404 + 88 passes; Python coverage, evaluation-evidence checks and audit generation PASS |

Both correctly recorded source revision NOT VERIFIED. Full left optional
PostgreSQL integration and Docker health SKIP, and JavaScript coverage and
proposal acceptance NOT VERIFIED. Its generated audit stayed in the extracted
copy; the original coverage audit was preserved. The snapshot and results are
retained locally under `tmp/prepush-review-4b53b830e160/`. This verifies ZIP
source portability on the existing Windows host, not clean-machine installation
or a future remote GitHub archive. Documentation was finalized after these runs;
the tested runtime/test source bytes were unchanged.

## Test 7: complete real-system E2E

The report contains **18 PASS, zero FAIL/SKIP and three NOT VERIFIED**.
The 18 executable checks include cleanup; it is not an additional nineteenth pass.

All initial isolation, both-hospital SQLite capture/application, dependency-chain
replay, MetaGuard grant/ACK/completion, protected Cloud application, Dashboard/SQL
consistency, duplicate replay and all-four-event-type checks passed.
All eight comprehensive scenarios passed:

1. Cloud validation, authentication, duplicates, dependencies and fencing.
2. Actual scheduler inheritance ON/OFF, deterministic ties and ageing.
3. Durable experimental scheduling policies.
4. Independent hospital-link outages and automatic recovery.
5. Restarts, lost ACK/commit responses, retry and quarantine recovery.
6. Fixed admission pacing/caps, telemetry and AIMD state/rate changes.
7. Experimental pressure-health and bounded deferral.
8. Dashboard APIs/history/recovery and 30 authenticated existing-record live reads.

The final Dashboard probe observed ten live samples per Cloud service, healthy
database checks, and live p95 values of 7.47 ms (Patient), 6.77 ms (Emergency),
and 6.99 ms (Admin). Its ten-sample populations had zero measured violations of
the configured 250-ms SLO. These are bounded functional observations, not load
capacity, comparative superiority or performance-acceptance results.

The report retains 37 captures and 34 unique applied lifecycle records. The
remaining three Hospital A fixture events deliberately exercise permanent
quarantine, a quarantined descendant and a missing prerequisite. Hospital B
has no unresolved events; no unresolved grants or active faults remain.

Owned-resource cleanup PASS records removal of nine owned containers and three
owned volumes, with the unique test image retained. Preservation PASS records
all original baseline resources unchanged. This verifies recorded resource
identities/metadata, not original database-content equality. The review did not
restart Docker, alter resources or rerun E2E.

## Fixes and focused regression coverage

- Controller: explicit candidate replacement/withdrawal retires older cancelled
  or expired grants, preventing reconciliation from resurrecting them. Ordinary
  cancellation retry, acknowledged-work pinning, transaction locks and audits remain.
- SQL harness: parameterless statements pass `None` to Psycopg, preserving literal
  `%` in the transient-error trigger function. Parameterized queries, server identity,
  read-only evidence and rollback remain enforced. Error reports retain only safe
  metadata and a query hash, never raw SQL/error text or credentials.
- E2E Dashboard: provisioning supplies its generated replay telemetry credential
  alongside the distinct live credential, satisfying the existing protective gate.
  Local-only restrictions, authentication, pacing and request bounds remain.

Focused PostgreSQL regressions executed during the investigations on separately
initialized native PostgreSQL 18.6 clusters; the E2E itself used PostgreSQL 16.
The retained diagnostic records are local under `tmp/test7-second-pg-*` and
`tmp/test7-dashboard-pg-*`. The clusters were stopped with data retained.
These ASGI/native-database tests complement the successful Docker E2E and must
not be mislabeled as additional full E2E runs.

## Reproduction and remaining limits

From a complete checkout or extracted source with the documented dependencies:

```powershell
.\test.project.ps1 -Quick
```

For coverage, `-Full` runs the same regression population and refreshes the local
coverage audit. The mutable `PROJECT_TEST_COVERAGE.md` refresh is intentionally
excluded from this proposed commit. See [testing](../testing.md) for the guarded
PostgreSQL allowlist and its explicit scratch-database requirements.

A separately authorized E2E reproduction starts with:

```powershell
.\test.project.ps1 -EndToEnd
```

Review that new run's isolation manifest and evidence before supplying its own
`<project>:<UUID>` confirmation through the documented continuation workflow.
Never reuse a historical confirmation token. E2E creates resources, injects
faults and performs owned cleanup; these commands are instructions, not an
authorization to execute them during review.

Browser interaction/accessibility/rendering, per-event Cloud log correlation
and performance acceptance/sustained fairness/full D2 remain **NOT VERIFIED**.
Clean-machine Windows installation, final submitted-commit GitHub Actions and
all-member approval remain separate submission tasks.

[W5/H5 findings](../reports/H5_W5_REVIEW_20261103.md), failed drain/routine
guardrails, H5's failed network-preservation guard and zero strict accepted
trials, provisional read-only capacity and missing original raw archives remain
unchanged. This functional pass does not waive them. The former `docs/TODO.md`
and missing-evidence inventory are unavailable in this checkout. See the
[requirements audit](../reports/CORE_REQUIREMENTS_AUDIT.md#requirements-to-implementation-matrix) and
[evidence availability notes](../EVIDENCE_INDEX.md#availability-and-historical-records).
