# AWS hospital network outage test

## Recorded result — 23 October 2026

**Passed** on the original three EC2 hosts in Learner Lab, `us-east-1`.
Application commit: `ab76b88638f7a1ebaa50e45f866a62eb69a03390`.
The fault helper was an additional working-tree script copied to the hospital
hosts. [Raw evidence](../evidence/aws-outage-20261023.json) contains the API results,
recovery timestamps, packet counter, database counts and original-record checks.

| Check | Observed result |
|---|---|
| Before the fault | Six containers healthy; both hospitals reached all three cloud APIs; both original records intact |
| Hospital A partition | Its Docker-container traffic to the cloud private address on TCP 8001/8003/8004 was rejected by one EC2-host `DOCKER-USER` rule |
| Capture during partition | A accepted four events (201); all remained pending in SQLite, and cloud lookup returned 404 for all four |
| Hospital B control | B remained connected; four events automatically applied across Patient, Emergency and Admin during A's partition |
| Packet evidence | Rule counter reached 27 packets / 1,620 bytes at the recorded pending checkpoint |
| Reconnection | Rule removed, rollback timer cancelled; A automatically applied all four events without calling `/replay` |
| Dependencies | Registration → encounter → emergency → discharge; PostgreSQL application timestamps followed this order at each site |
| Duplicate safety | Eight identical retries returned 200 / `already_applied`; PostgreSQL retained 10 distinct applied rows: two originals plus eight new, five per site |
| End state | Both original records intact; all six containers healthy before stopping; fault rule absent and timer inactive |

The application observed disconnection at `14:48:20.900377Z`, reconnection at
`14:49:21.270202Z` and queue drain at `14:49:21.428454Z`. Its observed disconnected
interval was about 60.37 seconds; reconnection-to-drain was 0.158 seconds for this
four-event run. These are application timestamps from one correctness test,
not throughput, p95, fairness or recovery performance benchmarks.

This tests a real packet-filter partition on the AWS hospital host, scoped to
the application's cloud connections. It does not simulate a failed physical
link, an Availability Zone/region failure, lost acknowledgements or controller
grants. `REJECT` makes failures immediate; packet loss with timeouts is a separate
test. The five-minute emergency timer was installed and confirmed active; normal
cleanup removed the rule first, so automatic timer expiry was not exercised.

## Deployment and budget safeguards used

- Reused the original one `t3.small` cloud and two `t3.micro` hospitals; created
  no additional instances, volumes, snapshots, buckets or NAT gateways.
- Took fresh PostgreSQL/SQLite backups on each existing disk under
  `~/crisisguard-backups/20261023-pre-outage`. Source/settings copies and old-image
  tags support rollback. These new backups were exported but not restore-tested
  in this run; earlier restore evidence remains in [BACKUPS.md](../runbooks/BACKUPS.md).
- Deployed the committed archive with `deploy.sh`, preserving private env files,
  passwords, Compose project names and named volumes. Enabled `SITE_ID`, automatic
  replay and all three private service URLs at each hospital.
- Updated SSH only to the current operator's `/32` in the two security groups;
  added cloud TCP 8003/8004 only from the hospital security group. TCP 8001 already
  used that source. No API listener was opened publicly.
- Stopped all three original instances after verification and ended Learner Lab.
  Their disks and the synthetic test records remain for the next session. Stopped
  instances retain storage charges; the lab's displayed budget is delayed, so no
  exact dollar cost is claimed. Temporary local CLI credentials were removed.

## Repeat on your own deployment

Use only synthetic records. Start your own lab, check its remaining credit and
use existing hosts. Obtain their current public IPs; keep SSH restricted to your
current `/32`. Use the [deployment guide](../../infrastructure/aws/README.md) or
[Docker launcher](../runbooks/AWS_LAUNCHER.md) to establish the three hosts first.

