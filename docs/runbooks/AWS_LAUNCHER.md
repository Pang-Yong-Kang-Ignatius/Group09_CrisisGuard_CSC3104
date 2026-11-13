# Docker launcher for an independent AWS Learner Lab

The launcher accepts a Git checkout or an extracted GitHub ZIP. Its `package`
command creates a minimal source archive without Git or a local Python install.
AWS bootstrap runs a [configuration preflight](../CONFIGURATION.md) before
starting containers. For shutdown/final retirement, use the [teardown guide](AWS_TEARDOWN.md)
and read-only `cleanup-plan` inventory; the launcher has no deletion command.

## Three-host protected deployment

The fresh launcher configures eight containers across three independent EC2
hosts. **This implementation has local validation only until a separately
approved AWS test succeeds.** Historical pilot evidence below covers an older
six-container configuration.

| Host | Components and persistent data |
|---|---|
| Cloud (`t3.small`) | PostgreSQL, Patient, Emergency, Admin, Controller and Dashboard; PostgreSQL named volume |
| Hospital A (`t3.micro`) | Hospital_A capture/replay, its own SQLite named volume |
| Hospital B (`t3.micro`) | Hospital_B capture/replay, its own SQLite named volume |

MetaGuard is enabled in Controller, Cloud writers and hospital replay; it is not
a ninth container. Local capture remains independent of Cloud availability.

### Credential distribution and transport

The source archive, S3 bucket and EC2 user data contain no generated secrets.
CloudFormation first prepares Docker on fresh hosts. The launcher retrieves each
host's public Ed25519 key from **authenticated EC2 console output**, pins it in
Paramiko, and authenticates using your existing lab EC2 private key. Unknown or
missing pins fail closed. There is no `AutoAddPolicy` or `ssh-keyscan` trust.

The launcher generates six distinct 256-bit values in memory: PostgreSQL,
operator, Hospital A, Hospital B, live-telemetry and replay-telemetry credentials,
plus separate RSA tunnel keys.
It sends JSON over encrypted SSH stdin, never arguments or S3. Cloud receives
the two hospital token mappings and operator/database secrets. Each hospital
receives only its own identity token and tunnel private key, plus the shared
replay-only telemetry provenance token used by the existing local design.
The telemetry token grants no event or controller access; live telemetry stays
on Cloud and no peer identity, operator or database secret reaches a hospital. Root-only `600` settings
are created exclusively; existing files are never overwritten. A failed partial
configuration requires private inspection, not a second launch or secret reset.

Each hospital initiates a persistent systemd SSH connection to Cloud's **private
IPv4 address**. Four local forwards on that hospital's private address
(`18001/18003/18004/18005`) carry HTTP inside SSH to Cloud's private
`8001/8003/8004/8005` listeners. Docker replay uses these host-local forwards.
Cloud authenticates the hospital token for Controller/grant-protected writes.
A network partition leaves capture and SQLite running; the tunnel restarts and
automatic replay resumes when connectivity returns.

Separate reverse forwards bind on Cloud's private IP: `18000` for Hospital A
and `18002` for Hospital B, each targeting its hospital's `127.0.0.1:8000`.
Dashboard uses these two endpoints for capture and event queues. Hospital API
access is authenticated by the SSH channel; capture endpoints remain loopback
only. Restricted authorized keys allow exactly the four Cloud destinations and
that hospital's reverse listener. No shell, agent, Unix socket or password access
is granted to the tunnel user. PostgreSQL has no host port. Dashboard binds only
`127.0.0.1:8080`, accessible through an operator SSH tunnel.

Security groups permit only SSH: operator public `/32` to all hosts and Hospital
SG to Cloud SSH. No application, reverse-forward, PostgreSQL or Dashboard port
is opened in a security group. Plain HTTP exists only inside a host or encrypted
SSH; it does not cross the VPC directly. The existing shared LabInstanceProfile
is used for source download/signals, never for secret distribution. The lab's
administrators and host root remain trusted; a shared powerful lab role is not
an isolation boundary against a malicious administrator.

This uses no new IAM roles, Secrets Manager, NAT gateways, load balancers or
Elastic IPs. Required permissions remain subject to Learner Lab policy. In
particular, `ec2:GetConsoleOutput` is required for fail-closed host-key pinning,
and `ec2:DescribeInstanceTypeOfferings` is required for AZ selection.
Missing permissions are blockers to report to the instructor, not bypass.

This launcher automates **fresh** three-host deployments. Run it on your computer
with Docker Desktop's Linux engine. Docker runs the provisioning client;
CloudFormation creates the actual AWS environment. It does not start the AWS
Academy session or grant access to another student's AWS account.

