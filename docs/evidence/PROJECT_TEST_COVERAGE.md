# CrisisGuard project testing and coverage audit

Generated UTC: 2026-11-07T08:15:06.140318+00:00. Source revision: `5c2a6acaae9aa20123f769872e164d5580f38cbd`.
Working-tree source hashes, Git status and detailed results are retained in the unique local run.
Results directory: `.project-test-results/20261107T081506Z-c86a8d45` (ignored; never historical evidence).

Environment: Python `3.11.9`; Node `v24.22.1`; `win32`.
Only Python 3.11 is supported by the runner. No dependencies were installed.

| Check | Status |
|---|---|
| Source revision | PASS |
| Python unit tests | PASS |
| API contract checks | PASS |
| Security regression | PASS |
| Python coverage | PASS |
| JavaScript unit tests | PASS |
| JavaScript coverage | NOT VERIFIED |
| PostgreSQL integration | SKIP |
| Docker health | SKIP |
| Evaluation evidence | PASS |
| Proposal acceptance | NOT VERIFIED |
| Coverage audit generation | PASS |

PASS means that an executed check succeeded. SKIP is never a pass. NOT VERIFIED means insufficient evidence.
A passing regression suite does not establish proposal acceptance or full component correctness.

| Suite | Total | Passed | Failed | Skipped |
|---|---:|---:|---:|---:|
| Python safe regression | 1542 | 1542 | 0 | 0 |
| JavaScript (including queue file test) | 94 | 94 | 0 | 0 |

Python counts are pytest cases, including parametrization. Node counts include one legacy queue script as a file-level test; internal assertions are not counted as individual cases.

## Python line, branch and function evidence

Line coverage: 5750/10345 (55.58%).
Branch coverage: 1800/3216 (55.97%).
See coverage.json, coverage.xml, coverage.txt and htmlcov/ for missing lines and branches.
Function entries: 399/620 discovered Python function definitions observed by a pytest profiling hook (collection/setup/call/teardown and new threads).
Function entries include failing tests and setup; an entry does not prove correct return values, all branches, subprocess execution or semantic verification.

| Production module | Functions observed / discovered | Unexercised functions (first 12; complete inventory in JSON) |
|---|---:|---|
| `cloud/admin_service/main.py` | 5/5 | None observed |
| `cloud/controller/admission.py` | 10/14 | create_schema:177, load:194, poll:240, fetch:245 |
| `cloud/controller/experimental.py` | 3/4 | withdraw_drr:58 |
| `cloud/controller/grants.py` | 3/3 | None observed |
| `cloud/controller/main.py` | 30/31 | _admission_loop:54 |
| `cloud/controller/pressure_health.py` | 2/2 | None observed |
| `cloud/controller/scheduler.py` | 4/4 | None observed |
| `cloud/controller/storage.py` | 1/23 | init_db:21, add_ticket:118, set_candidate:150, list_tickets:210, update_priority:221, issue_grant:232, get_grant:310, _audit:333, _lock:340, get_grant_status:343, acknowledge_grant:356, complete_grant:380 … |
| `cloud/database/db.py` | 3/8 | get_connection:14, save_cloud_event:58, get_missing_prerequisites:188, get_recent_cloud_events:217, get_cloud_event:260 |
| `cloud/emergency_service/main.py` | 5/5 | None observed |
| `cloud/metaguard.py` | 5/5 | None observed |
| `cloud/patient_service/main.py` | 5/5 | None observed |
| `cloud/telemetry.py` | 8/8 | None observed |
| `cloud/traffic.py` | 2/2 | None observed |
| `dashboard/__init__.py` | 0/0 | None observed |
| `dashboard/main.py` | 11/16 | health:33, service_health:187, cloud_metrics:201, status_codes:215, dashboard_event_history:279 |
| `dashboard/test_traffic.py` | 20/20 | None observed |
| `edge/capture_api/admission_retry.py` | 4/4 | None observed |
| `edge/capture_api/auto_replay.py` | 3/3 | None observed |
| `edge/capture_api/cloud_client.py` | 6/7 | replay_pending_events:178 |
| `edge/capture_api/grant_client.py` | 7/7 | None observed |
| `edge/capture_api/main.py` | 5/8 | home:76, list_events:125, get_recovery_status:135 |
| `edge/capture_api/scheduled_replay.py` | 2/2 | None observed |
| `edge/database/db.py` | 15/17 | get_pending_events:132, get_cloud_event:513 |
| `edge/scheduler/config.py` | 3/3 | None observed |
| `edge/scheduler/dag.py` | 8/8 | None observed |
| `edge/scheduler/inspector.py` | 1/2 | _demo:302 |
| `edge/scheduler/live_preview.py` | 1/1 | None observed |
| `edge/scheduler/priority.py` | 2/2 | None observed |
| `edge/scheduler/scheduler.py` | 2/2 | None observed |
| `edge/scheduler/session.py` | 4/4 | None observed |
| `edge/scheduler/validation.py` | 10/10 | None observed |
| `evaluation/experiments/analyze_inheritance_results.py` | 12/13 | main:393 |
| `evaluation/experiments/analyze_recovery_matrix.py` | 7/8 | main:298 |
| `evaluation/experiments/assess_acceptance.py` | 1/2 | main:139 |
| `evaluation/experiments/assess_final_holdout.py` | 1/3 | assessment:27, main:72 |
| `evaluation/experiments/calibrate_event_costs.py` | 1/5 | replica:40, main:163, request:75, send:79 |
| `evaluation/experiments/calibrate_live_reads.py` | 6/9 | run_step:116, positive_float:133, main:140 |
| `evaluation/experiments/calibrate_ready_load.py` | 8/13 | observe:86, execute:193, main:241, get:87, save:271 |
| `evaluation/experiments/compare_inheritance.py` | 5/6 | main:183 |
| `evaluation/experiments/run_inheritance_matrix.py` | 6/7 | main:216 |
| `evaluation/experiments/run_recovery_matrix.py` | 19/29 | __init__:337, save:348, execute_trial:351, request:372, run_case:860, replay:489, live_reads:498, resource_samples:554, activate_automatic:566, read_one:509 |
| `evaluation/workloads/generate_workload.py` | 6/7 | main:234 |
| `infrastructure/scripts/audit_deployment.py` | 10/10 | None observed |
| `infrastructure/scripts/aws_outage_host.py` | 5/14 | run:22, container:36, paths:60, request:122, snapshot:132, capture:152, cloud_state:174, duplicates:184, main:203 |
| `infrastructure/scripts/database_backup.py` | 8/10 | run:39, table_fingerprint_sql:129 |
| `infrastructure/scripts/init_local_env.py` | 6/6 | None observed |
| `infrastructure/scripts/local_outage_agent.py` | 11/15 | docker:31, main:183, status:97, status:168 |
| `infrastructure/scripts/metaguard_admission_checks.py` | 0/8 | verify_admission:21, status:32, until:35, wait_state:46, drive:90, check:48, own:95, worker:101 |
| `infrastructure/scripts/metaguard_fault_proxy.py` | 0/3 | log_message:18, respond:21, dispatch:28 |
| `infrastructure/scripts/metaguard_reliability_checks.py` | 0/8 | verify_reliability:16, until:25, event:36, capture:40, applied:44, faults:48, status:53, safe:59 |
| `infrastructure/scripts/metaguard_replay_checks.py` | 0/2 | verify_replay:16, request:28 |
| `infrastructure/scripts/project_audit_catalog.py` | 0/0 | None observed |
| `infrastructure/scripts/project_docker_guard.py` | 11/11 | None observed |
| `infrastructure/scripts/project_e2e.py` | 27/45 | create_stack:697, establish_database:788, command:130, scenario:239, inspect:250, live_reader:254, read_docker:263, inventory:266, capture_baseline:269, operator:414, hospital_auth:417, api:420 … |
| `infrastructure/scripts/project_e2e_scenarios.py` | 0/21 | event:23, capture:29, replay:41, proxy_fault:46, drain:60, lifecycle:71, smoke:108, authorized_grant:168, cloud_contracts:188, scheduler:231, policies:268, partitions:306 … |
| `infrastructure/scripts/project_test_probe.py` | 4/7 | profile:22, pytest_configure:29, pytest_unconfigure:84 |
| `infrastructure/scripts/project_verify.py` | 16/24 | run:43, docker_checks:292, javascript_coverage:347, generate_audit:368, integration:627, docker:646, audit:654, redirect_request:336 |
| `infrastructure/scripts/rehearse_update.py` | 0/9 | sqlite_fingerprint:32, base_files:36, settings_file:40, activate:44, capture:53, settle:64, backup_and_restore:75, rehearse:84, main:242 |
| `infrastructure/scripts/release_checkpoint.py` | 12/14 | run:27, main:204 |
| `infrastructure/scripts/smoke_deployment.py` | 12/22 | run:40, free_ports:53, request:67, cloud_records:200, verify_dashboard:270, verify_health_checker:295, verify_automatic_recovery:314, main:541, call:138, start:141 |
| `infrastructure/scripts/smoke_metaguard.py` | 0/10 | wait_for:33, verify:45, run:84, docker:95, call:101, passed:104, containers:108, api:182, query:192, ticket:197 |
| `infrastructure/scripts/smoke_metaguard_deployment.py` | 5/18 | verify_links:87, verify:145, run:163, docker:169, snapshot:172, passed:179, call:264, api:266, query:274, wait:278, state:288, allocate:319 … |
| `infrastructure/scripts/verify_aws_recovery.py` | 10/11 | command:96 |
| `infrastructure/scripts/verify_aws_ssh_transport.py` | 0/9 | main:24, run:30, key:32, responder:55, get:73, wait:78, base:86, do_GET:57, log_message:61 |
| `infrastructure/scripts/verify_devops_config.py` | 0/4 | run:23, verify:29, main:117, audit:50 |
| `infrastructure/scripts/verify_windows_launcher.py` | 0/2 | verify:26, docker:52 |