1. Back up the three databases. Deploy the current committed application and
   ensure both hospitals have `AUTO_REPLAY=1`, the correct `SITE_ID`, and private
   Patient/Emergency/Admin URLs. Confirm six healthy containers, empty pending
   queues and all three service health checks. Do not run bootstrap over an
   existing deployment: preserve its database password and project names.
2. Copy `infrastructure/scripts/aws_outage_host.py` into the same repository path
   on each hospital. It uses Python's standard library, Docker, iptables and
   systemd on Ubuntu. It refuses to inject a fault into Hospital B, public cloud
   destinations, unexpected endpoint settings or an already-armed test. It
   requires Docker's iptables `DOCKER-USER` chain and one container network.
3. In SSH terminals on **both hospitals**, from `~/CrisisGuard`, set a **fresh**
   test identifier and your cloud private address:

   ```bash
   TEST_ID=CG_OUTAGE_YOUR_UNIQUE_RUN
   CLOUD_IP=YOUR_CLOUD_PRIVATE_IP
   sudo python3 infrastructure/scripts/aws_outage_host.py snapshot --test-id "$TEST_ID" --cloud-host "$CLOUD_IP"
   ```

4. On **Hospital A only**, arm the five-minute maximum fault and verify all three
   container-to-cloud probes become unreachable while local capture stays 200:

   ```bash
   sudo python3 infrastructure/scripts/aws_outage_host.py arm --test-id "$TEST_ID" --cloud-host "$CLOUD_IP"
   sudo python3 infrastructure/scripts/aws_outage_host.py snapshot --test-id "$TEST_ID" --cloud-host "$CLOUD_IP"
   ```

5. On **each hospital**, create one four-event dependency chain:

   ```bash
   sudo python3 infrastructure/scripts/aws_outage_host.py capture --test-id "$TEST_ID" --cloud-host "$CLOUD_IP"
   ```

   Within the five-minute window, take another snapshot on each host. Expect A's
   four pending records and B's four applied records. From **B**, run:

   ```bash
   sudo python3 infrastructure/scripts/aws_outage_host.py cloud-state --test-id "$TEST_ID" --cloud-host "$CLOUD_IP"
   ```

   Expect A=404 and B=200. Save output and UTC timestamps. A snapshot also records
   the exact fault rule's packet counter. Do not restart/recreate A's container
   while the rule is armed: the rule targets its current container IP.

6. On **A**, always restore promptly, including if a check fails:

   ```bash
   sudo python3 infrastructure/scripts/aws_outage_host.py restore --test-id "$TEST_ID" --cloud-host "$CLOUD_IP"
   ```

   It removes only its saved exact rule, verifies absence and cancels its timer.
   If SSH is lost, the already-scheduled timer removes that exact rule after five
   minutes. After reconnecting, run `restore` to verify and clear saved state.
   Never flush iptables or disable Docker's firewall.

7. Take snapshots until A is reachable and all four events are applied; do not
   invoke manual replay. Repeat `cloud-state` from B (all eight should be 200).
   On each host, run `duplicates` with the same arguments: all four must return
   200 / `already_applied`. Check PostgreSQL distinct/total counts against your
   baseline plus eight, and verify both dependency orders and original records.
8. Save your own evidence, stop the three EC2 instances, confirm Stopped, end
   Learner Lab, and remove temporary local credentials. Retain database volumes.

The helper implements one narrowly scoped Docker firewall rule following
[Docker's documented DOCKER-USER chain](https://docs.docker.com/engine/network/firewall-iptables/).
The initial nine safety tests passed before the live test. Afterward, restoration
was hardened to retain its timer/state when iptables reports a verification
error. The final **10 helper tests plus 10 existing recovery-checker tests passed**;
that additional error path was unit-tested, without restarting AWS for another
fault. The live hosts hold the initially tested helper; copy the current script
before repeating a test. No full-suite or hosted CI result is claimed for this
change. Run the focused helper tests with:

```powershell
python -m pytest tests/unit/test_aws_outage_host.py -q
```
