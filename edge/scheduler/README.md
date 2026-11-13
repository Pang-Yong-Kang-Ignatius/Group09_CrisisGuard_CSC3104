
# CrisisGuard Dependency Scheduler & Priority Inheritance

## Overview

This module provides dependency-aware event scheduling for CrisisGuard's coordinated post-partition recovery system. It determines **which locally captured event should be scheduled next**, taking into account prerequisite acknowledgements, event urgency, waiting time, and event availability.

Its responsibilities are:

- Constructing and validating event dependency graphs (DAGs).
- Identifying events eligible for scheduling.
- Detecting dependency cycles, self-dependencies, and missing prerequisites.
- Calculating time-aged priorities and dependency-priority inheritance.
- Selecting events deterministically using priority, creation time, and event ID.
- Supporting inheritance **ON/OFF** for controlled comparisons.
- Providing a reusable scheduling session for repeated selections.
- Validating event records and scheduling arguments before selection.

**Scope boundary:** This module does not assign initial priorities, transmit events to the cloud, acknowledge cloud application, persist recovery state, perform resource admission, or enforce inter-site fairness. Those functions belong to other CrisisGuard components.

## Active replay policy

Hospital replay and the live Inspector share validated process settings:

| Environment setting | Default | Accepted values |
|---|---|---|
| `SCHEDULER_ENABLE_INHERITANCE` | `1` | `1`, `0`, `true`, `false` (case-insensitive) |
| `SCHEDULER_AGEING_INTERVAL_SECONDS` | `600` | Positive, finite numeric seconds, including fractional intervals |

The root local Compose and split hospital Compose pass both settings to hospital
containers; AWS hospital overlays inherit that mapping. Set values in the private
env file used by the actual deployment. Recreate the hospital container using its
complete Compose file set and a reviewed image containing this code. Editing an
env file or refreshing the browser does not change an existing container's policy.
This source review does not establish which image or scheduler settings are
currently deployed on AWS. Verify the actual image and active policy before
relying on these features in a running deployment.

Defaults preserve inheritance ON with ten-minute ageing. Inheritance OFF still
enforces dependencies and ranks by each event's own aged priority; it is not FIFO
or ageing OFF. Invalid settings reject hospital startup before database
initialization, and reject direct replay before work is read, claimed or sent.
Each replay invocation retains one settings snapshot for its drain.

`GET /scheduler/inspect` without an `inheritance` query uses the active replay
policy. Explicit `?inheritance=true` or `false` requests a read-only preview and
does not change replay. The response includes `replay_policy`,
`ageing_interval_seconds`, and `comparison_only` (true only when the preview
differs from active inheritance). The Dashboard defaults to **Active replay
policy** and displays the active inheritance/ageing settings separately from
preview inheritance. Grant reconciliation and outstanding-attempt restrictions
continue to apply in either policy.

Direct priority helpers also reject nonfinite/boolean ageing intervals and
invalid evaluation times, even for an empty graph or an event without a creation
timestamp. Creation timestamps remain optional; without one the event does not age.


Experimental replay uses this same settings object. `REPLAY_SELECTION_POLICY`
accepts `priority` (default) or `fifo`; FIFO requires
`CRISISGUARD_EXPERIMENTAL_POLICIES=1`. Optional `REPLAY_INHERITANCE=0/1`
overrides `SCHEDULER_ENABLE_INHERITANCE`; setting it to `0`, or overriding a
different configured inheritance value, also requires explicit opt-in. Without
that alias, Bettina's configured inheritance is used. Ageing always comes from
`SCHEDULER_AGEING_INTERVAL_SECONDS`; invalid base settings are rejected even when
an experimental override is present. Existing experiment runners inject these
aliases into their private Compose config; normal deployment mappings are unchanged.

Replay, the default Inspector and selection logs share the resolved policy,
including `selection_policy` in `replay_policy`/`scheduler_policy`. FIFO only
changes ranking among dependency-eligible events. Effective-priority reporting,
retry/quarantine checks and uncertain-grant pinning remain active. Explicit
Inspector inheritance comparisons remain read-only and retain the active ranking.

## Module Structure

| File | Responsibility |
|---|---|
| `dag.py` | Dependency graph construction, eligibility, cycle and missing-prerequisite diagnostics, and blockage propagation |
| `priority.py` | Priority ageing and dependency-priority inheritance |
| `scheduler.py` | Stateless `select_next_event()` selection |
| `session.py` | `SchedulingSession`, which reuses a dependency graph across selections |
| `validation.py` | Event-record, event-collection, and scheduler-argument validation |

## Event Fields

Each event must contain `event_id`, `base_priority`, `prerequisite_ids`, and `status`. The `created_at` field is optional.

| Field | Meaning |
|---|---|
| `event_id` | Unique event identifier |
| `base_priority` | Integer initial priority supplied by the calling component (not a boolean) |
| `prerequisite_ids` | IDs of events that must be confirmed applied first |
| `status` | Event state: `pending` or `applied` |
| `created_at` | Optional timezone-aware ISO 8601 timestamp or Python `datetime`, or `None` |

