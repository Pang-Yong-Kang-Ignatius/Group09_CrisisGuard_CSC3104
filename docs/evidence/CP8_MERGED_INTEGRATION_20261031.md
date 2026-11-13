# CrisisGuard Final Merged Integration Verification

**Date:** 31 October 2026  
**Project:** CSC3104 Group 09, CrisisGuard  
**Verified commit:** `459cb69a26ca40d5dc5abf0420bbf1d9cf251af4`  
**Branch:** `main`  
**Environment:** Windows, Python 3.11, Docker Desktop, PostgreSQL 16

## 1. Objective

Verify that the merged CrisisGuard implementation remains compatible across the Cloud APIs, MetaGuard controller, hospital edge services, dependency scheduler and Dashboard.

This verification includes Haris's experimental scheduling changes merged through Pull Request #19.

Testing was performed using isolated PostgreSQL and Docker environments to preserve existing project databases and volumes.

## 2. Python Regression Tests

### 2.1 Scheduling and Protected Replay

Test command:

```powershell
.\.venv311\Scripts\python.exe -m pytest -q tests/unit/test_cp8_policies.py tests/unit/test_scheduler_replay_policy.py tests/unit/test_metaguard_replay.py
```

**Result: 59 passed, 0 failed.**

### 2.2 PostgreSQL Controller Integration

Test command:

```powershell
.\.venv311\Scripts\python.exe -m pytest -q tests/integration/test_controller_postgres.py tests/integration/test_cp8_postgres.py tests/integration/test_metaguard_reliability.py
```

**Result: 84 passed, 0 failed.**

These tests used a separate PostgreSQL test container and isolated database schemas.

### 2.3 Combined Results

| Verification | Passed | Failed |
|---|---:|---:|
| Scheduling and protected replay | 59 | 0 |
| PostgreSQL controller integration | 84 | 0 |
| **Total** | **143** | **0** |

Both test runs reported one non-failing Starlette dependency deprecation warning.

## 3. Docker Integration

An isolated Docker Compose project named `cp8-final-check` was created using the merged source.

All eight services started successfully.

| Service | Host Port | Status |
|---|---|---|
| Hospital A | 18100 | Healthy |
| Patient API | 18101 | Healthy |
| Hospital B | 18102 | Healthy |
| Emergency API | 18103 | Healthy |
| Admin API | 18104 | Healthy |
| MetaGuard Controller | 18105 | Healthy |
| Dashboard | 18180 | Healthy |
| PostgreSQL | 15435 | Healthy |

All seven HTTP service probes returned HTTP 200.

The isolated PostgreSQL event table initially contained zero events.

**Result: Eight-service Docker integration passed.**

## 4. Hospital Persistence

Two synthetic patient-registration events were captured while automatic replay was disabled.

| Event ID | Hospital | Initial Status |
|---|---|---|
| CP8_A_b0018a4d | Hospital_A | pending |
| CP8_B_b0018a4d | Hospital_B | pending |

Both hospital containers were restarted.

After restarting:

- Both events remained in their respective SQLite databases.
- Both events retained their `pending` status.
- PostgreSQL still contained zero events.
- Hospital identities remained correct.

**Result: Local event persistence verified.**

## 5. MetaGuard-Protected Recovery

Automatic replay was enabled for both hospitals.

After recovery, PostgreSQL contained:

```text
CP8_A_b0018a4d|Hospital_A|applied
CP8_B_b0018a4d|Hospital_B|applied
```

Both events were successfully applied with the correct event IDs and hospital identities.

**Result: Protected two-hospital recovery verified.**

This test confirms successful recovery through the protected stack. Individual internal grant transitions were not independently traced during this Docker test.

## 6. GitHub Actions

GitHub Actions was reported green for the merged main branch.

**Commit:** `459cb69a26ca40d5dc5abf0420bbf1d9cf251af4`

**Workflow run URL:** Pending addition.

The exact workflow run and job conclusions should be retained with the final release evidence.

## 7. Scope and Limitations

This verification confirms successful local integration for the recorded scenarios.

It does not establish:

- Full proposal performance acceptance criteria.
- Calibrated 30%/70% Cloud capacity conditions.
- Performance superiority of constant-rate or AIMD admission.
- Independent physical hospital-network partitions.
- Final three-host AWS performance.
- Correctness under every possible concurrent failure scenario.

These requirements remain tracked separately in `docs/TODO.md`.

## 8. Conclusion

The merged CrisisGuard implementation passed 143 focused Python checks and an isolated eight-service Docker integration test.

Both hospitals successfully retained pending events across restarts and subsequently applied them to PostgreSQL through MetaGuard-protected recovery.

The results provide evidence of compatibility between the merged Cloud, Edge, Scheduler, MetaGuard and Dashboard components.

Final teammate approvals and complete proposal evaluation acceptance remain outstanding.