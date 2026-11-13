# AWS deployment and team handoff (TX)

Host inventories, addresses, and Running/Stopped statements below describe the
dated verification sessions. They are not a live infrastructure inventory;
confirm the selected account, host identities and current state before use.

For a **fresh GitHub ZIP**, use the [three-host launcher guide](../../docs/runbooks/AWS_LAUNCHER.md).
It configures eight containers using pinned SSH, private encrypted tunnels and
per-hospital credentials. Its current implementation needs a separately approved
AWS test. The dated TX deployment evidence below concerns existing hosts and
must not be interpreted as verification of the new fresh-host bootstrap.

The numbered manual walkthrough below retains the earlier legacy deployment
procedure. It is not the current protected launcher's Compose selection or
network design. For a fresh protected deployment, follow the launcher guide;
for an existing deployment, preserve its recorded active-release configuration.
The unused `infrastructure/docker/compose.controller.example.yaml` is an older
integration scaffold, not the implemented MetaGuard deployment overlay.

**Current TX deployment — 1 November 2026:** the original three instances are
running the approved `16e74b8` MetaGuard release, with seven healthy containers.
The existing PostgreSQL container and all three data volumes were retained.
Both scoped hospital/controller partitions and same-image AIMD-to-fixed backout
passed; final policy is **fixed**. Fresh pre/post-upgrade backup copies passed
isolated restores on EC2 and Windows. Cloud TCP 8005 now accepts only the hospital
security group, alongside the existing private API rules. See
[current evidence](../../docs/evidence/tx-aws-review-20261101.json) and the
[evidence index](../../docs/EVIDENCE_INDEX.md).

## Historical TX acceptance (1 November 2026)

The dated `16e74b8` release passed 982 unit cases, 153 real PostgreSQL/API
integration cases, 12 offline Compose audits, and three hosted CI jobs with
retained artifacts; the focused 69 cases overlap the unit suite. Its local split
run applied 29 synthetic events and preserved 26 pre-existing containers and
23 volumes. On TX's AWS hosts, 32 new synthetic events applied once in dependency
order; all 12 old Cloud rows retained their fingerprints, for 44 Cloud rows and
22 per hospital. Separate A/B scoped firewall faults preserved four pending
events across each hospital restart while the other site progressed. Protected
recovery, same-image AIMD-to-fixed backout, and copied backup/isolated restore
checks passed. These are revision-specific results, not verification of the
current branch, physical-link faults, clean-machine setup, production security,
or current-release EC2 power cycling. See the retained
[AWS assessment](../../docs/evidence/tx-aws-review-20261101.json),
[local split assessment](../../docs/evidence/tx-protected-deployment-20261101.json),
[CI record](../../docs/evidence/tx-ci-final-release-20261101.json), and
[unit record](../../docs/evidence/tx-unit-20261101.json).

For this deployment, read `/home/ubuntu/crisisguard-active-release.json` on each
host and use its exact project, private env path and ordered Compose files.
Cloud's final list includes `policy-fixed.private.json`. The historical commands
below reference the older source and must not replace this active configuration.
The older **Stopped** statements describe prior lab sessions; the current three
instances have been left **Running**. Ignatius's separate deployment is unchanged.

## Policy-only backout on an existing deployment

This is distinct from rolling back an application image. Record the reviewed
source, CI, exact image IDs, Compose order, project, private settings and volume
identities. Take fresh PostgreSQL and both SQLite backups, compare transferred
SHA-256 values and check isolated restores as in [BACKUPS.md](../../docs/runbooks/BACKUPS.md).
Pause writers and require zero unresolved issued, acknowledged or
reconciliation-required grants without deleting uncertain attempts. Stop both
original hospital containers while retaining SQLite volumes. Pin a private
policy-only override to the **same running controller image ID**, set
`CONTROLLER_ADMISSION_POLICY=fixed`, append the override after the existing
Cloud/AWS/MetaGuard Compose files, and run `audit_deployment.py cloud` on that
exact selection. Recreate **only** the controller with
`up -d --no-build --no-deps --wait controller`; keep PostgreSQL and every volume. Require
authenticated `/admission/status` to show fixed policy, fresh telemetry and
zero unresolved grants, and compare image, database, event and grant identities.
Only then restart both original hospitals and verify new uniquely identified
synthetic events apply once. If a gate fails, keep replay paused and preserve
data for diagnosis. An older controller binary or database restore is not a
policy-only backout. The local verifier and TX's AWS run exercised bounded
versions of this procedure; neither establishes arbitrary migration safety.