Higher numerical priority values represent greater urgency. The capture and replay components may use additional fields, such as `site_id`, `event_type`, and `payload`, but these are not used to rank events in this module.

The caller must supply `applied_ids`: the IDs **confirmed applied centrally**. Merely capturing an event or observing its presence in a local database does not satisfy its prerequisites.

## Input Validation

Both scheduling implementations validate inputs before selecting an event. Invalid records or arguments are rejected rather than silently repaired.

### Event Record Validation

| Input | Validation |
|---|---|
| `events` | Must be an iterable of event dictionaries, such as a list, tuple, or generator |
| `event_id` | Must be a nonempty string |
| `base_priority` | Must be an integer; boolean values are rejected |
| `prerequisite_ids` | Must be a list of nonempty string IDs without duplicates |
| `status` | Must be `pending` or `applied` |
| `created_at` | Optional; accepts `None`, timezone-aware Python `datetime`, or timezone-aware ISO 8601 string |
| Duplicate event IDs | Rejected during scheduler/DAG construction |
| Missing required fields | Rejected with `ValueError` |

Empty event collections are valid and result in no event being selected.

Strings, byte sequences, mappings, non-iterable values, and collections containing invalid event records are rejected.

**Missing prerequisite IDs are not treated as malformed records.** The scheduler identifies them through the DAG and prevents affected events from being selected until their prerequisites are confirmed applied.

### Scheduler Parameter Validation

| Parameter | Validation |
|---|---|
| `ageing_interval_seconds` | Must be positive, finite, and numeric; boolean values are rejected |
| `enable_inheritance` | Must be the boolean `True` or `False` |
| `applied_ids` | Must be a collection of nonempty string IDs |
| `in_flight_ids` | Must be `None` or a collection of nonempty string IDs |
| `permanently_blocked_ids` | Must be `None` or a collection of nonempty string IDs |
| `now` | Must be `None` or a timezone-aware Python `datetime` |
| `retry_after` | Must be `None` or a mapping from event IDs to timezone-aware Python `datetime` deadlines or `None` |

For example, `enable_inheritance="False"` is rejected because it is a string, not a boolean.

### Timestamp Handling

The event's `created_at` value may be an ISO 8601 string, such as:

```text
2026-10-21T12:00:00+00:00
```

The scheduler also accepts a timezone-aware Python `datetime` object or `None`.

However, the evaluation timestamp `now` and values supplied through `retry_after` must already be timezone-aware Python `datetime` objects.

The calling component must convert any stored database timestamp strings before passing them as retry deadlines.

### Base-Priority Range

The cloud-facing event API defines a base-priority range of **1–5**.

The core scheduler validates that `base_priority` is an integer but does not independently restrict it to 1–5. This allows scheduler evaluation tests to use a wider range of priorities.

The event-creation boundary is responsible for enforcing the external priority contract for real events.

**Aged and inherited effective priorities may legitimately exceed 5**, even when an event's initial base priority is between 1 and 5.

### Validation Responsibilities

- The stateless scheduler validates event records and scheduling arguments on every call.
- `SchedulingSession` validates its event snapshot during construction.
- `SchedulingSession.select_next()` validates changing scheduling arguments on every selection.
- Invalid inputs raise clear errors in the tested validation scenarios.
- Validation does not modify event records, assign priorities, or resolve missing dependencies.

## Dependency Eligibility

An event is eligible for selection only when:

1. Its status is `pending` and its ID is not in `applied_ids`.
2. **Every** prerequisite ID appears in `applied_ids`.
3. It is not already in flight.
4. Any configured retry deadline has elapsed.
5. It is not part of a dependency cycle or a descendant blocked by a cycle.
6. It is not permanently blocked or dependent on a permanently blocked event.

An unresolved missing prerequisite makes the dependent event ineligible, but **does not automatically quarantine it**. Unrelated eligible events can continue to be scheduled even when another workflow is blocked.

In-flight states and retry deadlines affect **eligibility**, rather than permanently changing event priorities.

Already-applied events also do not act as intermediate links that incorrectly propagate missing-prerequisite exclusions or permanent blockage into unfinished workflows.

## Priority Ageing

The scheduler increases an event's urgency as it waits:

```text
aged_priority = base_priority + floor(waiting_seconds / ageing_interval_seconds)
```

- The default interval is **600 seconds (10 minutes)**.
- Waiting time is clamped to zero when an event has a future creation timestamp.
- An event with `created_at=None` retains its base priority instead of ageing.
- Timestamps used in ageing must be timezone-aware.
- Invalid ageing intervals are rejected during input validation.

Example: An event with base priority 1 that has waited 25 minutes has aged priority **3** under the default interval.

## Dependency-Priority Inheritance

When inheritance is enabled, an unfinished event receives the maximum aged priority of itself and its unfinished, healthy descendants:

```text
effective_priority = max(
    own_aged_priority,
    effective_priorities_of_healthy_unfinished_descendants
)
```

This lets an urgent dependent event increase the priority of the prerequisites that must be completed before it can run.

### Which Descendants Contribute Urgency?