**Historical validation, 23 October:** the Docker image built successfully, the
documented PowerShell wrapper ran successfully, and the container rendered its
preview with networking disabled. CloudFormation schema validation using
`cfn-lint 1.58.0` passed after correcting the preview placeholder IDs and removing
redundant dependencies. **20 focused tests** passed, including a read-only-plan
check, account guard and embedded bootstrap Python syntax. Broad SSH sources and
missing credentials were rejected in real container runs. Temporary containers
were removed. See [local test evidence](../evidence/aws-launcher-20261023-local.json).

**Actual AWS pilot passed later on 23 October.** The Docker launcher created the
private source bucket and three EC2 hosts in `us-east-1` using `vockey` and the
existing `LabInstanceProfile`. CloudFormation reached `CREATE_COMPLETE` after
bootstrap health signals. Both hospitals automatically replayed a four-event
dependency chain through Patient, Emergency and Admin. All eight records were
applied, identical retries returned HTTP 200 `already_applied`, and wrong-site
capture returned HTTP 403. PostgreSQL contained exactly eight distinct rows.
The actual `plan`, `launch`, `status` and `stop` commands passed.
See [live pilot evidence](../evidence/aws-launcher-20261023-pilot.json).
After verification, the user-approved pilot stacks, instances, all three retained
disks and source bucket were removed. The temporary credentials file was removed.
The original three servers remained stopped with their disks and data retained.

Added a read-only preflight for this course's nine-instance/32-vCPU ceilings;
the final image passed a live plan with it, and **24 focused tests** passed.
This check is not a spending cap or a reservation against other concurrent users.
Always read your lab's current restrictions, which may be different. A previous
broader unit run stalled; there is no new full-suite pass claim. Hosted CI is
still pending. Resume, EC2 restart and network-partition recovery were not tested
in this fresh pilot. The original deployment was preserved and stopped.

The pilot source was the reviewed `d8978a7` application plus the uncommitted
bootstrap candidate, packaged explicitly for this test. It did not create a Git
commit. This historical evidence predates Git-independent packaging and does not
verify the current ZIP path.

## What it creates and configures

- Three Ubuntu 24.04 x86-64 instances: one `t3.small` cloud, two `t3.micro` hospitals.
- 52 GiB total encrypted gp3 EBS, Standard CPU credits and required IMDSv2.
- Two security groups: operator SSH from your public IPv4 `/32`, and private
  hospital-to-Cloud SSH. Application ports are not opened; hospital APIs stay on loopback.
- Docker Engine/Compose, the same source on each host, random private PostgreSQL
  credentials, distinct hospital sites/stores, all three cloud URLs and automatic replay.
- A separate CloudFormation source stack with a private encrypted S3 bucket.
  The explicit source bundle is uploaded there, checksum-checked and downloaded by each host
  using the **existing** `LabInstanceProfile`. No IAM user, role or policy is created.
- CloudFormation waits for host-preparation signals. The subsequent SSH phase
  gates Compose health and checks both hospital queues through Dashboard.
  `CREATE_COMPLETE` alone is not application readiness.

It uses an existing public subnet/internet gateway. It does not create a VPC,
NAT Gateway, RDS, load balancer or Elastic IP. EC2/public IPv4, EBS and S3 can use
lab credit. Check your lab's budget and permitted instance types first.

## One-time preparation per teammate

1. Download the reviewed revision from GitHub: open the CrisisGuard repository,
   choose **Code → Download ZIP**, save the ZIP, and extract it into a new folder.
   A Git clone also works. Record the GitHub commit SHA associated with the ZIP
   from the commit page; the extracted ZIP itself has no `.git` metadata. Check
   the latest CI result for that commit. Do not mix files from different revisions.
   The bundle includes only explicit source paths in
   `infrastructure/aws/package_source.py`; update that list when runtime source
   files change. It excludes `.env`, credentials, keys, virtual environments,
   local databases, caches, test results and documentation.
2. Install and start Docker Desktop with Linux containers. In PowerShell, enter
   the extracted folder, even if its path contains spaces:

   ```powershell
   Set-Location -LiteralPath 'C:\path with spaces\CrisisGuard-main'
   .\infrastructure\aws\run-lab.ps1 -Command package
   Get-Content -LiteralPath '.\.venv\aws-launcher\source.tar.sha256'
   Get-FileHash -Algorithm SHA256 -LiteralPath '.\.venv\aws-launcher\source.tar'
   ```

   The two displayed hashes must agree. `package` builds the local launcher
   image, checks every required file, and writes a reproducible tar and SHA-256
   sidecar. It makes no AWS API calls and needs neither Git nor local Python.
   A missing file, link in a selected source path, or Docker failure stops here.
   If Docker reports permission denied on its named pipe, start Docker Desktop,
   select Linux containers and check that your Windows account can use its engine.
   If a required source file is missing, download/extract the reviewed ZIP again;
   do not copy isolated files from another revision into the folder.