**Maintenance verification — 25 October:** the original cloud/A/B hosts passed
source-verified label-only update, compatible rollback, fresh isolated backup
restores and failed-startup/backout checks. Twelve cloud records/six per hospital
were retained. Previous application images were restored; the deployed application
remains `ab76b88`. No new EC2/stacks or access changes were needed. All three hosts
are **Stopped** and Learner Lab is **Terminated / 00:00**. Private backups and
checkpoints remain on the hosts. See [evidence](../../docs/evidence/aws-update-rollback-20261025.json).

For an existing deployment, use the [safe update and compatible rollback runbook](../../docs/runbooks/DEPLOYMENT_UPDATES.md).
It covers fresh backups, immutable application-image pins, per-host health/record
gates and rollback with retained databases. Local and same-source AWS rehearsal
results are documented separately; neither proves arbitrary migration compatibility.

**Reusable checker — 24 October:** [AWS_HEALTH_CHECKS.md](../../docs/runbooks/AWS_HEALTH_CHECKS.md)
documents host-side checks for any teammate's configuration. The script now
discovers running hospital cloud URLs, checks all three services from inside
containers, validates automatic replay/site identity and reports backlog counts.
Select custom env/Compose/project settings and your own optional event
expectations. Historical records are no longer checked implicitly. It is read-only
and requires no AWS credentials; local regression results do not imply a new
live AWS verification. See the linked guide before using older commands below.

**Latest verification — 23 October 2026:** the original three hosts were upgraded
to committed `ab76b88`, preserving all private settings/passwords and volumes.
Both hospitals now use automatic replay, site identity and all three private
Cloud API URLs. A scoped Hospital A network partition passed: four pending events
recovered automatically while B remained connected. PostgreSQL retained both
originals plus eight test records, with no extra rows after duplicate retries.
Cloud ports 8001/8003/8004 accept only the hospital SG; SSH remains operator `/32`.
All three original instances are **Stopped**, with databases retained; Learner Lab
ended. No additional resources were created. See
[procedure, results and raw evidence](../../docs/reports/AWS_OUTAGE_TEST.md).
Earlier dated sections below describe their historical deployment versions.