| Descendant state | Contributes inherited urgency? | Explanation |
|---|---|---|
| Pending with a valid dependency workflow | Yes | Its urgency may propagate to unfinished prerequisites |
| Temporarily in flight | Yes | Temporary unavailability affects selection, not inheritance |
| Waiting for a retry deadline | Yes | The workflow may still be urgent while retry is delayed |
| Confirmed applied | No | Its work is complete |
| Permanently blocked or quarantined | No | It cannot make forward progress |
| In a cycle or dependent on a cycle | No | The affected workflow is excluded |
| Has an unresolved missing prerequisite | No | The event and its descendants are excluded from inheritance while that prerequisite remains unresolved |

### Missing-Prerequisite Inheritance Rule

An event with an unresolved missing prerequisite, **and its unfinished descendants**, is excluded from contributing inherited urgency to other prerequisites.

This is a **temporary scheduling exclusion**, not permanent quarantine.

If the missing prerequisite is later confirmed applied, inheritance can be restored on the next scheduling decision.

For example:

```text
ROOT (priority 1) --> CHILD (priority 9)
                           ^
                           |
                     MISSING prerequisite

OTHER (priority 3, independent)
```

While the prerequisite is unresolved, `CHILD` must **not** boost `ROOT` to priority 9.

With ageing held constant, `OTHER` (priority 3) is selected ahead of `ROOT` (priority 1).

Once the missing prerequisite is confirmed applied, `ROOT` can inherit `CHILD`'s priority 9.

The `enable_inheritance=False` option uses each eligible event's **own aged priority** instead of inherited priority.

## Deterministic Event Selection

Among eligible events, the scheduler selects in this order:

1. **Highest effective priority**.
2. **Earliest creation timestamp** when priorities are equal.
3. **Lexicographically smallest event ID** when both priority and timestamp are equal.

Given the same event snapshot, acknowledgement state, unavailability state, and evaluation time, selection is independent of the input list order.

### Fairness and starvation assumptions

Ageing raises the priority of events that have a valid creation timestamp, but it
does not by itself guarantee that every event will eventually be selected or
applied. A per-event progress claim would additionally require the event and its
prerequisite chain to remain eligible, finite retry delays, eventual prerequisite
acknowledgements, continued controller grants and service progress, and a workload
whose sustained arrivals do not exceed service capacity. Events without a
creation timestamp do not age. Missing prerequisites, cycles, permanent failures,
unavailable services, or admission pauses can prevent progress regardless of
priority.

Inheritance can raise a prerequisite's ranking for urgent unfinished descendants;
it does not bypass dependency checks or set a completion-time bound. This local
scheduler ranks events within one supplied hospital snapshot. Inter-site turns,
bounded bypass, and their conditional grant-count bound belong to the controller.
That bound assumes continuously eligible site tickets and progressing allocation;
it is not a time, resource-share, or per-event completion guarantee.

## Cycle, Missing-Prerequisite, and Blockage Diagnostics

`DependencyDAG` provides the following diagnostics:

| Method | Purpose |
|---|---|
| `get_cycle_event_ids()` | Return only IDs that participate directly in cycles, including self-cycles |
| `get_cycle_blocked_ids()` | Return cycle members and descendants that depend on those cycles |
| `get_missing_prerequisites(applied_ids)` | Report prerequisite IDs absent from the snapshot and not confirmed applied |
| `get_permanently_blocked_ids(ids)` | Propagate supplied permanent-blockage IDs through dependent descendants in the given DAG |
| `get_eligible_events(...)` | Return pending events with satisfied prerequisites and no applicable temporary exclusion |

Graph processing is iterative to avoid recursion-depth limits on long dependency chains. Duplicate event IDs are rejected with `ValueError`.

**Important distinction:** A missing prerequisite is *unresolved*, not automatically *permanently blocked*. Both prevent the affected workflow from contributing inherited urgency under the current policy, but only explicitly supplied permanent-blockage IDs represent quarantined or permanently failed work.

## Supported Scenarios and Expected Behaviour

### A. Dependency Handling

| Scenario | Example | Expected behaviour |
|---|---|---|
| Independent events | A and B have no prerequisites | Both can be eligible; priority and tie-breakers determine selection |
| Single prerequisite | B depends on A | B cannot execute until A is confirmed applied |
| Multiple prerequisites | C depends on A and B | C becomes eligible only after both A and B are confirmed applied |
| Partially satisfied prerequisites | A applied, B pending; C depends on both | C remains ineligible |
| Multilevel chain | A -> B -> C | A must be applied before B, and B before C |
| Diamond dependency | A -> B and C; B and C -> D | D waits for both B and C |
| Missing prerequisite | B requires absent X | B remains ineligible and is reported by the DAG |
| Absent but already applied prerequisite | X absent from snapshot but in `applied_ids` | X is treated as satisfied |
| Dependency cycle | A <-> B | Both excluded from scheduling |
| Self-dependency | A depends on A | A excluded as cyclic |
| Descendant of a cycle | A <-> B -> C | C is blocked along with the cycle |
| Independent work beside a cycle | A <-> B; C independent | C may still proceed |
| Empty backlog | No events | Returns `None` |
| No eligible events | All pending events unavailable or blocked | Returns `None` |

### B. Priority and Inheritance