## JavaScript coverage

Native NODE_V8_COVERAGE raw files are retained. The preload names only complete byte-identical VM modules.
Most existing harnesses slice/prepend JavaScript without source offsets; those anonymous paths cannot be accurately mapped to production files. No project-wide percentage or browser coverage is claimed.

| Dashboard module | Measured ranges | Executed ranges | Status |
|---|---:|---:|---|
| `dashboard/static/app.js` | 0 | 0 | NOT VERIFIED (unmapped VM/browser paths) |
| `dashboard/static/outage.js` | 0 | 0 | NOT VERIFIED (unmapped VM/browser paths) |
| `dashboard/static/quick-events.js` | 0 | 0 | NOT VERIFIED (unmapped VM/browser paths) |
| `dashboard/static/sync-status.js` | 0 | 0 | NOT VERIFIED (unmapped VM/browser paths) |
| `dashboard/static/test-traffic.js` | 80 | 66 | NOT VERIFIED (partial V8 ranges only) |
| `dashboard/static/workspace.js` | 0 | 0 | NOT VERIFIED (unmapped VM/browser paths) |

Navigation, metrics, capture, history, queue/recovery, Inspector, test traffic, errors and loading/disabled states have DOM-model tests. Actual browser navigation, accessibility, rendering and concurrency remain NOT VERIFIED.

## Component coverage matrix