**Teammates deploying in their own Learner Lab:** start with the
[Docker launcher guide](../../docs/runbooks/AWS_LAUNCHER.md) for automated fresh deployment
(Docker checks and actual fresh AWS pilot passed on 23 October; see the linked evidence), or the
[fresh-lab overview in the root README](../../README.md#12-optional-aws-academy-deployment)
(23 October). It uses your own account, keys, IPs and fresh databases. The dated
TX evidence below describes the older deployed commit. Current source/examples
include multi-service URLs and automatic replay; verify them on the selected
commit before deployment.

Verification update, 22 October 2026: all three existing EC2 hosts passed stop/start
recovery. All six containers started healthy automatically and the original
hospital and cloud records survived. Cloud container recreation also passed.
A PostgreSQL backup was created, its archive checked, and an off-server copy
downloaded with a matching SHA-256, then restored successfully in an isolated
local PostgreSQL container. Both actual AWS hospital SQLite backups also passed
isolated restores on EC2 and Windows, with matching download checksums.
Network-outage recovery was completed separately on 23 October, as linked above.
All three EC2 instances were confirmed **Stopped** again after these checks and
Learner Lab reported `Lab terminated` with a zero timer. This ended the session;
the EC2 instances, EBS volumes and existing deployment were retained.
This is a synthetic-data coursework deployment. Complete
the local checks in [DOCKER.md](../../docs/DOCKER.md#protected-local-startup)
before using it. Record the deployed Git commit on all three machines.

## Cloud host verified on 16 October 2026

- Region: `us-east-1`; name: `crisisguard-cloud`.
- Instance: `i-07c7700e26d3565cc`, Ubuntu 24.04 LTS x86-64, `t3.small`,
  On-Demand, Standard CPU credits. System and instance status checks passed.
- Private IPv4: `172.31.23.56`; VPC: `vpc-0035706cab1a4e7f0`;
  subnet: `subnet-00a45599dda20cac5`.
- Security group: `sg-0eae5cc5fc043294a` (`crisisguard-cloud-sg`), with TCP 22
  inbound from TX's current public IPv4 `/32` and TCP 8001 from the hospital
  security group `sg-019db2aad892f9e3d`. API ports are not publicly accessible.
- Root EBS: `vol-07d44c9aa1a27dc7b`, 20 GiB gp3, unencrypted,
  **Delete on termination: Yes**. Stop/start preserves it; termination deletes it.
- SSH host fingerprint verified against the EC2 system log before connecting.
- Docker Engine `29.9.1` and Compose `v5.6.0` installed using Docker's official
  Ubuntu apt repository; Docker enabled at boot and `hello-world` passed.
- Docker root `/var/lib/docker` is on `/dev/nvme0n1p1`, the EBS-backed root
  filesystem. Cloud PostgreSQL persistence across recreation and stop/start on AWS
  was subsequently verified on 22 October 2026.

- Cloud containers `postgres`, `patient`, `emergency`, and `admin` were healthy before shutdown;
  health requests on private ports 8001, 8003, and 8004 passed.

## Hospital deployment verified on 16 October 2026

| Host | Instance ID | Private IPv4 | Public IPv4 at deployment |
|---|---|---|---|
| Hospital A | `i-0d51d7af792a2cd8a` | `172.31.27.60` | `54.82.101.133` |
| Hospital B | `i-0380d518323626a27` | `172.31.19.60` | `174.129.148.135` |

Both use Ubuntu 24.04 x86-64, On-Demand `t3.micro`, Standard CPU credits, and
separate 16 GiB gp3 root volumes. Both passed all three EC2 status checks.
They share the cloud's VPC and subnet and use the existing `vockey` key pair.
Their security group `sg-019db2aad892f9e3d` (`crisisguard-hospitals-sg`) allows
only TCP 22 inbound from TX's current public IPv4 `/32`.

Docker Engine `29.9.1` and Compose `v5.6.0` are installed and Docker is enabled
at boot on each hospital. Docker's data directory is on the root EBS filesystem:
A uses `vol-0a9f90dc035a218c3`; B uses `vol-0d0cc16fc8e1527d1`.
The named volumes are `crisisguard-aws-hospital-a_hospital-data` and
`crisisguard-aws-hospital-b_hospital-data`. Hospital capture binds only to
`127.0.0.1:8000` on each host.

Deployment source: committed archive of
`2c7cb71fbfca624b540bc8c18b08bb8c62c4b9d6`. Each hospital records this in
`~/CrisisGuard/DEPLOYED_COMMIT`, uses its matching hospital environment template,
and sends events to `http://172.31.23.56:8001/events`.
The API takes `site_id` from each submitted event; submit `Hospital_A` on A
and `Hospital_B` on B. The project name does not enforce the event's site ID.

Verification completed at approximately 20:40-20:41 Singapore time:

| Hospital | Synthetic event | Result |
|---|---|---|
| A | `TX_AWS_A_20261016_01` | Applied |
| B | `TX_AWS_B_20261016_01` | Applied |

For each hospital, the event was captured as pending, survived container
recreation, and was applied through manual scheduler-aware replay to the private
cloud endpoint. An identical direct cloud retry returned `already_applied`.
A second hospital container recreation preserved the complete applied record.
Evidence is saved on the respective host as `~/CrisisGuard/TX_AWS_A_evidence.json`
and `~/CrisisGuard/TX_AWS_B_evidence.json`. These checks do not demonstrate
automatic replay or recovery from a network outage.

To view the hospital APIs after resuming, replace the public-IP placeholders below
with the current addresses from EC2. Run each command in a separate local
PowerShell window and keep it open. Ports 8061/8062 avoid the local Compose demo
ports 8051/8052. Check current public IPs in EC2 after an instance stop/start.

```powershell
ssh -i "$env:USERPROFILE\Downloads\labsuser.pem" -o ExitOnForwardFailure=yes -N -L 8061:127.0.0.1:8000 ubuntu@HOSPITAL_A_CURRENT_PUBLIC_IP
ssh -i "$env:USERPROFILE\Downloads\labsuser.pem" -o ExitOnForwardFailure=yes -N -L 8062:127.0.0.1:8000 ubuntu@HOSPITAL_B_CURRENT_PUBLIC_IP
```

Open Hospital A at `http://localhost:8061/docs` and Hospital B at
`http://localhost:8062/docs`. These addresses work only while their tunnels run.
Reuse the three existing instances in later lab sessions instead of launching
replacements. Stop them when idle; retained EBS storage continues to incur charges.

## Resume the existing deployment

1. Open Learner Lab and choose **Start Lab**. This lab starts previously stopped
   EC2 instances, including ones stopped manually. Wait for the lab to be ready.
2. Open EC2 in `us-east-1`. Find the three instance IDs recorded above, wait for
   their status checks to pass, and record their **new public IPv4 addresses**.
   The public addresses in the deployment table are historical and must not be reused.
3. If SSH times out after changing networks, check the TCP 22 source in the two
   security groups against your current public IPv4 `/32`. Keep API ports private.
   Connect as `ubuntu` using your locally held Learner Lab key; never commit or
   send the private key to teammates.
4. Docker is enabled at boot and the containers use `restart: unless-stopped`.
   Check the existing deployment using the commands below. Reuse its private env
   files and volumes; do not copy the examples over them or regenerate the database
   password. For a normal resume, no installation, upload or rebuild is needed.

On the **cloud host**, after SSH:

```bash
cd ~/CrisisGuard
sudo docker compose --env-file infrastructure/aws/cloud.env \
  -f infrastructure/docker/compose.cloud.yaml \
  -f infrastructure/aws/compose.cloud.override.yaml ps
curl --fail http://172.31.23.56:8001/health
curl --fail http://172.31.23.56:8003/health
curl --fail http://172.31.23.56:8004/health
```

On **each hospital host**, after SSH:

```bash
cd ~/CrisisGuard
sudo docker compose --env-file infrastructure/aws/hospital.env \
  -f infrastructure/docker/compose.hospital.yaml \
  -f infrastructure/aws/compose.hospital.override.yaml ps
curl --fail http://127.0.0.1:8000/events
curl --fail http://172.31.23.56:8001/health
```

If services are absent or stopped, run the same appropriate Compose command with
`up -d --wait --wait-timeout 180` in place of `ps`. If it fails, inspect `ps` and
`logs --tail 100` before changing configuration. Do not use `down -v`, volume prune,
or delete database files to fix startup.

Confirm A still has `TX_AWS_A_20261016_01` and B has `TX_AWS_B_20261016_01`, both
with status `applied`. The first AWS stop/start recovery test passed on 22 October;
save fresh evidence when repeating it. Open the SSH tunnels above to use Swagger
from your own computer.

When finished, select these three instances, choose **Instance state > Stop
instance**, and wait until each is **Stopped**. Then choose **End Lab** in Learner
Lab. On 16 October this was completed: all three instances showed Stopped and the
lab reported `Lab terminated` with a zero timer. That lab-session label does not
mean the EC2 instances were terminated. Retained EBS storage still consumes budget.
Do not choose EC2 **Terminate** or lab **Reset** to pause work: those can delete
the deployment and data. The lab is not a backup; keep code in Git and complete
the database backup/restore work below.

## Remaining TX work

The historical TX acceptance above supersedes the older manual-recovery task
list. Automatic multi-service replay and the scoped
AWS network-outage test passed on 23 October; those are completed checkpoints.

- Run the new [configuration preflight](../../docs/CONFIGURATION.md) before a
  deployment; preserve existing private settings and restore-checked backups.
- Review/push the latest changes and verify hosted CI; collect an independent
  teammate deployment record with source commit, image IDs, health checks,
  backup/restore results and observed outcomes. The historical acceptance
  template is not included in this submission snapshot or its Git history; use
  a separately retained original if available.
- Same-source EC2 update/rollback rehearsal passed on 25 October. For each actual
  application release, review schema/API/event compatibility and repeat its
  applicable acceptance gates; the label-only rehearsal is not migration proof.
- Use the implemented MetaGuard overlays and
  [deployment update guide](../../docs/runbooks/DEPLOYMENT_UPDATES.md)
  for protected deployments. The current three-host launcher provisions the
  protected eight-container stack; direct use of `deploy.sh` without its
  `--metaguard` or `--full-system` option remains the legacy procedure. Review the
  [current evidence index](../../docs/EVIDENCE_INDEX.md)
  and [policy-only backout](#policy-only-backout-on-an-existing-deployment) before an upgrade.
  Historical experiments are documented in the [evidence index](../../docs/EVIDENCE_INDEX.md).
- For ordinary shutdown or final owner-approved retirement, use the
  [exact-ID teardown guide](../../docs/runbooks/AWS_TEARDOWN.md). The launcher now has a
  read-only `cleanup-plan`; it has no deletion command.

## Recovery verified on 22 October 2026 (historical checker)

At that checkpoint, `infrastructure/scripts/verify_aws_recovery.py` checked the existing deployment
without rebuilding containers, replaying events or changing database contents.

**Current behavior differs:** the updated checker uses configuration discovery,
requires automatic replay for hospital checks by default and checks records only
when `--expect-event` or `--expect-events` is supplied. The dated evidence below
retains its original scope; use the new guide for current commands and manifests.
It required healthy containers, reachable cloud health endpoints and the complete
original applied records from 16 October. On the cloud, it reads both records
directly from PostgreSQL in a read-only transaction. On each hospital, it checks
that site's record through its local API. JSON evidence includes the UTC check
time, hostname, service states and verified event IDs; it excludes credentials
and patient payloads.

The checker passed its 10 focused local unit tests, then passed on all three EC2
hosts. The deployment tested was still `2c7cb71`; only the checker was uploaded.
Newer backend, dashboard and evaluation code in the local branch was not deployed.

TX started Learner Lab; the existing instances booted at approximately 13:35
Singapore time. All three passed 3/3 EC2 status checks. With TX's approval, the
SSH source in both project security groups was replaced with the current client
public IPv4 `/32`; the private Patient API rule was unchanged.

| Check | Time (Singapore) | Result / saved evidence |
|---|---|---|
| Stop/start recovery, both SQLite stores and PostgreSQL | 13:42 | All six containers healthy; complete original A/B records still applied. [JSON](../../docs/evidence/aws-20261022-stop-start.json) |
| Cloud container recreation with existing volume and images | 13:44 | All four cloud containers recreated and healthy; original cloud and hospital records unchanged; private cloud health reachable from both hospitals. [JSON](../../docs/evidence/aws-20261022-cloud-recreation.json) |
| PostgreSQL backup | Before recreation | Custom-format archive created; `pg_restore --list` passed; downloaded copy has matching SHA-256. |
| Restore actual AWS PostgreSQL backup locally | 13:59 | Fresh isolated PostgreSQL container restored both original events; both applied. [JSON](../../docs/evidence/aws-20261022-backup-local-restore.json). This is an off-host restore drill, not a live application cutover. |
| Back up actual AWS hospitals and restore on EC2 / Windows | 14:14–14:16 | Both 20,480-byte backups passed SHA-256 verification and isolated restoration. Each contains its original applied event and recovery-state row. [JSON](../../docs/evidence/aws-20261022-hospital-backups.json). |

The backup is 2,115 bytes, SHA-256
`67fef154f9a66cd26ddefc99770622c43d5159d3a333ca6308be7111738f5579`.
It is stored on the cloud host at
`~/crisisguard-backups/20261022-recovery/cloud-before-recreation.dump` and on TX's
computer in a private backup folder outside the repository, with filename
`cloud-before-recreation-20261022.dump`.
The database backup is kept outside Git; committed evidence contains only check
results, service states and synthetic event IDs.

After gaining lab access, use the resume checklist above. Copy this single script
to the existing `~/CrisisGuard/infrastructure/scripts/` directory on each host;
there is no need to replace the deployed source or private env files to run it.
From local PowerShell, substitute the current public IP of the destination:

```powershell
scp -i "$env:USERPROFILE\Downloads\labsuser.pem" infrastructure/scripts/verify_aws_recovery.py ubuntu@CURRENT_PUBLIC_IP:~/CrisisGuard/infrastructure/scripts/verify_aws_recovery.py
```

After SSH, run the appropriate command from `~/CrisisGuard` on each host:

```bash
# Cloud host
sudo python3 infrastructure/scripts/verify_aws_recovery.py cloud --output cloud-recovery.json
# Hospital A host
sudo python3 infrastructure/scripts/verify_aws_recovery.py hospital-a --output hospital-a-recovery.json
# Hospital B host
sudo python3 infrastructure/scripts/verify_aws_recovery.py hospital-b --output hospital-b-recovery.json
```

Use a new evidence filename for each run; existing files are not overwritten.
Exit code 0 and `"result": "passed"` mean that host's checks passed. A failure
does not repair or reset the deployment: inspect the reported reason and existing
container state first. Copy the evidence files back to your computer. These
checks verify retained historical records and health, not automatic replay,
network outage recovery or backup restoration.

## AWS Academy Learner Lab entry point

TX is using AWS Academy Learner Lab. Open the course's **Modules**, select
**Launch AWS Academy Learner Lab**, start the lab session, then open the AWS console
when the lab indicator is ready. Check the remaining budget, permitted region,
allowed instance types and any existing resources before launching new instances.
Do not paste temporary AWS credentials or SSH private keys into chat or Git.

The [course overview](https://awsacademy.instructure.com/courses/185965) states a
$50 course budget and a default four-hour session. This is the course allocation,
not a verified remaining balance. It also states EC2 instances are automatically
stopped at session end while resources persist, and some resources can continue
incurring charges. Plan to restart the same instances in later sessions rather
than creating replacements. Verify the current lab restrictions in the session.

The local persistence demonstration and hosted GitHub Actions checks have passed;
see [the recorded results](../../docs/DOCKER.md#historical-verification-references).

## 1. Choose the lab resources

Use your course-approved AWS account/region and confirm its budget and allowed
instance types. Create three Ubuntu 24.04 LTS x86-64 EC2 instances in the same VPC:

| Host | Compose services | Stored data |
|---|---|---|
| Hospital A | hospital | Its own SQLite named volume |
| Hospital B | hospital | Its own SQLite named volume |
| Cloud | patient, emergency, admin, postgres | PostgreSQL named volume |

Choose capacity available in your lab; do not assume the deployment is free.
Record each instance ID, private IP, EBS volume ID and security group ID. All three
hosts need outbound access to download packages, images and the repository.
Use the lab's existing internet/bastion arrangement. If you use public IPs for SSH,
application traffic must still use the cloud's private IP.

## 2. Restrict the network

Create a hospital security group used by A/B and a cloud security group.

| Destination | Inbound access |
|---|---|
| All hosts, TCP 22 | Your current public IPv4 `/32`, or the lab bastion security group |
| Cloud, TCP 8001 | Hospital security group |
| Cloud, TCP 8003 and 8004 | Hospital security group for Emergency/Admin routing and health checks |
| Hospital capture, TCP 8000 | No VPC/public rule needed with the localhost binding and SSH tunnels below |
| PostgreSQL, TCP 5432 | No host publication or security-group rule |

Security-group references allow traffic from the associated instances' private
addresses. They require working VPC routes; they do not create routes. See
[AWS security-group rules](https://docs.aws.amazon.com/vpc/latest/userguide/security-group-rules.html).
These legacy API instructions predate protected writes. Current protected Cloud
writes authenticate hospital identity and require a matching MetaGuard grant;
status/health reads and hospital capture still rely on restricted network access.
Keep API ports off the public internet. The current launcher uses pinned SSH
tunnels instead of these historical direct inter-host API ingress rules.

## 3. Install Docker and confirm persistent storage

On each host, install Docker Engine and the Compose plugin using the official
[Ubuntu installation instructions](https://docs.docker.com/engine/install/ubuntu/).
Use the apt-repository method. Then check:

```bash
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
sudo docker info --format '{{.DockerRootDir}}'
lsblk -f
findmnt -T /var/lib/docker
```

Use the actual DockerRootDir path in `findmnt` if it differs. Confirm in EC2 that
its backing device is an EBS volume. Named volumes on that filesystem survive
container recreation and instance stop/start. They are not a backup.

For the initial lab deployment an EBS root volume can hold Docker's named volumes.
Check its **Delete on termination** setting before relying on it: root volumes
normally default to deletion. Set it to false if the records must survive instance
termination; retained volumes continue to incur charges. See
[AWS EBS persistence settings](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/preserving-volumes-on-termination.html).
Do not format a device or move Docker's data directory on an existing installation
as part of this guide. Take backups before any storage migration.

Clone the project on each host using your existing GitHub access and check out the
same reviewed commit. The commands below run from the CrisisGuard repository root.

## 4. Deploy the cloud first

```bash
cp infrastructure/aws/cloud.env.example infrastructure/aws/cloud.env
chmod 600 infrastructure/aws/cloud.env
openssl rand -hex 24
nano infrastructure/aws/cloud.env
```

Set `CLOUD_BIND_ADDRESS` to the cloud instance's private IPv4 and paste the generated
hex password into `POSTGRES_PASSWORD`. Hex avoids URL-escaping issues in the current
`DATABASE_URL` construction. Keep the file private; `*.env` under infrastructure is
Git-ignored and excluded from the Docker build context. Changing the password in
this file later does not change credentials already stored in PostgreSQL.

Use a clean shell without local Compose variable overrides. Shell variables take
precedence over env-file settings. Start:

```bash
sudo bash infrastructure/aws/deploy.sh cloud "$PWD/infrastructure/aws/cloud.env"
```

Expect four healthy services. From either hospital host, substitute the real IP:

```bash
curl --fail http://CLOUD_PRIVATE_IP:8001/health
```

## 5. Deploy each hospital

On A, copy the A example; on B, use the B example:

```bash
cp infrastructure/aws/hospital-a.env.example infrastructure/aws/hospital.env
chmod 600 infrastructure/aws/hospital.env
nano infrastructure/aws/hospital.env
sudo bash infrastructure/aws/deploy.sh hospital "$PWD/infrastructure/aws/hospital.env"
```

Replace the private-IP placeholder in all three URLs: `CLOUD_URL`,
`EMERGENCY_URL` and `ADMIN_URL`. Keep the matching `SITE_ID`. Each host keeps its own volume.
The startup script adds restart policies and bounded Docker log rotation through
the AWS override file. Current examples set `AUTO_REPLAY=1`; current Edge code
starts the background replay loop within the Capture API process. Check
`GET /recovery/status` and each cloud service's health after startup. Earlier
TX deployment evidence used manual replay on the older `2c7cb71` commit.

From your Windows machine, open two PowerShell terminals (substitute your key and
reachable SSH host addresses, or use your lab's bastion/ProxyJump arrangement):

```powershell
ssh -i "C:\path\lab-key.pem" -N -L 8051:127.0.0.1:8000 ubuntu@HOSPITAL_A_SSH_HOST
ssh -i "C:\path\lab-key.pem" -N -L 8052:127.0.0.1:8000 ubuntu@HOSPITAL_B_SSH_HOST
```

Keep each tunnel open. Use `http://localhost:8051/docs` and
`http://localhost:8052/docs` to capture new synthetic patient events, supplying
`site_id: Hospital_A` or `Hospital_B` respectively.

For a controlled manual check, invoke scheduler-aware replay from the hospital
container. Set `AUTO_REPLAY=0` in its private env and redeploy first if you need
to isolate manual behavior from background replay:

```bash
sudo docker compose --env-file infrastructure/aws/hospital.env -f infrastructure/docker/compose.hospital.yaml -f infrastructure/aws/compose.hospital.override.yaml exec -T hospital python -c 'from edge.capture_api.scheduled_replay import replay_scheduled_events; print(replay_scheduled_events())'
```

Check local `GET /events` for `applied`. Current Edge code routes patient events
to Patient, emergency records to Emergency and discharge events to Admin using
the three configured URLs. This manual walkthrough predates controller-grant
integration; current protected replay is implemented by the MetaGuard overlays
and launcher. Its source availability alone does not establish verification of
the selected deployed revision.
Restore `AUTO_REPLAY=1` and redeploy when returning to the automatic-recovery setup.

## 6. Capture evidence and test recovery

Record timestamps, event IDs, commit SHA, `docker compose ps` and API responses.
Repeat for both sites:

1. Capture and replay a synthetic patient event.
2. Use the same env/base/override arguments with `up -d --force-recreate --wait`.
   Verify the local applied event remains and the cloud acknowledges an identical
   retry with `already_applied`. Recreate the cloud as well and verify again.
3. With capture running, stop the cloud APIs using the cloud Compose arguments
   and `stop patient emergency admin`. Capture new events at both sites and check
   they remain pending. Start the cloud with the deployment script; with
   `AUTO_REPLAY=1`, wait for automatic recovery without invoking manual replay.
4. Reboot each instance separately; confirm Docker/services return and records remain.

Step 3 is an API outage. For the separately verified per-hospital network fault,
follow [AWS_OUTAGE_TEST.md](../../docs/reports/AWS_OUTAGE_TEST.md). Its scoped container
rule and timed restoration preserve capture and SSH; packet probes/counters
establish the fault. Security-group rule removal alone is not proof of a partition.
That 23 October test used direct Cloud connections at revision `ab76b886`.
Its firewall helper does not cover the later SSH-tunneled deployment: it blocks
three direct API ports and refuses Hospital B. A two-hospital tunnel interruption
was proposed later, but no completed result is retained here.

## 7. Backups, shutdown and teardown

Use the repeatable [backup and isolated restore walkthrough](../../docs/runbooks/BACKUPS.md)
for cloud and hospitals. The 22 October hospital downloads restored successfully
on Windows. On 25 October the current tools were staged separately on all three
hosts for the maintenance rehearsal; fresh backups and isolated restores passed
there. Those new backups remain private on EC2 and were not downloaded. The
existing application checkout and private environment files were preserved.

Keep the same project names and env files when restarting. `down` retains named
volumes; `down -v` deletes them. Do not use volume pruning on a data-bearing host.

Before destructive changes, create a PostgreSQL logical backup using the cloud
Compose arguments with `exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"'`
and redirect output into a private backup file. For SQLite use Python's SQLite
backup API or stop the hospital before copying its database. Copy backups off the
instance and test restoration into a separate project before calling them verified.

Stop EC2 instances when the lab is idle as allowed by your course; EBS and some
other resources may still incur charges. At final teardown, export required data,
terminate only the three identified project instances, then review retained EBS
volumes/snapshots and any allocated public addresses. This guide does not automate
deleting cloud resources.