| Scenario | Expected behaviour |
|---|---|
| Different base priorities | Higher aged/effective priority wins among eligible events |
| Urgent dependent event | An unfinished prerequisite can inherit its dependent's aged urgency |
| Multilevel inheritance | Urgency propagates back through multiple prerequisites |
| Multiple descendants | The highest healthy descendant priority is inherited |
| Inheritance OFF | Events are ranked by their own aged priorities only |
| Ageing interval boundary | Priority increases once for each completed ageing interval |
| Future creation timestamp | No negative waiting-time penalty |
| Missing creation timestamp | Base priority retained |
| Permanently blocked descendant | No inherited urgency from the blocked workflow |
| Unresolved missing-prerequisite descendant | No inherited urgency until the prerequisite is resolved |
| Missing prerequisite later confirmed applied | Affected inheritance can be restored |
| Temporarily unavailable descendant | May still contribute inherited urgency |
| Different evaluation times | Ageing and effective priorities are recalculated |

### C. Event Availability and Failure Handling

| Scenario | Expected behaviour |
|---|---|
| Confirmed applied event | Not selected again |
| Non-pending event | Not selected |
| In-flight event | Temporarily ineligible |
| Retry deadline in future | Temporarily ineligible |
| Retry deadline elapsed | Eligible again if all other conditions hold |
| Permanently blocked event | Excluded |
| Descendant of permanent failure | Also excluded |
| Permanently blocked prerequisite absent from snapshot | Blockage propagates through unfinished descendants |
| Independent event beside blocked workflow | Independent progress continues |
| Already-applied historical dependency | Does not incorrectly block unfinished work |

The scheduler does **not** decide when a cloud failure is permanent. The caller provides permanent-failure IDs through `permanently_blocked_ids`.

The replay/recovery component also maintains its own state; a quarantined event may still appear as `pending` in a local event listing if quarantine is stored separately.

### D. Ordering and Validation

| Scenario | Expected behaviour |
|---|---|
| Equal effective priorities | Earlier-created event wins |
| Equal priority and timestamp | Lexicographically smaller ID wins |
| Reversed event input order | Same result for equivalent snapshots |
| Duplicate event IDs | `ValueError` |
| Invalid event fields | `ValueError` |
| Invalid event container | `ValueError` |
| Invalid inheritance flag | `ValueError` |
| Non-positive ageing interval | `ValueError` |
| Timezone-naive evaluation timestamp | Rejected |
| Invalid retry-deadline format | Rejected |
| Deep dependency chain | Iterative processing avoids recursion-depth errors |

General HTTP request validation and initial priority assignment belong to the capture or calling components.

### Capture Boundary and Existing Malformed Records

The hospital `POST /capture` model uses the same
`validate_prerequisite_ids()` helper as scheduler event validation. Duplicate
prerequisite IDs, empty IDs, and whitespace-only IDs receive **HTTP 422 before
either the event or its recovery-state row is saved**. IDs are compared exactly;
nonempty IDs are not stripped, case-folded, or silently deduplicated.

This is structural validation only. Missing prerequisites, self-dependencies,
and multi-event cycles remain captureable so the scheduler can report and
isolate those workflows. A missing prerequisite may be captured later.

`tests/unit/test_capture_prerequisites.py` covers rejected captures leaving both
SQLite tables unchanged, a working read-only Inspector, and healthy inherited-
priority replay through a mocked cloud connection. It also covers accepted
missing/cyclic dependencies and exact ID preservation.

**Existing-record policy:** this prevents new malformed prerequisite lists from
entering through capture; it does not repair records already stored. The core
scheduler intentionally remains strict. An existing malformed record can still
stop selection, including when it is applied or quarantined, because record
validation runs before those exclusions. Quarantining alone is not a repair.

Before updating an affected database, pause capture/replay, take a verified
backup, and have the edge owner review the intended dependency IDs. Apply an
explicit reviewed correction using the team's database procedure, then rerun
the audit and scheduler tests. Do not automatically remove prerequisites,
rewrite IDs, delete events, or mark them applied to unblock recovery. If the
intended workflow is unknown, leave it unchanged and report the affected IDs.

With the existing hospital container running, this PowerShell audit reads the
database without changing it. It works with the existing event validator and
does not require the new capture code to have been deployed. Repeat with
`hospital-b` as needed:

```powershell
@'
import json
import sqlite3
from edge.database import db
from edge.scheduler.validation import validate_event_record

issues = 0
with sqlite3.connect(db.DB_PATH.as_uri() + "?mode=ro", uri=True) as conn:
    rows = conn.execute(
        "SELECT event_id, base_priority, prerequisite_ids, status, created_at FROM events"
    ).fetchall()
for event_id, priority, prerequisites, status, created_at in rows:
    try:
        validate_event_record({
            "event_id": event_id,
            "base_priority": priority,
            "prerequisite_ids": json.loads(prerequisites),
            "status": status,
            "created_at": created_at,
        })
    except (ValueError, TypeError) as error:
        issues += 1
        print(f"{event_id!r} ({status}): {error}")
print(f"Malformed records: {issues}")
raise SystemExit(1 if issues else 0)
'@ | docker compose exec -T hospital-a python -B -
```

