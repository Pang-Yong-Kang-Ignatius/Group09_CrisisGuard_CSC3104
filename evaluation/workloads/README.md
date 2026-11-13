# CrisisGuard synthetic workload generator

Haris owns workloads and evaluation. This generator creates deterministic offline
backlogs for two hospitals. It uses Python's standard library and writes JSON only.
It does not call APIs, change databases, start services or collect performance results.
The generator, associated tests and this guide are already included in the
CrisisGuard repository. Run the commands below from the repository root.

From the CrisisGuard repository root in PowerShell:

```powershell
.\.venv311\Scripts\python.exe evaluation/workloads/generate_workload.py --run-id haris-demo01 --seed 3104 --total-events 88 --ratio 10 1 --depth 3 --urgent-distribution a_only --output evaluation/workloads/generated/demo01.json
```

Expected summary: 88 events (80 A, 8 B), 22 workflows, 4 urgent workflows at A,
all workflows with depth 3. A SHA256 fingerprint is printed. The output path must
not already exist; choose a new filename for another sample.

## Configuration and rounding

| Option | Meaning |
|---|---|
| `--run-id` | Required experiment namespace; use a new value for independent captures |
| `--seed` | Default 3104; controls urgency placement and routine priorities |
| `--total-events` | Total original backlog across both hospitals, not per hospital |
| `--ratio A B` | Positive integer backlog weights; use `1 1` or `10 1` |
| `--depth` | Maximum dependency edges per workflow; 3 means up to 4 events |
| `--urgent-fraction` | Fraction of workflows whose terminal event is urgent; default 0.2 |
| `--urgent-distribution` | `balanced`: proportional to each site's workflow count; `a_only`: all urgent workflows at A |
| `--snapshot-at` | Fixed simulation reference time; default `2026-10-21T00:00:00+00:00` |
| `--capture-interval` | Seconds between events within each site; default 1; zero gives equal timestamps |
| `--output` | New JSON file; existing files are never overwritten |

Backlogs use largest-remainder rounding; A wins exact ties. Both sites must receive
at least one event. Chains fill each site's budget, with a shorter final chain if
necessary; actual depth counts are reported. For exact depth 3 throughout, choose
counts divisible by 4 (e.g. total 88 gives 80:8 or 44:44).

Urgent workflow count is `floor(workflow_count * urgent_fraction + 0.5)`.
The global count stays the same when switching urgent distribution. Balanced means
equal approximate urgent *fractions*, not equal absolute counts under a 10:1
backlog. A-only fails if A has insufficient workflow capacity. Small workloads
may round to zero urgent workflows; inspect the summary. Depth changes workflow
count and therefore urgent terminal count: report both and freeze the intended
urgency definition before final comparison experiments.

## Output contract

The JSON contains configuration, actual-count summary, `events`, and `workflows`.
Events contain the six existing API fields plus scheduler `status` and `created_at`.
Workflow metadata lists the required events and final completion event, allowing
later runners to measure workflow completion and retain the original denominator.
IDs are deterministic UUIDs namespaced by run ID, seed and workload parameters;
there is no policy name or wall-clock generation timestamp in the input identity.

Urgent terminal events have priority 5; prerequisites and routine work have
priorities 1-3. Dependencies are acyclic, stay within a site and refer to events in
the same backlog. Event lists are in capture order per site. A workflow is a
synthetic event chain, not a clinically validated treatment sequence. Depth 0
creates independent emergency or registration events; longer chains can repeat encounter
creation. This matches the current event ledger's type/dependency contract only.

To use with Bettina's scheduler:

```python
import json
from datetime import datetime
from edge.scheduler.scheduler import select_next_event

with open("evaluation/workloads/generated/demo01.json", encoding="utf-8") as f:
    workload = json.load(f)
events = [e for e in workload["events"] if e["site_id"] == "Hospital_A"]
selected = select_next_event(
    events, applied_ids=set(),
    now=datetime.fromisoformat(workload["config"]["snapshot_at"]),
    enable_inheritance=True,
)
```

For paired policies, reuse the exact input JSON and isolated clean application
state. Change the run ID for independent captures into an existing database.
For long-term reproducibility, retain the JSON, hash, generator version and Python
version rather than relying on a seed alone.

## Integration requirements

- `capture_request(event)` returns only the six fields accepted by capture/cloud
  APIs. The bundle itself is not an API request.
- The current SQLite `save_event()` assigns the real current creation timestamp.
  Posting a generated event will not preserve its simulated `created_at`. A future
  runner must record actual capture times or coordinate an explicit timestamp
  import contract. Use the fixed snapshot only in scheduler simulations.
- Route event types to Patient/Emergency/Admin according to the
  [Cloud API contract](../../docs/CLOUD_API_CONTRACT.md). This offline generator
  does not submit or route events; API routing must be verified separately.
- Controller grants, bounded bypass, live traffic, event-cost variation and
  network fault injection are outside this generator's scope. For the separate
  system-level studies and their limitations, see the
  [evidence index](../../docs/EVIDENCE_INDEX.md). No speedup or recovery result
  is established by generating a workload.

## Validation

```powershell
.\.venv311\Scripts\python.exe -m pytest tests/unit/test_workload_generator.py -q
```

Tests cover byte reproducibility, namespaces, exact backlog sizes, urgency counts,
remainder workflows, invalid configurations, corrupt graphs and complete drains
through the existing scheduler with inheritance on and off for both sites.