3. Start **your own** AWS Academy Learner Lab and wait for it to be ready.
4. Confirm that your lab permits CloudFormation, S3, EC2, these instance types
   and passing the existing `LabInstanceProfile`. That profile needs permission
   to read the source bucket and send CloudFormation signals. A lab restriction
   must be resolved with the instructor; the launcher does not bypass it.
5. Download your own lab PEM for later SSH/tunnels, using its existing key pair
   (normally `vockey`). Launch requires this unencrypted OpenSSH/PEM private key via `-SshKeyFile`.
   It is mounted read-only at runtime, never built into an image or uploaded.
   PPK and passphrase-protected keys are not supported by this unattended path.
6. Use the existing `crisisguard-lab` profile in your private
   `C:\Users\YOUR_NAME\.aws\credentials` file. When refreshing temporary
   values from AWS Details, preserve all other profile blocks. Alternatively,
   select a dedicated private INI file with `-CredentialsFile`. Never replace
   the whole shared file. The selected file must include this named profile
   and its session token:

   ```ini
   [crisisguard-lab]
   aws_access_key_id = COPY_CURRENT_LAB_VALUE
   aws_secret_access_key = COPY_CURRENT_LAB_VALUE
   aws_session_token = COPY_CURRENT_LAB_VALUE
   ```

   Keep the file private on your computer. Refresh it when the lab credentials
   expire. Do not use a permanent IAM key, commit this file, send it to teammates
   or paste credentials into chat. Only the selected credentials file is mounted read-only
   into the short-lived local launcher container; credentials are not baked into
   the image, uploaded with source or written into EC2 user data.
7. Find your current **public IPv4**, not your Wi-Fi `192.168.*` address. Use it
   with `/32` in the commands below. Do not use `0.0.0.0/0`.
8. Check the lab's current remaining credits and EC2 quotas in the Academy/AWS
   console. Budget for one `t3.small`, two `t3.micro`, 52 GiB gp3 EBS, three
   public IPv4 addresses and a small private S3 bucket. Use current regional
   prices and expected run hours to estimate cost; stopped EBS/S3 can still
   consume credit. The launcher's nine-instance/32-vCPU preflight does not
   measure available credits or guarantee capacity. Choose a unique stack name
   for your own lab, for example:

   ```powershell
   $cgStack = 'cg-prof-' + (Get-Date -Format 'yyyyMMdd') + '-' + [guid]::NewGuid().ToString('N').Substring(0,6)
   ```

## Run from PowerShell at the repository root

First, optionally render the resource template **without AWS credentials or API calls**:

```powershell
.\infrastructure\aws\run-lab.ps1 -Command preview -Stack $cgStack -SshCidr 'YOUR_PUBLIC_IP/32'
```

Preview checks/builds the local launcher image and prints placeholder resource
definitions. It is not an AWS deployment or a permission check.
The zero-valued VPC/subnet/AMI IDs are schema-valid examples, not usable resources.

Before any online command, obtain your own 12-digit Learner Lab account ID from
the authenticated AWS console. Replace the placeholder below and retain the
same value for plan, launch, status, stop and resume. `-ExpectedAccount` checks
the authenticated STS account; `-AccountId` separately confirms the account for
launch/stop/resume. Both must match your authorized account.

Then run the read-only plan:

```powershell
$cgAccount = 'YOUR_12_DIGIT_AWS_ACCOUNT_ID'
.\infrastructure\aws\run-lab.ps1 -Command plan -Stack $cgStack -SshCidr 'YOUR_PUBLIC_IP/32' -ExpectedAccount $cgAccount
```

The plan prints the AWS account, region, subnet/VPC, AMI, resource sizes and archive
SHA-256. It validates the template through AWS without creating resources or
uploading source. Check that it is **your Learner Lab account**. It does not prove
all creation/instance-profile permissions, capacity or budget are available.
It counts active/transitional instances and vCPUs across all returned pages and
refuses a launch that would exceed this course's configured nine/32 ceilings.