Run the new regression tests from the repository root with your Python
environment containing `requirements.txt`:

```powershell
python -m pytest tests/unit/test_capture_prerequisites.py tests/unit/test_scheduler_validation.py tests/unit/test_edge_routing_and_recovery.py tests/unit/test_scheduled_replay.py tests/unit/test_scheduler_inspector_api.py -q
```

### Dashboard Dependency Capture Demo

The Hospital Edge Simulation form includes **Prerequisite IDs (comma separated)**.
For example, entering `ROOT-1, ROOT-2` sends `['ROOT-1', 'ROOT-2']` to the selected
hospital capture API. The form trims surrounding spaces and ignores empty
comma-separated entries; a blank field sends an empty list. Duplicate IDs are
kept so the API can reject them with HTTP 422. After a successful capture the
field clears for the next event; errors preserve the input for correction.

For a controlled local demo, first pause automatic replay on both hospitals.
With the normal Compose stack already running, these PowerShell commands
rebuild the edited dashboard and hospital code and disable automatic replay:

```powershell
$env:AUTO_REPLAY = "0"
docker compose up -d --build --no-deps dashboard hospital-a hospital-b
```

Open `http://localhost:8080` and refresh the page to load the edited form. In
Hospital Edge Simulation, select Hospital A and capture these three events in
order (use fresh IDs if they already exist):

| Event ID | Type | Priority | Prerequisite IDs |
|---|---|---:|---|
| DEMO-C | Patient Registration | 3 | Leave blank |
| DEMO-A | Patient Registration | 1 | Leave blank |
| DEMO-B | Patient Registration | 5 | DEMO-A |

Complete the captures within one ageing interval and use a queue without other
eligible events for the expected comparison. In Scheduler Inspector, select
Hospital A. Inheritance ON should select `DEMO-A` with effective priority 5
inherited from `DEMO-B`; OFF should select `DEMO-C`. `DEMO-B` must wait for
`DEMO-A` to be acknowledged. The Inspector toggle changes the preview policy;
live scheduled replay follows its configured policy (inheritance ON by default).

When finished, restore the normal automatic replay setting:

```powershell
$env:AUTO_REPLAY = "1"
docker compose up -d --no-deps hospital-a hospital-b
Remove-Item Env:AUTO_REPLAY
```

The dashboard payload/error-path checks run separately from pytest:

```powershell
node --test tests/unit/test_dashboard_capture.cjs
node tests/unit/test_dashboard_queue.cjs
```

### E. Reusable Scheduling Session

`SchedulingSession` constructs a deep-copied snapshot and dependency graph once, then supports repeated decisions as recovery state changes.

| Scenario | Expected behaviour |
|---|---|
| More IDs become applied | Re-evaluates eligibility and inherited urgency |
| In-flight IDs change | Uses the latest in-flight set |
| Retry deadlines change | Uses the latest retry information |
| Permanent-blockage IDs change | Re-evaluates blocked workflows |
| Missing prerequisite becomes applied | Restores affected inheritance when appropriate |
| Evaluation time changes | Recalculates ageing |
| Original input event dictionaries change | Session remains unaffected by external mutation |
| Caller modifies the returned event | Session snapshot remains unaffected |
| New events are captured | Construct a new session containing the updated event set |
| Event dependencies or base priorities change | Construct a new session from the updated event records |
| Applied event has a missing historical prerequisite | Does not suppress inherited urgency of healthy unfinished work |
| Permanent blockage reaches an applied intermediary | Does not continue propagation through that already-applied event |

### Applied-Event Propagation Rule

The stateless scheduler constructs its dependency graph from unfinished events, excluding IDs already present in `applied_ids`.

The reusable scheduler retains its original dependency graph, but must produce equivalent selection results as acknowledgement state changes.

To maintain this consistency:

- Missing-prerequisite checks consider unfinished event records.
- Missing-prerequisite exclusions propagate through unfinished descendants.
- Permanent blockage propagates through unfinished descendants rather than through already-applied intermediate events.
- Explicitly blocked prerequisites outside the snapshot are still considered when identifying blocked workflows.
- Centrally confirmed acknowledgements are supplied through `applied_ids`.

The reusable session caches external prerequisite-to-dependent relationships when it is constructed.

It then uses an active-event-aware blockage propagation step so that completed historical dependencies do not incorrectly suppress inherited priorities.

Two regression tests cover these cases:

1. An applied event with a missing historical prerequisite must not suppress urgency in an unfinished workflow.
2. Permanent blockage must not propagate through an already-applied intermediate event.

The existing scheduled-replay pathway uses **`select_next_event()`**, not `SchedulingSession`.

The reusable session is an **evaluated alternative**, not an optimization already integrated into live replay.

## Scheduler Interfaces

### Stateless Scheduler

```python
from edge.scheduler.scheduler import select_next_event

selected = select_next_event(
    events,
    applied_ids,
    now=None,
    ageing_interval_seconds=600,
    in_flight_ids=None,
    retry_after=None,
    permanently_blocked_ids=None,
    enable_inheritance=True,
    include_priority=False,
    selection_policy="priority",
)
```