| Component | Source | Executed test references | Status / remaining scope |
|---|---|---|---|
| Patient API | `cloud/patient_service` | tests/integration/test_cloud_apis.py | NOT VERIFIED (partial test evidence): Real PostgreSQL and concurrent commits |
| Emergency API | `cloud/emergency_service` | tests/integration/test_cloud_apis.py | NOT VERIFIED (partial test evidence): Real PostgreSQL and concurrent commits |
| Admin API | `cloud/admin_service` | tests/integration/test_cloud_apis.py | NOT VERIFIED (partial test evidence): Real PostgreSQL and concurrent commits |
| PostgreSQL persistence, idempotency, prerequisites, DB errors | `cloud/database` | tests/integration/test_cloud_apis.py | NOT VERIFIED (partial test evidence): Real transaction isolation requires isolated PostgreSQL |
| Authentication, hospital identity, immutable content, grant fencing | `cloud/metaguard.py` | tests/integration/test_manual_replay_security.py; tests/unit/test_metaguard_reliability_security.py; tests/unit/test_project_security_gaps.py | NOT VERIFIED (partial test evidence): Cross-service transaction races require PostgreSQL |
| Traffic classification; live/replay/unclassified telemetry; p95/SLO | `cloud/traffic.py,cloud/telemetry.py` | tests/unit/test_cloud_live_metrics.py; tests/unit/test_cloud_metrics.py | NOT VERIFIED (partial test evidence): Client latency differs from middleware latency |
| Hospital A / B capture, SQLite, identity isolation | `edge/capture_api/main.py,edge/database` | tests/unit/test_capture_prerequisites.py; tests/unit/test_edge_routing_and_recovery.py; tests/unit/test_recovery_state.py | NOT VERIFIED (partial test evidence): Independent network partitions and physical power loss |
| Pending/applied/quarantined, dependencies, recovery journal | `edge/database` | tests/unit/test_recovery_state.py; tests/unit/test_scheduled_replay.py | NOT VERIFIED (partial test evidence): Persistent disk faults and process kill windows |
| Automatic recovery; manual replay authorization | `edge/capture_api/auto_replay.py,edge/capture_api/main.py` | tests/integration/test_manual_replay_security.py; tests/unit/test_smoke_automatic_recovery.py | NOT VERIFIED (partial test evidence): Actual restart/reconnect timing |
| Crash reconciliation; uncertain attempts; cloud communication | `edge/capture_api/cloud_client.py,edge/capture_api/scheduled_replay.py` | tests/unit/test_metaguard_replay.py; tests/unit/test_scheduled_replay.py | NOT VERIFIED (partial test evidence): Live ambiguous response/commit races |
| Retry exponential backoff and jitter | `edge/database/db.py` | tests/unit/test_recovery_state.py | NOT VERIFIED (partial test evidence): Distribution under sustained concurrent retries |
| Controller communication; grant ownership and acknowledgement | `edge/capture_api/grant_client.py` | tests/unit/test_metaguard_replay.py | NOT VERIFIED (partial test evidence): Live controller outages |
| Experimental bounded admission-deferral retry | `edge/capture_api/admission_retry.py` | tests/unit/test_admission_deferral_retry.py | NOT VERIFIED (partial test evidence): Opt-in; pilot is not full acceptance |
| Dependency DAG eligibility; missing prerequisites; blocked events | `edge/scheduler/dag.py` | tests/unit/test_dag.py; tests/unit/test_missing_prerequisite_inheritance.py | NOT VERIFIED (partial test evidence): Global distributed order is not promised |
| Priority ageing; inheritance ON/OFF; tie-breaking | `edge/scheduler/priority.py,edge/scheduler/scheduler.py` | tests/unit/test_ageing.py; tests/unit/test_inheritance_analysis.py; tests/unit/test_inheritance_comparison.py; tests/unit/test_inheritance_matrix.py; tests/unit/test_missing_prerequisite_inheritance.py; tests/unit/test_priority.py; tests/unit/test_scheduler.py; tests/unit/test_scheduler_configuration.py; tests/unit/test_scheduler_inspector.py; tests/unit/test_scheduler_inspector_api.py; tests/unit/test_scheduler_replay_policy.py; tests/unit/test_scheduler_session.py; tests/unit/test_scheduler_validation.py | NOT VERIFIED (partial test evidence): Elapsed urgent p95 gain remains unestablished |
| Retry eligibility; session pruning; input validation | `edge/scheduler/session.py,edge/scheduler/validation.py` | tests/unit/test_scheduler_session.py; tests/unit/test_scheduler_validation.py | NOT VERIFIED (partial test evidence): Sustained time-advancing starvation bounds |
| Scheduler Inspector and live preview | `edge/scheduler/inspector.py,edge/scheduler/live_preview.py` | tests/unit/test_scheduler_inspector.py; tests/unit/test_scheduler_inspector_api.py | NOT VERIFIED (partial test evidence): Preview is not an execution trace |
| FIFO / Priority / Bounded / RR / DRR configuration | `cloud/controller/scheduler.py,cloud/controller/experimental.py,edge/scheduler/config.py` | tests/unit/test_controller.py; tests/unit/test_controller_api.py; tests/unit/test_controller_grants.py; tests/unit/test_cp8_policies.py; tests/unit/test_scheduler_configuration.py | NOT VERIFIED (partial test evidence): Experimental policies remain opt-in; RR is bounded with B=0 |
| Persistent tickets; grant issuance/ACK/expiry/fencing/reconciliation | `cloud/controller/storage.py` | tests/unit/test_controller_grants.py; tests/unit/test_metaguard_reliability_security.py | NOT VERIFIED (partial test evidence): In-memory model does not establish durable storage correctness |
| Controller HTTP, outages, authentication, DB errors | `cloud/controller/main.py` | tests/unit/test_controller_api.py; tests/unit/test_metaguard_reliability_security.py; tests/unit/test_project_security_gaps.py | NOT VERIFIED (partial test evidence): Real multi-process outage paths |
| In-memory scheduler/grant reference models | `cloud/controller/grants.py,cloud/controller/scheduler.py` | tests/unit/test_controller.py; tests/unit/test_controller_api.py; tests/unit/test_controller_grants.py | NOT VERIFIED (partial test evidence): Separate from production PostgreSQL storage |
| Fixed-rate/AIMD; Normal/Caution/Recovery | `cloud/controller/admission.py` | tests/unit/test_admission.py; tests/unit/test_admission_deferral_retry.py; tests/unit/test_admission_study.py; tests/unit/test_aimd_recovery_diagnosis.py | NOT VERIFIED (partial test evidence): Original recovery-time regression FAILED |
| Telemetry freshness; rate/in-flight admission safety | `cloud/controller/admission.py,cloud/controller/storage.py` | tests/unit/test_admission.py; tests/unit/test_admission_deferral_retry.py; tests/unit/test_admission_study.py | NOT VERIFIED (partial test evidence): Fresh DB transactional behavior requires isolated integration |
| Experimental pressure-health policy | `cloud/controller/pressure_health.py` | tests/unit/test_pressure_health.py | NOT VERIFIED (partial test evidence): Opt-in; no production threshold promotion |
| Dashboard HTTP boundary; proxy concurrency; loading/error responses | `dashboard/main.py` | tests/unit/test_dashboard_metaguard.py; tests/unit/test_dashboard_offline.py; tests/unit/test_dashboard_replay_authorization.py | NOT VERIFIED (partial test evidence): Real browser and live upstream concurrency |
| Navigation and accessibility attributes | `dashboard/static/workspace.js` | tests/unit/test_dashboard_navigation.cjs | NOT VERIFIED (partial test evidence): Actual browser keyboard/screen-reader/layout checks NOT VERIFIED |
| Monitoring; Patient/Emergency/Admin metrics; p95/SLO; replay/unclassified/DB health | `dashboard/static/app.js,dashboard/static/workspace.js` | tests/unit/test_dashboard_recovery.py; tests/unit/test_dashboard_metrics.cjs | NOT VERIFIED (partial test evidence): Sliced VM tests do not measure whole browser modules |
| Event capture and input validation | `dashboard/static/app.js,dashboard/static/quick-events.js` | tests/unit/test_dashboard_capture.cjs; tests/unit/test_dashboard_quick_events.cjs | NOT VERIFIED (partial test evidence): Actual browser interactions NOT VERIFIED |
| Event history, queue, recovery controls and sync | `dashboard/static/app.js,dashboard/static/sync-status.js` | tests/unit/test_dashboard_queue.cjs; tests/unit/test_dashboard_replay_ui.cjs; tests/unit/test_dashboard_sync.cjs | NOT VERIFIED (partial test evidence): Polling/focus/background-tab behavior in real browser |
| Scheduler Inspector UI | `dashboard/static/app.js` | tests/unit/test_dashboard_inspector.cjs | NOT VERIFIED (partial test evidence): Actual browser rendering NOT VERIFIED |
| Local test traffic generator; bounded concurrent requests | `dashboard/test_traffic.py,dashboard/static/test-traffic.js` | tests/unit/test_dashboard_test_traffic.py; tests/unit/test_dashboard_test_traffic.cjs | NOT VERIFIED (partial test evidence): No traffic generated by verification runner |
| Outage UI/agent and AWS host controls | `dashboard/static/outage.js,infrastructure/scripts/local_outage_agent.py,infrastructure/scripts/aws_outage_host.py` | tests/unit/test_aws_outage_host.py; tests/unit/test_local_outage_agent.py | NOT VERIFIED (partial test evidence): Fault injection deliberately omitted |
| Docker Compose; eight-service startup; readiness; environment | `compose.yaml,infrastructure/docker,start.project.bat` | tests/unit/test_deployment_audit.py; tests/unit/test_init_local_env.py; tests/unit/test_metaguard_deployment.py; tests/unit/test_windows_launcher_batch.py | NOT VERIFIED (partial test evidence): Read-only Docker checks do not establish clean-machine startup |
| AWS deployment config and launcher; GitHub Actions | `infrastructure/aws,.github/workflows` | tests/unit/test_deployment_audit.py; tests/unit/test_lab_launcher.py | NOT VERIFIED (partial test evidence): No AWS operations; hosted CI for final revision not established |
| Backup/restore; release checkpoints; secret handling | `infrastructure/scripts/database_backup.py,infrastructure/scripts/release_checkpoint.py,infrastructure/scripts/audit_deployment.py` | tests/unit/test_database_backup.py; tests/unit/test_deployment_audit.py; tests/unit/test_release_checkpoint.py | NOT VERIFIED (partial test evidence): No live backup/restore or deployment performed |
| Smoke/recovery/preflight/update verification utilities | `infrastructure/scripts` | tests/unit/test_aws_recovery_verification.py; tests/unit/test_metaguard_smoke_diagnostics.py; tests/unit/test_reusable_health_checker.py; tests/unit/test_smoke_automatic_recovery.py; tests/unit/test_smoke_dashboard.py | NOT VERIFIED (partial test evidence): Dangerous entrypoints only modeled; never automatically invoked |
| Workload generation and generator accuracy | `evaluation/workloads` | tests/unit/test_generator_accuracy.py; tests/unit/test_workload_generator.py | NOT VERIFIED (partial test evidence): Generated workload correctness is not capacity evidence |
| Experiment config/calibration; inheritance comparisons | `evaluation/experiments` | tests/unit/test_cp8_costs.py; tests/unit/test_inheritance_analysis.py; tests/unit/test_inheritance_comparison.py; tests/unit/test_inheritance_matrix.py; tests/unit/test_live_read_calibration.py; tests/unit/test_missing_prerequisite_inheritance.py; tests/unit/test_ready_load_calibration.py | NOT VERIFIED (partial test evidence): No performance experiments run |
| Recovery metrics; admission/AIMD/policy comparisons; p95/SLO/statistics | `evaluation/experiments` | tests/unit/test_acceptance.py; tests/unit/test_admission_study.py; tests/unit/test_cp8_analysis.py | NOT VERIFIED (partial test evidence): Full D2 incomplete; urgent gain vs RR unestablished |
| Evidence provenance and experiment reproducibility | `evaluation/experiments,docs/evidence` | tests/unit/test_acceptance.py; tests/unit/test_generator_accuracy.py; tests/unit/test_live_generator_boundaries.py | NOT VERIFIED (partial test evidence): Historical raw evidence scope and protocol amendments retained |