**Do not launch until the account, region, stack, credits, quotas, resource list,
current price estimate and eight-container/three-host scope have been reviewed and the owner
has explicitly approved creation.** To create that scoped environment, use the
account ID printed by the plan:

```powershell
.\infrastructure\aws\run-lab.ps1 -Command launch -Stack $cgStack -SshCidr 'YOUR_PUBLIC_IP/32' -ExpectedAccount $cgAccount -AccountId $cgAccount -Profile crisisguard-lab -SshKeyFile 'C:\private\labsuser.pem'
```

That command creates resources and uploads the packaged project source into
**your account's private S3 bucket**. Allow time for package installation and
image builds (up to the bootstrap timeout). Wait for `CREATE_COMPLETE` and the
bootstrap success message. Keep the same source checkout between plan and launch.
Do not start several copies or close the command assuming it stops AWS.
Expected output includes the source archive SHA-256, a resource summary and
CloudFormation `CREATE_COMPLETE`. This proves host preparation only. Wait also for the SSH configuration and
both-hospital Dashboard check. It does not prove end-to-end event application;
run the host and event checks below separately.

Defaults are region `us-east-1`, stack `crisisguard`, key `vockey`, and existing
instance profile `LabInstanceProfile`. Optional flags are `-Region`, `-Stack`,
`-KeyName`, `-InstanceProfile`, `-SubnetId`, `-Profile`, `-ExpectedAccount` and
`-CredentialsFile`. Default profile is `crisisguard-lab`. The original lab's
expected-account default remains `565337149326`; for your own lab, explicitly
supply `-ExpectedAccount $cgAccount` on every online command, including
`cleanup-plan`. Every online command stops on an STS account mismatch before
other AWS service calls. Do not remove or bypass this guard.
Credentials must contain that named profile (or explicitly select another profile).
Without a subnet override, discovery selects a default subnet whose AZ offers
both requested instance types, then checks its internet-gateway route. Offerings
are not a capacity reservation. Explicit unsupported AZs fail before creation.
For a restricted/private-subnet lab, follow the manual guide with your instructor.

## Access, stop and resume

Get current host IDs, states and IPs:

```powershell
.\infrastructure\aws\run-lab.ps1 -Command status -Stack $cgStack -ExpectedAccount $cgAccount
```