- `events`: Iterable snapshot of event records.
- `applied_ids`: IDs confirmed applied centrally.
- `now`: Optional evaluation timestamp.
- `ageing_interval_seconds`: Positive ageing interval in seconds.
- `in_flight_ids`: IDs temporarily unavailable due to active processing.
- `retry_after`: Mapping of event ID to retry-eligible timestamp.
- `permanently_blocked_ids`: Explicitly blocked or quarantined event IDs.
- `enable_inheritance`: Boolean controlling dependency-priority inheritance.
- `include_priority`: When true, return a dictionary containing the selected event's fields plus `effective_priority`; defaults to false.
- `selection_policy`: `priority` (default) ranks by effective priority, creation time and event ID. `fifo` ranks eligible events by creation time and event ID while preserving dependency and availability checks. The process configuration requires explicit experimental opt-in for FIFO; the core function accepts it directly.

Returns the selected event dictionary, or `None` if no event is eligible.
A missing creation timestamp is treated as `now` for tie-breaking; the event
still retains its own base priority without ageing.

The function does **not** transmit or acknowledge the selected event.

### Reusable Scheduling Session

```python
from edge.scheduler.session import SchedulingSession

session = SchedulingSession(
    events,
    enable_inheritance=True,
    ageing_interval_seconds=600,
)

selected = session.select_next(
    applied_ids,
    now=None,
    in_flight_ids=None,
    retry_after=None,
    permanently_blocked_ids=None,
)
```

`SchedulingSession` keeps an independent event snapshot.

Build a new session when new events arrive or the snapshot's event fields or dependencies change.

Its returned events are also copies, so external mutation cannot alter the session's internal state.

As applied IDs and recovery conditions change, it recalculates eligibility and priorities while reusing the dependency graph.

## Complexity and Performance

A single dependency graph traversal or effective-priority propagation pass is **O(V + E)**, where `V` is the number of events and `E` is the number of dependency edges.

The stateless scheduler rebuilds graph structures and recalculates priorities on successive selections.

Consequently, repeatedly selecting every event from a backlog can incur substantial recomputation and exhibited **approximately quadratic growth** over the tested synthetic backlog sizes.

`SchedulingSession` reduces graph-rebuilding work but still recalculates state-dependent priorities.

### Benchmark Methodology

Synthetic workloads use **chain** and **shallow** dependency graphs.

Measurements isolate **scheduler CPU overhead** and simulate successful acknowledgement by adding selected IDs to `applied_ids`.

They do not send HTTP requests or apply events in cloud microservices.

| Script | Purpose |
|---|---|
| `tests/benchmarks/benchmark_scheduler.py` | Single next-event selection across 100–5,000 events, inheritance ON/OFF; median of five measurements |
| `tests/benchmarks/benchmark_full_drain.py` | Full backlog drain for 100, 500, and 1,000 events |
| `tests/benchmarks/benchmark_session.py` | Original versus reusable session, including session construction; median of three runs and exact selection-order comparison |
| `tests/benchmarks/benchmark_pruned_drain.py` | Original versus externally pruned applied-event lists |
| `tests/benchmarks/profile_scheduler.py` | cProfile breakdown of a 1,000-event chain drain with inheritance ON |

### Observed Results (Earlier Benchmark Runs)

**Single selection, 5,000 events:**

| Graph | Inheritance ON | Inheritance OFF |
|---|---:|---:|
| Chain | 10.720 ms | 4.109 ms |
| Shallow | 9.132 ms | 5.600 ms |

**Full drain, 1,000 events:**

| Graph | Inheritance ON | Inheritance OFF |
|---|---:|---:|
| Chain | 1,023.026 ms | 420.555 ms |
| Shallow | 956.088 ms | 574.848 ms |

**Reusable-session comparison, 1,000 events:**

| Graph | Inheritance | Stateless | Session | Speedup |
|---|---|---:|---:|---:|
| Chain | ON | 1,045.435 ms | 839.862 ms | **1.24x** |
| Chain | OFF | 425.892 ms | 371.480 ms | 1.15x |
| Shallow | ON | 973.547 ms | 789.911 ms | **1.23x** |
| Shallow | OFF | 588.189 ms | 552.614 ms | 1.06x |

All original and reusable-session scheduling orders matched in these benchmark scenarios.

**These timings were recorded before the later validation and applied-event corrections. Performance has not yet been rebenchmarked on the final implementation.**

**External pruning:** Observed speedups ranged from approximately **0.96x to 1.05x**, with no consistent benefit. The stateless scheduler already excludes applied events internally before constructing its graph.

**Profiling:** A 1,000-event chain drain with inheritance ON involved **500,500 calls to `calculate_aged_priority()`** and **2,000 dependency-graph constructions**.

Repeated inherited-priority calculations were a major contributor to processing time.

The profile reported 2.637 seconds **with profiler overhead**, which should not be compared directly with unprofiled benchmark results.

### Interpretation and Limitations

- These are **synthetic scheduler-only measurements**, not end-to-end cloud recovery timings.
- They exclude database reads/writes, HTTP communication, application processing, cloud capacity constraints, admission control, and inter-site coordination.
- Full-drain and pruning configurations were measured once per configuration; small differences may reflect runtime noise.
- The results show that reusable sessions can reduce overhead for the evaluated workloads, but do not establish a production-level performance guarantee or justify switching the replay path without further integration testing.

