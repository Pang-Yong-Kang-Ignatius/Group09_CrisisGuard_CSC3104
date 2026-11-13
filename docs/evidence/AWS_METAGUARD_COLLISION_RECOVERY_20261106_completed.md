# AWS MetaGuard collision recovery: completed verification

Date: 2026-11-06 (Asia/Singapore). Account `565337149326`, region `us-east-1`,
stack `cg-prof-20261106-c64320`, deployed application commit `15a7996`.

Earlier attempts stopped without recovery writes. The first lacked AWS credentials
and SSH reachability. The next confirmed the account and hosts but found the
backup helper absent from the deployed archive, so no backup or recovery ran.
Packaging had omitted `database_backup.py`; the committed helper was transferred
byte-for-byte, verified on all three hosts, and then the three backup and
isolated restore checks in the preflight record passed. These blocked attempts
are not counted as successful recovery verification. Their original records
belong to the earlier development history, which is not included in this
submission repository. Those records are unavailable here; this retained summary
preserves their scope and outcome.

## Gates before writes

- The operator confirmed dashboard submissions, test generators, and external
  capture clients were paused for the recovery.
- The AWS dashboard test-traffic endpoint was disabled. No known test-generator
  process was found on the three hosts or the operator's Windows machine.
- Two read-only observations 20 seconds apart matched: Hospital A had the exact
  six original events, five pending dependency lists, local grant binding and
  digest; Hospital B had three applied local events; Cloud had four applied
  events with `CG-AWS-DEP-REG-01` owned by Hospital B. No capture POST was logged
  by either hospital or the dashboard during the observation interval.
- All three previously verified backup and isolated restore checks passed before
  the recovery writes. The original host and container identities, exact named
  volume mounts, and installed backup-helper SHA-256 were checked. Hospital A's
  collision grant was still `reconciliation_required`; Cloud still held the
  colliding ID for Hospital B. The retry count of 156 was observational, not a
  fixed precondition.

| Isolated backup restore | Bytes | SHA-256 | Verified contents |
|---|---:|---|---|
| Hospital A | 28,672 | `a15a1cb851d0cf0e226f893bc40f09b5b37bfc6071d351ca0daa9b428999f496` | 6 events, 1 applied, 5 pending |
| Hospital B | 28,672 | `5dc0a7f9af6c6d1f002671638bb61f431c91804801b807e57f604fa25c5e2845` | 3 events, all applied |
| Cloud | 11,500 | `dbc65ee8de43177eba65312e451164df7a29370e80902688c3ff3768df55cc95` | 4 events, all applied; collision ID once |

Copied backups matched host hashes. SQLite restores used disposable files; the
Cloud restore used an isolated container with no network or persistent volume.
The preflight stopped before recovery writes and had not independently confirmed
the client-submission pause. That pause and the live preconditions were checked
again before the controlled recovery below. Private backup archives and their
manifest remain outside Git; these hashes do not make them available in the ZIP.

## Controlled changes

1. Stopped only the original Hospital A application container and verified it
   retained the original `crisisguard-aws-hospital-a_hospital-data` volume.
2. Used the authenticated Controller operator route to resolve only grant
   `ca62e903-0a4c-4bdb-81d9-c50c4f042849` as **not applied for Hospital A**.
   The route returned `expired`.
3. While Hospital A remained stopped, changed only the collision event's
   `recovery_state.quarantined` and `quarantine_reason` fields under exact
   event/grant/phase/in-flight guards. The event stayed `pending`; its original
   `sent` grant journal stayed intact. Four unrelated events remained replayable.
4. Restarted the same Hospital A container with its original image and volume.

## Read-only final verification

| Check | Result |
|---|---|
| Hospital A queue | PASS: 5 applied; only `CG-AWS-DEP-REG-01` pending and quarantined; replayable count 0; no outstanding MetaGuard event |
| Four unrelated events in Cloud | PASS: exactly one applied Hospital A row per ID, four completed Hospital A grants |
| Dependency order | PASS: each registration applied before its dependent encounter |
| Hospital B collision row | PASS: remains applied for Hospital B, with unchanged immutable digest `4b1c8f2a78aec39ab3e1c03b48eeb43fce89e0b602f651ab8f079bdcd85fd2f8` |
| Controller audit | PASS: one `reconciliation_required -> expired` operator-resolution record; subsequent `expired -> superseded` candidate-change record |
| Existing infrastructure | PASS: original Hospital A/B and Cloud PostgreSQL/Controller container/image/volume identities retained; all eight stack containers running healthy |
| Backups | PASS: all three original host backup SHA-256 values still matched; private off-host copies remain preserved |

Cloud event count increased from 4 to 8, matching the four unrelated Hospital A
events. The original A grant's final status is `superseded` because normal
candidate advertisement retired the previously resolved `expired` grant; the
authoritative resolution and later transition are both retained in the audit.

No event ID or payload was renamed or overwritten. No Docker volume, EC2
instance, application image, or Cloud application code was replaced. The
uncommitted grant-client patch was not deployed. No commit, push, or merge was
performed. No credentials, tokens, keys, private URLs, or event payloads are
included in this evidence.