Use your own PEM and the Hospital A/B public IPs to open the SSH tunnels in the
[hospital SSH tunnel instructions](../../infrastructure/aws/README.md#5-deploy-each-hospital).
Confirm a synthetic capture becomes applied at both sites and the cloud stores
it. Record the ZIP's GitHub commit SHA, archive SHA-256, timestamps and results.
Use fresh unique event IDs; verify both hospital SQLite stores, exactly one Cloud
PostgreSQL application per ID, dependency order and retry deduplication. A
controlled connectivity interruption requires a separate approved test window:
capture while disconnected, verify durable local pending state, restore the
connection and verify replay and no duplicate Cloud effects. Bootstrap health
checks alone establish none of these outcomes.

For Dashboard, use your verified Cloud host key and your own PEM:

```powershell
ssh -i 'C:\private\labsuser.pem' -L 8080:127.0.0.1:8080 ubuntu@CLOUD_PUBLIC_IP
```

Open `http://127.0.0.1:8080`. Confirm both hospitals are healthy in
`/api/hospitals`, capture distinct synthetic dependency chains at each site,
then verify pending-to-applied replay, Controller grants and Cloud history.
Do not use `StrictHostKeyChecking=no`; obtain the initial host-key fingerprint
from EC2 console output through your authenticated account. Dashboard is an
operator console; keep the local tunnel private.

Run the [reusable deployment checker](AWS_HEALTH_CHECKS.md) over SSH on each host
to check its running configuration, all three APIs, hospital automatic replay
and queues. Supply your own expected event IDs or a full capture-body manifest
for record verification; no original TX records are required. The checker is
read-only, uses no AWS credentials, and produces a new pass/fail JSON report.

Stop exactly the three instances belonging to this launcher stack:

```powershell
.\infrastructure\aws\run-lab.ps1 -Command stop -Stack $cgStack -ExpectedAccount $cgAccount -AccountId $cgAccount
```

It waits for EC2 `Stopped`. Then choose **End Lab** in AWS Academy and verify the
timer reaches zero. Stopping the launcher container does **not** stop EC2. EBS and
S3 persist and may still cost credit. `stop` retains both CloudFormation stacks,
the three EC2 root EBS volumes, PostgreSQL's named volume, each hospital's SQLite
named volume, and the private environment files on those disks. It does not
delete a stack or erase a database. A new lab session may restart the instances;
check status before doing anything else. If needed, explicitly resume the same three:

```powershell
.\infrastructure\aws\run-lab.ps1 -Command resume -Stack $cgStack -ExpectedAccount $cgAccount -AccountId $cgAccount
```

Resume waits for EC2 checks, not application health. Use the reusable checker
through SSH after startup. Restart policies start Compose automatically; user
data is not re-run on resume.
Public IPs can change: use `status` rather than saved CloudFormation output IPs.
Refresh temporary credentials first if required. A changed client IP requires a
reviewed SSH-rule update; re-running launch does not change existing rules.

## Failed launches, storage and cleanup

`launch` refuses any existing live/failed stack with the chosen name, including
its `-source` stack. It never updates, replaces or deletes an existing deployment.
Use `status`/`resume` for a completed one. If creation fails, inspect CloudFormation
**Events** and `/var/log/cloud-init-output.log` over SSH. Do not print private env
files or share logs without reviewing them. An expired credential error can happen
while the AWS deployment continues; refresh the credentials and inspect the console.

Automatic rollback is disabled so failed bootstrap does not silently terminate
hosts holding evidence. Failed stacks can leave running resources. If the main
stack contains fewer than three hosts, `stop` deliberately refuses the incomplete
set: stop the exact created instance IDs in the EC2 console and end the lab.
Retain the error/evidence and resolve the failure before retrying; don't choose
new stack names to accumulate copies.

There is no automatic deletion command. Before final teardown, export and verify
backups. Then delete only your identified application/source stacks through the
console. EC2 root disks have `DeleteOnTermination=false` and the source bucket is
retained, so review/delete those identified retained disks and source objects
separately when the coursework no longer needs them. Stack deletion alone is not
complete cleanup. Do not delete TX's original hosts or reset the whole lab.
Use [the teardown guide](AWS_TEARDOWN.md) to inventory and review exact resource
ownership before any manual deletion; stack deletion is permanent retirement,
not an ordinary stop. No cleanup is authorized by the `cleanup-plan` output.

References: [CloudFormation EC2 resources](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ec2-instance.html),
[resource signals](https://docs.aws.amazon.com/AWSCloudFormation/latest/APIReference/API_SignalResource.html),
[Docker's Ubuntu installation](https://docs.docker.com/engine/install/ubuntu/).

## Fresh-ZIP test limitations and recovery

### Repeatable local checks (no AWS)

```powershell
python -m pytest tests/unit -q
python infrastructure/scripts/verify_devops_config.py
docker build -f infrastructure/aws/launcher.Dockerfile -t crisisguard-aws-launcher:ci infrastructure/aws
docker build -f infrastructure/docker/Dockerfile -t crisisguard-three-host:review .
docker build -f tests/fixtures/aws-ssh.Dockerfile -t crisisguard-ssh-transport:review infrastructure/aws
$sshTest = 'cg-ssh-test-' + [guid]::NewGuid().ToString('N').Substring(0,10)
docker run --name $sshTest --mount "type=bind,source=$((Get-Location).Path),target=/repo,readonly" crisisguard-ssh-transport:review python3 /repo/infrastructure/scripts/verify_aws_ssh_transport.py --isolated-container
```

The SSH test uses synthetic keys/responders in one Ubuntu namespace, verifies
the production forwarding ACLs and systemd unit, rejects wrong pins and peer
reverse listeners, and tests disconnect/reconnect. It retains its exited local
container; it does not delete Docker resources. It is not an EC2 deployment or
an application PostgreSQL integration test. The same test runs in CI.

Local rendering, schema, unit, packaging and transport tests cannot prove current
Learner Lab permissions, quotas, AZ capacity, public-IP reachability or credit.
Run a read-only plan with the named profile first; separately verify
`ec2:GetConsoleOutput`, EC2/CloudFormation/S3 actions, source access, signal access
and `iam:PassRole` for the existing LabInstanceProfile. No new AWS test has been
authorized by these instructions.

The SSH configuration phase is intentionally fresh-only. If interrupted, retained
hosts/disks may cost credit and partially configured files are preserved. Inspect
those exact new hosts privately; do not rerun launch under a different stack name,
reset tokens or delete volumes automatically. Runtime secrets live on encrypted
EBS; take reviewed backups separately before any approved destructive operation.
An EC2 stop/start in the same VPC retains private addresses, and enabled Docker
restart policies/systemd tunnels recover automatically; public SSH IPs can change.
A terminated/replaced host needs an explicit recovery procedure and credential
review. No automated destructive recovery is provided.