## Current verification and performance review — 4 November 2026

The selected scheduler is the implementation in branch `bettina` at commit
`58fef574bfca49e6de03b2ef64e786091e0ddd61`, using priority selection, inheritance
ON and 600-second ageing. Its scheduler executable source and focused tests are
unchanged from tested source revision `9fbc918be76cda2148fb01970e6e21a4a3c09aa7`.
See [Bettina's scheduler review](../../docs/reports/BETTINA_SCHEDULER_REVIEW_20261102.md)
for the historical approval scopes and final-policy follow-up.

The 4 November focused run returned **285 passed, 0 failed, one existing
Starlette/httpx deprecation warning** on Python 3.14.3. The earlier reported
284-test run is recorded separately in the review; these overlapping runs are
not added together. This verifies the selected functional scenarios, not the
full project's Python 3.11, integration, hosted-CI or deployment checks.

The [H5 holdout](../../docs/reports/H5_FINAL_POLICY_HOLDOUT_20261102.md)
compared bounded B=1 against RR with inheritance under matching AIMD/retry
settings. Hospital A urgent p95 changed by +0.38%/+3.32% at 36/84 RPS, so the
10% improvement evaluation target was not demonstrated. Hospital B routine p95
was mixed (+15.64%/-22.57%); Hospital B urgent p95 is undefined because the
A-only urgency workload contained no B urgent events. Routine p95 is a workflow
completion proxy after reconnection, not exact queue-only waiting.

H5's network-preservation guard failed: 40 trials passed functional validity
checks, but zero were strictly accepted. Its numerical effects remain post-hoc
diagnostic sensitivity evidence. The separate [W5 admission study and H5/W5
reconciliation](../../docs/reports/H5_W5_REVIEW_20261103.md) adds no matched RR
scheduler arms and does not clear H5's failure. **Scheduler evidence review is
complete; strict final-policy performance acceptance remains unestablished.**

## Testing

Unit tests are located in `tests/unit/`.

The dated counts below are historical results for their recorded revisions;
they are not current full-suite results or additional distinct tests to add to
the focused verification above.

**Local unit regression (27 October 2026): 729 passed, 0 failed** on Python 3.12
using `python -m pytest tests/unit -q`. This includes 12 new capture-prerequisite
cases; the focused capture/validation/recovery/Inspector run passed 164 tests.
One upstream TestClient deprecation warning was reported. These are local unit
results, not live-cloud or AWS verification. The earlier 204-test result was a
historical scheduler checkpoint.

Coverage includes:

- Dependency eligibility, multiple prerequisites, and multilevel and diamond graphs.
- Missing prerequisites, cycles, self-dependencies, and blocked descendants.
- Priority ageing, inherited priorities, and inheritance ON/OFF comparisons.
- Permanent blockage, quarantine propagation, and externally blocked prerequisites.
- In-flight exclusions, retry deadlines, and deterministic selection.
- Long chains, randomized scheduler/reference comparisons, and session snapshot isolation.
- Compatibility checks for replay and recovery-state components.
- Missing-prerequisite inheritance restoration when the missing prerequisite becomes applied.
- Event collection and event-record validation, including invalid field types and duplicate identifiers.
- Scheduler argument validation, including timestamps, retry deadlines, ageing intervals, and inheritance settings.
- Integration tests confirming both scheduler implementations enforce the validation rules.
- Two regression tests confirming that already-applied events do not incorrectly suppress inheritance or propagate permanent blockage.

Additional randomized comparisons between the two scheduler implementations were performed during review. These are separate from the reproducible pytest test count.

### Run All Tests

```bash
python -m pytest -q
```

### Run Scheduler Unit Tests

From the repository root, with the project dependencies installed in your
Python environment, run the focused scheduler/policy selection:

```bash
python -m pytest -q tests/unit/test_dag.py tests/unit/test_priority.py tests/unit/test_scheduler*.py tests/unit/test_cp8_policies.py
```

The exact local invocation, including the Python environment and plugin/cache
settings used for the recorded 285-test run, is retained in
[Bettina's review](../../docs/reports/BETTINA_SCHEDULER_REVIEW_20261102.md).
The full `tests/unit` suite also includes other components.

### Docker Testing

For the tested Windows and Docker workflow, local tests can be copied into the running `hospital-a` container and executed there:

```powershell
$container = (docker compose ps -q hospital-a).Trim()
docker cp ".\tests" "${container}:/tmp/crisisguard-tests"
docker compose exec -T hospital-a python -m pytest /tmp/crisisguard-tests/unit -v
```

**Docker note:** The container's installed scheduler code must match the local source under test.

Copying a source file into a running container affects that container only; rebuilding or recreating it can restore the code from its image.

Confirm the version before relying on test results.

### API and Integration Checks

Real event records were created through the Hospital A Swagger API.

Dependency ordering, cycles, missing prerequisites, and cloud-rejected events were checked through scheduled replay.

Priority ageing, temporary availability, deterministic tie-breaking, and certain inheritance-policy scenarios were also examined through **read-only scheduler calls** using API-created records.

A permanent cloud rejection was observed to quarantine the rejected event and block its dependent workflow while allowing independent work to proceed.

Recovery quarantine information is maintained separately from the local event's `status` field, so a quarantined record may still be displayed as `pending` by the event-list API.

These checks support functional integration readiness, but they are **not a substitute for full multi-site recovery experiments under realistic cloud load**.

## Important Limitations and Ownership Boundaries

### Candidate replacement, uncertainty and Inspector parity

Hospital replay now distinguishes **registered/definitely unsent** attempts from
`ack_pending`/`sent` (or unknown) phases. Registered work does not pin local
selection: an independent eligible event can compete while an earlier event
is in retry backoff. Dependencies still require confirmed applied IDs.

The authenticated `POST /hospitals/{hospital_id}/candidate` operation advertises
the current eligible event (ID, effective priority and immutable digest), or
withdraws an unsent candidate using `{}`. Waiting-ticket replacement preserves
the hospital's queue position and bypass count. When an old grant was already
issued, its replacement rejoins at the tail rather than claiming the consumed
position. Issued grants being replaced/withdrawn become terminal `superseded`
records, retained in the grant audit and never automatically requeued. The
SQLite event itself remains pending and can become eligible again later.

Replacement, allocation, Cloud persistence and ACK use the controller's shared
PostgreSQL advisory lock. If replacement wins, the old grant cannot ACK or
authorize a Cloud write. If ACK wins, replacement returns `pinned` without
mutating its grant/ticket. Acknowledged/reconciliation-required grants are never
withdrawn by this endpoint. A missing or mismatched authoritative grant does
not clear local uncertainty or authorize a resend. Only a matching controller-
fenced terminal grant can release an uncertain local attempt for retry.

The read-only Inspector uses the same local uncertain-attempt restriction as
replay. Its response retains `scheduler_next_event_id` and per-row
`dependency_eligible`, while `next_event_id`, `eligible` and `selected` reflect
the local replay restriction. `outstanding_event_id` and `replay_wait_reason`
explain a wait in the dashboard. No controller request, grant mutation or
database write occurs during inspection. Controller-side admission and
concurrent changes can still defer transmission. An explicit inheritance preview
does not alter the configured live replay policy. Omit the query to inspect the
active policy, or compare the returned `replay_policy` and `comparison_only` fields.

Replay emits JSON `scheduler.selection` and `scheduler.outcome` records through
Uvicorn's INFO logger. Selection records include the ID, base/effective
priority, prerequisite IDs and the active `scheduler_policy`; outcomes identify the local result. Payloads,
credentials and claim tokens are omitted. These distinguish actual selected
attempts from predictions and successful acknowledgements from mere selection.
They are process logs, not a durable distributed commit-order ledger.

Historical verification for the candidate-replacement change: 814 unit tests
on the then-updated main (806 in the
archive review copy), 61 real-PostgreSQL controller
integration tests, 49 JavaScript tests and the queue regression script passed.
The integration tests include 12 ACK/replacement races, waiting fairness,
withdrawal without stale requeue, and actual replay through controller/Cloud
FastAPI handlers plus isolated SQLite/PostgreSQL stores. Executed order was
`C, A, B`: C proceeded during A's retry delay; after A's delay was cleared, A
inherited B's urgency and B waited for A's applied acknowledgement. This is
isolated local functional evidence, not a throughput, starvation, AWS or
long-term fairness result. Existing lost-response/no-resend regressions remain.

**Rollout:** deploy/review the controller endpoint before updating hospitals.
An older controller without this endpoint causes the new client to fail closed
and retry, not bypass permission. Rebuild the dashboard to display the wait
explanation. Preserve databases, volumes and outstanding journals. At the time
of the historical candidate-replacement verification, deployment to the team's
shared services or AWS was not recorded. This README update does not verify
current deployment status.
A subsequent separate isolated running-stack demonstration passed 28 assertions,
including real grant replacement, the C/A/B execution trace and lost-response
reconciliation. The [running-stack record](../../docs/evidence/scheduler-policy-running-20261027.json)
retains the 28 historical assertions and exact observations.

| Functionality | Responsibility or limitation |
|---|---|
| Initial priority assignment | Supplied by the event-creation/capture component |
| Enforcing external base-priority range | Capture and cloud API contract |
| Fixing missing prerequisites or cycles | Scheduler reports/excludes affected workflows; it does not rewrite dependencies or create missing events |
| Determining permanent cloud failures | Calling replay/recovery component supplies blocked IDs |
| Retry/backoff scheduling | Calling component supplies retry deadlines; scheduler enforces eligibility |
| Transmission, acknowledgement, persistence, and deduplication | Handled by replay, cloud, and database components |
| Shared-cloud adaptive admission | Managed by the controller |
| Inter-site fairness and bounded bypass | Managed separately from local dependency ordering |
| Full end-to-end backlog recovery | Requires system-wide integration testing and evaluation |

The scheduler selects the next eligible event **within the supplied snapshot and state**.

It does not itself enforce hospital-to-hospital fairness, regulate cloud replay rate, or guarantee event completion time.