Source discovery additionally inventories all production Python modules above, six dashboard scripts, AWS launcher/shell files, Compose variants, batch scripts, static HTML/CSS and GitHub workflow configuration. AWS launcher Python is exercised by existing tests but is outside the requested coverage.py roots; shell/YAML/batch/HTML/CSS are not Python coverage denominators.

## API route coverage matrix

Routes are discovered via AST without importing services; nested telemetry routes are expanded for all three Cloud services. Framework OpenAPI/docs/static mounts are excluded. Observations are actual TestClient responses from passed tests, not inferred from test names.
Response classes are evidence of execution, not proof of complete scenario assertions. Candidate missing scenarios need contract review; concurrency is never inferred from HTTP status.

| Service | Method | Route | Passed test references (first 3; full JSON retained) | Observed scenarios | Missing / review candidates | Status |
|---|---|---|---|---|---|---|
| `cloud/admin_service/main.py` | GET | `/` | tests/unit/test_cloud_live_metrics.py::test_all_cloud_apps_expose_additive_fields_without_database_access[admin] | success | Assertion completeness review | NOT VERIFIED |
| `cloud/admin_service/main.py` | GET | `/health` | tests/integration/test_cloud_apis.py::test_health_check_db_failure[admin]; tests/integration/test_cloud_apis.py::test_health_check_success[admin] | success, dependency failure | Assertion completeness review | NOT VERIFIED |
| `cloud/admin_service/main.py` | POST | `/events` | tests/integration/test_cloud_apis.py::test_conflicting_event_returns_409[admin]; tests/integration/test_cloud_apis.py::test_database_unavailable_returns_503[admin]; tests/integration/test_cloud_apis.py::test_identical_retry_returns_200[admin] | success, invalid input, unauthorized, forbidden, conflict, dependency failure | concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/admin_service/main.py` | GET | `/events` | tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[admin- LOOKUP with spaces ]; tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[admin-..]; tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[admin-.] | success, invalid input, missing resource, dependency failure | Assertion completeness review | NOT VERIFIED |
| `cloud/admin_service/main.py` | GET | `/events/{event_id}` | tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[admin-LOOKUP_NORMAL-01]; tests/integration/test_cloud_apis.py::test_event_status_lookup_contract[admin]; tests/unit/test_cloud_live_metrics.py::test_successful_event_get_requires_live_provenance_not_just_http_200[admin] | success, missing resource, dependency failure | Assertion completeness review | NOT VERIFIED |
| `cloud/controller/main.py` | GET | `/health` | tests/unit/test_controller_api.py::test_health; tests/unit/test_metaguard_replay.py::test_missing_or_bad_credentials_fail_closed[headers0]; tests/unit/test_metaguard_replay.py::test_missing_or_bad_credentials_fail_closed[headers1] | success | Assertion completeness review | NOT VERIFIED |
| `cloud/controller/main.py` | GET | `/admission/status` | tests/unit/test_controller_api.py::test_operator_admission_status | success | unauthorized, forbidden, dependency failure | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/tickets` | tests/unit/test_controller_api.py::test_database_unavailable[post-/tickets-add_ticket]; tests/unit/test_controller_api.py::test_invalid_ticket[changes0]; tests/unit/test_controller_api.py::test_invalid_ticket[changes1] | success, invalid input, forbidden, dependency failure | unauthorized, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | GET | `/tickets` | tests/unit/test_controller_api.py::test_database_unavailable[get-/tickets-list_tickets]; tests/unit/test_controller_api.py::test_view_tickets; tests/unit/test_metaguard_replay.py::test_hospital_cannot_operate_other_hospital_or_operator_routes[GET-/tickets-None] | success, unauthorized, forbidden, dependency failure | Assertion completeness review | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/grants/next` | tests/unit/test_controller_api.py::test_database_unavailable[post-/grants/next-issue_grant]; tests/unit/test_controller_api.py::test_grants; tests/unit/test_metaguard_replay.py::test_hospital_cannot_operate_other_hospital_or_operator_routes[POST-/grants/next-None] | success, forbidden, dependency failure | unauthorized, invalid input, admission denial, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | GET | `/hospitals/{hospital_id}/grant` | tests/unit/test_metaguard_replay.py::test_query_lookup_and_priority_preserve_valid_event_ids[..]; tests/unit/test_metaguard_replay.py::test_query_lookup_and_priority_preserve_valid_event_ids[\u60a3\u8005]; tests/unit/test_metaguard_replay.py::test_query_lookup_and_priority_preserve_valid_event_ids[a?b#c] | success | unauthorized, forbidden, dependency failure, invalid input, missing resource | NOT VERIFIED |
| `cloud/controller/main.py` | GET | `/hospitals/{hospital_id}/events/{event_id:path}/grant` | tests/unit/test_metaguard_replay.py::test_hospital_cannot_operate_other_hospital_or_operator_routes[GET-/hospitals/Hospital_B/events/x/grant-None] | forbidden | success, unauthorized, dependency failure, invalid input, missing resource | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/hospitals/{hospital_id}/grants/next` | tests/unit/test_admission_deferral_retry.py::test_hint_requires_opt_in_and_owner_authorization; tests/unit/test_admission_deferral_retry.py::test_legacy_controller_response_has_no_hint; tests/unit/test_metaguard_replay.py::test_hospital_cannot_operate_other_hospital_or_operator_routes[POST-/hospitals/Hospital_B/grants/next-None] | success, forbidden | unauthorized, dependency failure, invalid input, admission denial, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/grants/{grant_id}/reconcile` | tests/unit/test_metaguard_reliability_security.py::test_operator_invalid_uuid_rejected_before_storage[reconcile]; tests/unit/test_metaguard_replay.py::test_hospital_cannot_operate_other_hospital_or_operator_routes[POST-/grants/GID/reconcile-body10] | invalid input, forbidden | success, unauthorized, dependency failure, missing resource, conflict, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/grants/{grant_id}/abandon` | tests/unit/test_metaguard_reliability_security.py::test_operator_invalid_uuid_rejected_before_storage[abandon]; tests/unit/test_metaguard_replay.py::test_hospital_cannot_operate_other_hospital_or_operator_routes[POST-/grants/GID/abandon-body11] | invalid input, forbidden | success, unauthorized, dependency failure, missing resource, conflict, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/hospitals/{hospital_id}/candidate` | tests/unit/test_controller_api.py::test_candidate_replace_and_withdraw_contract; tests/unit/test_controller_api.py::test_incomplete_candidate_rejected[body0]; tests/unit/test_controller_api.py::test_incomplete_candidate_rejected[body1] | success, invalid input, forbidden | unauthorized, dependency failure, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/hospitals/{hospital_id}/priority` | tests/unit/test_metaguard_replay.py::test_query_lookup_and_priority_preserve_valid_event_ids[..]; tests/unit/test_metaguard_replay.py::test_query_lookup_and_priority_preserve_valid_event_ids[\u60a3\u8005]; tests/unit/test_metaguard_replay.py::test_query_lookup_and_priority_preserve_valid_event_ids[a?b#c] | success | unauthorized, forbidden, dependency failure, invalid input, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/hospitals/{hospital_id}/events/{event_id:path}/priority` | None | None | success, unauthorized, forbidden, dependency failure, invalid input, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/grants/{grant_id}/acknowledge` | tests/unit/test_controller_api.py::test_ack_admission_deferral_is_not_lifecycle_ack; tests/unit/test_metaguard_reliability_security.py::test_operator_invalid_uuid_rejected_before_storage[acknowledge]; tests/unit/test_metaguard_replay.py::test_hospital_cannot_operate_other_hospital_or_operator_routes[POST-/grants/GID/acknowledge-body7] | invalid input, forbidden, admission denial | success, unauthorized, dependency failure, missing resource, conflict, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/grants/{grant_id}/complete` | tests/unit/test_metaguard_reliability_security.py::test_operator_invalid_uuid_rejected_before_storage[complete]; tests/unit/test_metaguard_replay.py::test_hospital_cannot_operate_other_hospital_or_operator_routes[POST-/grants/GID/complete-body9] | invalid input, forbidden | success, unauthorized, dependency failure, missing resource, conflict, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/grants/{grant_id}/cancel` | tests/unit/test_metaguard_reliability_security.py::test_operator_invalid_uuid_rejected_before_storage[cancel]; tests/unit/test_metaguard_replay.py::test_hospital_cannot_operate_other_hospital_or_operator_routes[POST-/grants/GID/cancel-body8] | invalid input, forbidden | success, unauthorized, dependency failure, missing resource, conflict, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | GET | `/grants/by-id/{grant_id}` | tests/unit/test_controller_api.py::test_grant_status_route_precedes_hospital_event_lookup[False]; tests/unit/test_controller_api.py::test_grant_status_route_precedes_hospital_event_lookup[True]; tests/unit/test_metaguard_reliability_security.py::test_operator_invalid_uuid_rejected_before_storage[status] | success, invalid input, missing resource, forbidden | unauthorized, dependency failure | NOT VERIFIED |
| `cloud/controller/main.py` | GET | `/grants/{hospital_id}/{event_id}` | tests/unit/test_controller_api.py::test_database_unavailable[get-/grants/A/E-get_grant]; tests/unit/test_controller_api.py::test_grants; tests/unit/test_metaguard_replay.py::test_hospital_cannot_operate_other_hospital_or_operator_routes[GET-/grants/Hospital_B/x-None] | success, missing resource, forbidden, dependency failure | unauthorized, invalid input | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/grants/reconcile-expired` | tests/unit/test_metaguard_reliability_security.py::test_hospital_cannot_submit_authoritative_cloud_results[/grants/reconcile-expired-None] | forbidden | success, unauthorized, dependency failure, invalid input, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/controller/main.py` | POST | `/grants/{grant_id}/resolve` | tests/unit/test_controller_api.py::test_authoritative_resolution_response[completed-200]; tests/unit/test_controller_api.py::test_authoritative_resolution_response[expired-200]; tests/unit/test_controller_api.py::test_authoritative_resolution_response[invalid_state-409] | success, invalid input, forbidden, conflict | unauthorized, dependency failure, missing resource, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/emergency_service/main.py` | GET | `/` | tests/unit/test_cloud_live_metrics.py::test_all_cloud_apps_expose_additive_fields_without_database_access[emergency] | success | Assertion completeness review | NOT VERIFIED |
| `cloud/emergency_service/main.py` | GET | `/health` | tests/integration/test_cloud_apis.py::test_health_check_db_failure[emergency]; tests/integration/test_cloud_apis.py::test_health_check_success[emergency] | success, dependency failure | Assertion completeness review | NOT VERIFIED |
| `cloud/emergency_service/main.py` | POST | `/events` | tests/integration/test_cloud_apis.py::test_conflicting_event_returns_409[emergency]; tests/integration/test_cloud_apis.py::test_database_unavailable_returns_503[emergency]; tests/integration/test_cloud_apis.py::test_identical_retry_returns_200[emergency] | success, invalid input, unauthorized, forbidden, conflict, dependency failure | concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/emergency_service/main.py` | GET | `/events` | tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[emergency- LOOKUP with spaces ]; tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[emergency-..]; tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[emergency-.] | success, invalid input, missing resource, dependency failure | Assertion completeness review | NOT VERIFIED |
| `cloud/emergency_service/main.py` | GET | `/events/{event_id}` | tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[emergency-LOOKUP_NORMAL-01]; tests/integration/test_cloud_apis.py::test_event_status_lookup_contract[emergency]; tests/unit/test_cloud_live_metrics.py::test_successful_event_get_requires_live_provenance_not_just_http_200[emergency] | success, missing resource, dependency failure | Assertion completeness review | NOT VERIFIED |
| `cloud/patient_service/main.py` | GET | `/` | tests/unit/test_cloud_live_metrics.py::test_all_cloud_apps_expose_additive_fields_without_database_access[patient] | success | Assertion completeness review | NOT VERIFIED |
| `cloud/patient_service/main.py` | GET | `/health` | tests/integration/test_cloud_apis.py::test_health_check_db_failure[patient]; tests/integration/test_cloud_apis.py::test_health_check_success[patient] | success, dependency failure | Assertion completeness review | NOT VERIFIED |
| `cloud/patient_service/main.py` | POST | `/events` | tests/integration/test_cloud_apis.py::test_conflicting_event_returns_409[patient]; tests/integration/test_cloud_apis.py::test_database_unavailable_returns_503[patient]; tests/integration/test_cloud_apis.py::test_identical_retry_returns_200[patient] | success, invalid input, unauthorized, forbidden, conflict, dependency failure | concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/patient_service/main.py` | GET | `/events` | tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[patient- LOOKUP with spaces ]; tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[patient-..]; tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[patient-.] | success, invalid input, missing resource, dependency failure | Assertion completeness review | NOT VERIFIED |
| `cloud/patient_service/main.py` | GET | `/events/{event_id}` | tests/integration/test_cloud_apis.py::test_edge_status_lookup_round_trip[patient-LOOKUP_NORMAL-01]; tests/integration/test_cloud_apis.py::test_event_status_lookup_contract[patient]; tests/unit/test_cloud_live_metrics.py::test_successful_event_get_requires_live_provenance_not_just_http_200[patient] | success, missing resource, dependency failure | Assertion completeness review | NOT VERIFIED |
| `dashboard/main.py` | GET | `/health` | None | None | success | NOT VERIFIED |
| `dashboard/main.py` | GET | `/` | tests/integration/test_manual_replay_security.py::test_dashboard_boundary[127.0.0.1-127.0.0.1-headers0-200]; tests/integration/test_manual_replay_security.py::test_dashboard_boundary[127.0.0.1-127.0.0.1-headers7-403]; tests/integration/test_manual_replay_security.py::test_dashboard_boundary[127.0.0.1-127.0.0.1-headers8-403] | success, forbidden | Assertion completeness review | NOT VERIFIED |
| `dashboard/main.py` | GET | `/api/services` | None | None | success, forbidden, dependency failure | NOT VERIFIED |
| `dashboard/main.py` | GET | `/api/metrics` | None | None | success, forbidden, dependency failure | NOT VERIFIED |
| `dashboard/main.py` | GET | `/api/status-codes` | None | None | success, forbidden | NOT VERIFIED |
| `dashboard/main.py` | POST | `/api/events/{service}` | tests/unit/test_dashboard_metaguard.py::test_enforcement_rejects_direct_posts_before_forwarding[admin-patient_discharge]; tests/unit/test_dashboard_metaguard.py::test_enforcement_rejects_direct_posts_before_forwarding[emergency-emergency_record]; tests/unit/test_dashboard_metaguard.py::test_enforcement_rejects_direct_posts_before_forwarding[patient-patient_registration] | success, forbidden | invalid input, missing resource, dependency failure, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `dashboard/main.py` | GET | `/api/history` | None | None | success, forbidden, invalid input, dependency failure | NOT VERIFIED |
| `dashboard/main.py` | GET | `/api/hospitals` | None | None | success, forbidden, dependency failure | NOT VERIFIED |
| `dashboard/main.py` | GET | `/api/hospitals/{hospital}/scheduler/inspect` | None | None | success, forbidden, invalid input, missing resource, dependency failure | NOT VERIFIED |
| `dashboard/main.py` | POST | `/api/hospitals/{hospital}/capture` | tests/unit/test_dashboard_metaguard.py::test_enforcement_keeps_hospital_capture_available[hospital-a-Hospital_A]; tests/unit/test_dashboard_metaguard.py::test_enforcement_keeps_hospital_capture_available[hospital-b-Hospital_B]; tests/unit/test_dashboard_offline.py::test_edge_capture_routes_to_selected_hospital[hospital-a-Hospital_A] | success | forbidden, invalid input, missing resource, dependency failure, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `dashboard/main.py` | POST | `/api/hospitals/{hospital}/replay` | tests/integration/test_manual_replay_security.py::test_dashboard_forwards_server_token; tests/unit/test_dashboard_replay_authorization.py::test_authorized_replay_uses_server_side_hospital_credential[hospital-a]; tests/unit/test_dashboard_replay_authorization.py::test_authorized_replay_uses_server_side_hospital_credential[hospital-b] | success, missing resource, forbidden, dependency failure | invalid input, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `dashboard/main.py` | GET | `/api/recovery` | tests/unit/test_dashboard_recovery.py::test_monitoring_obeys_local_dashboard_access_checks[headers0]; tests/unit/test_dashboard_recovery.py::test_monitoring_obeys_local_dashboard_access_checks[headers1]; tests/unit/test_dashboard_recovery.py::test_monitoring_obeys_local_dashboard_access_checks[headers2] | forbidden | success, dependency failure | NOT VERIFIED |
| `dashboard/main.py` | GET | `/api/test-traffic` | tests/unit/test_dashboard_test_traffic.py::test_database_failure_is_sanitized_and_releases_lock; tests/unit/test_dashboard_test_traffic.py::test_default_disabled_and_ec2_veto; tests/unit/test_dashboard_test_traffic.py::test_disabled_and_production_fail_closed[APP_ENV-production] | success, missing resource | forbidden, dependency failure | NOT VERIFIED |
| `dashboard/main.py` | GET | `/api/demo-records` | tests/unit/test_demo_records.py::test_conflicting_local_id_prevents_any_capture; tests/unit/test_demo_records.py::test_preparation_disabled_outside_protected_local_stack[APP_ENV-staging]; tests/unit/test_demo_records.py::test_preparation_disabled_outside_protected_local_stack[AWS_REGION-test-region] | success, missing resource | forbidden, dependency failure | NOT VERIFIED |
| `dashboard/main.py` | POST | `/api/demo-records` | tests/unit/test_demo_records.py::test_ambiguous_capture_is_not_duplicated_on_repeat; tests/unit/test_demo_records.py::test_conflicting_local_id_prevents_any_capture; tests/unit/test_demo_records.py::test_preparation_disabled_outside_protected_local_stack[APP_ENV-staging] | success, missing resource, forbidden, conflict, dependency failure | invalid input, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `dashboard/main.py` | POST | `/api/test-traffic` | tests/unit/test_dashboard_test_traffic.py::test_bad_json_and_oversized_input; tests/unit/test_dashboard_test_traffic.py::test_bounded_strict_input_without_echo[body0]; tests/unit/test_dashboard_test_traffic.py::test_bounded_strict_input_without_echo[body1] | success, invalid input, missing resource, forbidden, conflict, dependency failure | concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `edge/capture_api/main.py` | GET | `/` | None | None | success | NOT VERIFIED |
| `edge/capture_api/main.py` | POST | `/capture` | tests/unit/test_capture_prerequisites.py::test_capture_preserves_well_formed_prerequisite_ids[prerequisites0]; tests/unit/test_capture_prerequisites.py::test_capture_preserves_well_formed_prerequisite_ids[prerequisites1]; tests/unit/test_capture_prerequisites.py::test_capture_preserves_well_formed_prerequisite_ids[prerequisites2] | success, invalid input, forbidden | conflict, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `edge/capture_api/main.py` | GET | `/events` | None | None | success | NOT VERIFIED |
| `edge/capture_api/main.py` | GET | `/scheduler/inspect` | tests/unit/test_capture_prerequisites.py::test_capture_preserves_well_formed_prerequisite_ids[prerequisites0]; tests/unit/test_capture_prerequisites.py::test_capture_preserves_well_formed_prerequisite_ids[prerequisites1]; tests/unit/test_capture_prerequisites.py::test_capture_preserves_well_formed_prerequisite_ids[prerequisites2] | success | invalid input | NOT VERIFIED |
| `edge/capture_api/main.py` | GET | `/recovery/status` | None | None | success | NOT VERIFIED |
| `edge/capture_api/main.py` | POST | `/replay` | tests/integration/test_manual_replay_security.py::test_hospital_authorization[0-test-secret-Bearer test-secret-404]; tests/integration/test_manual_replay_security.py::test_hospital_authorization[1---503]; tests/integration/test_manual_replay_security.py::test_hospital_authorization[1-test-secret--403] | success, missing resource, forbidden, dependency failure | concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `infrastructure/scripts/local_outage_agent.py` | GET | `/status` | None | None | success, invalid input, unauthorized, forbidden, dependency failure | NOT VERIFIED |
| `infrastructure/scripts/local_outage_agent.py` | POST | `/operation` | tests/unit/test_local_outage_agent.py::test_allowed_origins_and_sanitized_failure[http://127.0.0.1:8080]; tests/unit/test_local_outage_agent.py::test_allowed_origins_and_sanitized_failure[http://localhost:8080]; tests/unit/test_local_outage_agent.py::test_arbitrary_operations_refused[body0] | invalid input, forbidden, dependency failure | success, unauthorized, concurrency (not inferred from HTTP status) | NOT VERIFIED |
| `cloud/patient_service/main.py` | GET | `/metrics` | tests/unit/test_cloud_live_metrics.py::test_all_cloud_apps_expose_additive_fields_without_database_access[patient] | success | Assertion completeness review | NOT VERIFIED |
| `cloud/emergency_service/main.py` | GET | `/metrics` | tests/unit/test_cloud_live_metrics.py::test_all_cloud_apps_expose_additive_fields_without_database_access[emergency] | success | Assertion completeness review | NOT VERIFIED |
| `cloud/admin_service/main.py` | GET | `/metrics` | tests/unit/test_cloud_live_metrics.py::test_all_cloud_apps_expose_additive_fields_without_database_access[admin] | success | Assertion completeness review | NOT VERIFIED |

## Proposal requirement coverage matrix

Original GROUP09 proposal PDF and appendices are **not available in this checkout**. Requirement text below is the retained R01–R24 transcription in docs/reports/CORE_REQUIREMENTS_AUDIT.md, not a fresh verification against the original PDF. Every transcribed requirement is retained; complete authoritative proposal coverage remains NOT VERIFIED.

| Requirement (retained transcription) | Classification | Current interpretation / evidence |
|---|---|---|
| R01: Two sites capture durably and support local reads while disconnected (main section 4; A1) | Implemented but partially verified | Capture/persistence implemented; independent partitions remain open; source/tests: `edge/capture_api/main.py:capture_event,list_events`; `edge/database/db.py:save_event,get_events,init_db` / `test_capture_prerequisites.py`, `test_recovery_state.py`, `test_edge_routing_and_recovery.py`; disposable outage harnesses |
| R02: Event identity, site, priority, capture time and prerequisites; durable attempt metadata (section 4; C1) | Implemented but partially verified | Dedicated schema-version/workflow/hash fields differ from appendix inventory; source/tests: `edge/database/db.py:init_db,save_event`; `cloud/metaguard.py:event_digest`; recovery/attempt tables / Capture validation, digest and recovery-state tests |
| R03: Prerequisites must be centrally acknowledged before dependent selection (section 4; B1) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: `edge/scheduler/dag.py:DependencyDAG.get_eligible_events`; `edge/capture_api/scheduled_replay.py:replay_scheduled_events` / `test_scheduler.py:test_select_after_prerequisite_applied`; DAG/session/replay tests, multiple-parent cases |
| R04: Highest aged descendant urgency propagates to prerequisites, excluding blocked workflows (section 4; B1) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: `edge/scheduler/priority.py:calculate_effective_priorities`; `scheduler.py:select_next_event`; `grant_client.py:transmit` advertises effective priority / `test_priority.py` (diamond/multilevel/quarantine), `test_missing_prerequisite_inheritance.py`; `test_inheritance_comparison.py:test_known_order_and_completion_metrics`; `test_metaguard_replay.py:test_grant_registration_uses_inherited_priority_without_mutating_event` |
| R05: Age by `base + floor(wait/A)` and preserve waiting through restart (B1) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: `priority.py:calculate_aged_priority`; SQLite persisted `created_at` / `test_ageing.py` (7), `test_scheduler.py:test_inheritance_modes_with_ageing`; persistent recovery tests |
| R06: Deterministic tie-breaking: effective priority, oldest capture, ID; mixed priorities and multi-parent DAGs (B1) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: `scheduler.py:select_next_event.sort_key`; `session.py:SchedulingSession` / `test_scheduler.py:test_deterministic_selection_regardless_of_input_order`, randomized reference, diamond DAG, deep chains, session parity |
| R07: Missing prerequisites/cycles reported; permanent failures quarantine and block descendants (section 4; C2) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: DAG cycle/missing/blockage methods; `scheduled_replay.py:replay_scheduled_events`; `db.py:quarantine_event,get_quarantined_recovery_ids` / DAG, missing inheritance, scheduler, inspector and scheduled replay tests |
| R08: One best eligible candidate per site; update priority without resetting position/bypasses; withdraw ineligible site (B1/B2; C2) | Implemented but partially verified | Candidate replacement now implemented; durable tests optional; source/tests: `storage.py:add_ticket,update_priority`; `grant_client.py:outstanding_event,transmit`; scheduled replay pins an outstanding candidate / `test_controller.py` and PostgreSQL priority-update preservation tests; grant replay tests |
| R09: Bounded overrides with FIFO ties, B=0 round robin, at most N-1+B preceding grants for continuously eligible ticket (section 4; B2/B3) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: `cloud/controller/scheduler.py:ControllerScheduler.next_hospital`; production `storage.py:issue_grant` uses serialized durable queue / `test_controller.py:test_continuous_urgent_arrivals_cannot_starve_head`; `test_controller_postgres.py:test_bounded_priority_and_round_robin,test_admission_denial_preserves_bypass_fairness`; reliability sustained imbalance test |
| R10: Persist ticket mutation and grant in one transaction; redeliver same unexpired ID without another allocation (B2; C2) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: `storage.py:issue_grant,get_grant,latest_grant,_audit`; controller endpoints / PostgreSQL rollback/concurrent-allocation/idempotent lifecycle tests; unit grant manager is a separate in-memory implementation |
| R11: Adaptive rate halves on unhealthy signal and increases additively after H healthy windows (section 4; B3) | Implemented but partially verified | AIMD arithmetic verified; opt-in performance assessed separately; source/tests: `cloud/controller/admission.py:Settings,policy,effective_rate,assess,advance,reserve`; `storage.py:update_admission_metrics` / `test_admission.py`; durable pacing tests; `metaguard_admission_checks.py:verify_admission` |
| R12: Use live-request p95, queue pressure and errors against calibrated thresholds (section 4) | Implemented but partially verified | Live p95/queue telemetry now present; default admission still uses original signals; source/tests: `cloud/telemetry.py:Telemetry.snapshot`; `admission.py:assess` / Unit threshold/malformed/stale tests; real delay/error harness |
| R13: Missing/stale telemetry stops new grants (section 4; B3) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: `admission.py:load,reserve` / `test_controller_postgres.py:test_stale_telemetry_downgrades_even_without_poller` now requires no grant; new issue/ACK denial cases and live 7B stale-data checks |
| R14: Pace grants and cap in-flight work; expiry must not free an already-running request (section 4; C2) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: `admission.py:reserve` counts issued/acknowledged/reconciliation_required for issue; `storage.py:reconcile_expired`; `cloud/metaguard.py:fence_cloud_write` / Concurrent admission, ACK cap/rate, stale and rollback PostgreSQL cases; in-flight commit reconciliation race test |
| R15: Dependency check, deduplication and owning-service record effect atomically; conflicting ID errors (section 4; C2) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: `cloud/database/db.py:save_cloud_event`; owning service event endpoints; `cloud/metaguard.py:cloud_persist,fence_cloud_write` / `test_cloud_apis.py`, concurrency/cross-service tests; PostgreSQL reconciliation/duplicate-retry tests |
| R16: ACK loss and restart retain events, reconcile before retry, avoid duplicate concurrent attempts (section 4; C2) | Implemented but partially verified | Unit evidence for selected invariants; full distributed scope remains open; source/tests: `grant_client.py:_journal,transmit`; `db.py:claim_event,acknowledge_event`; `storage.py:reconcile_authoritative`; Cloud fencing / `test_metaguard_replay.py` lost-response/ambiguous/no-grant cases; `test_controller_postgres.py:test_reconciliation_waits_for_in_flight_cloud_commit`; fault-proxy SIGKILL/lost-response scenarios |
| R17: Transient retries use jittered exponential backoff and a fresh grant after known failure (C2) | Implemented but partially verified | Retry jitter now implemented/tested; sustained retry burst experiment omitted; source/tests: `db.py:record_failed_attempt`; `grant_client.py:transmit`; authoritative retirement / Recovery state/backoff/replay/reconciliation tests |
| R18: Authenticated site identity and own-record access (C1) | Implemented but partially verified | Protected mode and ownership regressions; TLS/production exposure not certified; source/tests: `cloud/metaguard.py:hospital_identity,cloud_persist`; controller `_owner,_operator`; hospital SITE_ID checks / Role/auth/digest/cross-hospital tests, real-storage security checks |
| R19: Both site-cloud links disconnected, capture continues, controlled reconnection, equal/skewed backlogs (section 5; D2) | Implemented but partially verified | Independent dual-link partitions while live traffic continues remain open; source/tests: `smoke_metaguard.py`, `metaguard_replay_checks.py`, `metaguard_reliability_checks.py`, fault proxy; AWS outage scripts / Existing disposable deployment/adaptive/reliability harnesses |
| R20: Same admission/retry/ageing/resources for RR +/-inheritance, unrestricted inheritance, bounded +/-inheritance, DRR, fixed/adaptive comparison (section 5; D1) | Implemented but partially verified | Policy implementations exist; calibrated full matrix incomplete; source/tests: `evaluation/experiments/compare_inheritance.py`, `run_inheritance_matrix.py`; production controller fixed bypass_limit default 1 / Evaluation oracle/matrix/analysis tests |
| R21: 16 workload conditions, >=5 paired seeds, live load 30%/70%, skew/depth controls, randomised policy order (D2) | Implemented but partially verified | Full D2 evaluation incomplete; source/tests: Workload generator; local matrix runner / Generator/matrix tests; retained inheritance-matrix01 summary files |
| R22: Measure urgent/routine p95 elapsed time, SLO violations, original drain, grant/completion/cost shares, retries, CPU/memory and incomplete work (section 5; D3) | Implemented but partially verified | Reduced H1/H2 and historical studies valid only in documented scope; source/tests: Local comparison runner; harness admission/reliability samples; per-event status / Evaluation analyzer consistency checks; functional smoke assertions |
| R23: Acceptance: zero duplicate effects/order/bound errors; >=10% urgent-p95 gain vs RR+inheritance; routine/drain <=20% worse; live SLO <=1 percentage point worse, paired uncertainty (D3) | Performance target not established | Urgent p95 improvement vs RR unestablished; see separate failed guardrails; source/tests: No complete acceptance evaluator for deployed baselines found / Correctness tests cover selected invariants; local position analyzer cannot evaluate these time/SLO targets |
| R24: Three-host AWS Compose/EBS persistence; optional Kubernetes/broker/controller replication excluded (A3; C3) | Implemented but partially verified | AWS implementation and historical evidence; no fresh remote verification; source/tests: `infrastructure/aws/compose.*`, launcher/runbooks and opt-in MetaGuard overlays / Deployment-config tests; dated AWS JSON evidence |
| Original AIMD recovery/routine regression guardrail | Performance target failed | docs/archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md: reduced H1/H2 drain +278.52% / +276.34%; original findings preserved |
| Live SLO <=+1 percentage point in reduced H1/H2 | Performance target achieved | Only the documented reduced study; docs/reports/AIMD_RECOVERY_DIAGNOSIS_20261101.md explains startup/exposure limitations |
| Urgent p95 >=10% gain against RR | Performance target not established | No qualifying RR/skewed comparison |
| Full D2 matrix / final D3 acceptance | Performance target not established | Full D2 incomplete; tests cannot substitute for experiments |
| Experimental pressure-health / bounded-deferral pilots | Implemented but partially verified | Improvements in some measurements; opt-in, separate pilot scope, no acceptance promotion |
| R02 appendix dedicated schema-version, enforced workflow-ID and payload-hash columns | Not implemented | Metadata may be supplied in payload; grant binding uses a digest. This differs from a dedicated persisted schema field contract |

Current requirements audit: docs/reports/CORE_REQUIREMENTS_AUDIT.md. Reduced H1/H2 and historical scheduling/cost evidence remain valid within their documented methods, provenance, exclusions and uncertainty. No historical file was overwritten.

## Distributed recovery evidence

| Scenario | Unit / mocked evidence | Historical Docker / AWS reference under docs/evidence | Fresh evidence limitation |
|---|---|---|---|
| Hospital A / Hospital B / both disconnected | test_edge_routing_and_recovery.py; test_dashboard_offline.py | CP7D_FINAL_VERIFICATION_20261031.md | Independent site-link partitions not established; isolated integration only if explicitly recorded in this run |
| Cloud unavailable; queue draining; dependency chain | test_scheduled_replay.py; test_metaguard_replay.py | CP8_MERGED_INTEGRATION_20261031.md | Fresh end-to-end replay deliberately omitted; isolated integration only if explicitly recorded in this run |
| Controller unavailable; admission denied; stale telemetry; state transitions | test_admission.py; test_metaguard_replay.py; test_controller_postgres.py | CP7B_AWS_ADAPTIVE_TRANSITIONS_20261029.md | Historical AWS scope only; no fresh AWS evidence; isolated integration only if explicitly recorded in this run |
| PostgreSQL unavailable | test_cloud_apis.py; test_controller_api.py | CP7D_CLOUD_API_CONTRACT_20261031.md | Mocked dependency errors; no live outage injection; isolated integration only if explicitly recorded in this run |
| Restart during replay; lost ACK; ambiguous response; duplicate replay | test_metaguard_replay.py; test_recovery_state.py; test_controller_postgres.py | CP8_MERGED_INTEGRATION_20261031.md | No fresh SIGKILL/network race experiment; isolated integration only if explicitly recorded in this run |
| Missing prerequisite; retry jitter | test_missing_prerequisite_inheritance.py; test_recovery_state.py | CP7D_FINAL_VERIFICATION_20261031.md | Unit evidence does not prove live timing distributions; isolated integration only if explicitly recorded in this run |

## Important gaps and limitations

- Authentication and replay regressions cannot establish full SQL fencing/duplicate-domain effects without real isolated transactions.
- Durable storage, advisory-lock ordering, concurrent grants, lost ACK/commit reconciliation, DRR rollback and restart behavior need isolated PostgreSQL evidence.
- Missing branches in infrastructure smoke/update/backup and live evaluation runners cannot safely be exercised by executing those entrypoints automatically.
- No browser-level keyboard, screen-reader, responsive-layout, console, real-network or hidden-tab verification occurred.
- No fresh Docker startup, AWS, outage injection, performance runs or final hosted CI. Docker opt-in reports read-only observations only.
- API status observations do not establish every invalid-credential variant, secret non-exposure or true concurrent behavior; focused assertions remain in source tests.
- Metrics database failures return HTTP 200 with an unhealthy flag; dashboard proxy failures/conflicts can be embedded in envelopes. Status-only route evidence does not establish these semantics.
- Profiling does not measure subprocess functions. Missing line/branch instrumentation is never replaced by fabricated percentages.

## Reproduction

Run `.\test.project.ps1 -Quick`, then `.\test.project.ps1 -Full`. Optional `-WithDocker` performs read-only checks. Optional `-WithIntegration` requires the explicit isolated PostgreSQL proof described in docs/testing.md.
Full/default runs generate unique machine-readable summary/JUnit, function inventory, route matrix, raw V8 and optional coverage.py JSON/XML/HTML. This report is a current audit snapshot; historical evidence is preserved.
