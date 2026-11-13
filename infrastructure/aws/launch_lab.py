"""Launch an independent three-host coursework lab; no credentials are embedded.

preview is entirely offline. plan performs read-only AWS discovery. launch
creates new CloudFormation stacks and uploads the reviewed source archive to private S3.
Existing stacks are never updated/replaced by this tool. No delete command.
cleanup-plan only inventories the selected launcher's stacks and attached disks.
"""
import argparse
import base64
import hashlib
import ipaddress
import io
import json
from pathlib import PurePosixPath
import re
import secrets
import sys
import tarfile
import time
from pathlib import Path


def validate_inputs(args):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.stack):
        raise ValueError("Stack name must be 1-40 lowercase letters/digits/hyphens, starting with a letter")
    network = ipaddress.ip_network(args.ssh_cidr, strict=True)
    if network.version != 4 or network.prefixlen != 32:
        raise ValueError("SSH source must be one public IPv4 /32, never 0.0.0.0/0")
    if not network.network_address.is_global:
        raise ValueError("SSH source must be your public IPv4 /32")


def check_archive(path):
    """Check the Git-independent source bundle before any AWS upload."""
    required = {".dockerignore", ".env.example", "compose.yaml", "requirements.txt",
                "infrastructure/docker/Dockerfile", "infrastructure/docker/compose.metaguard.yaml",
                "infrastructure/aws/bootstrap_lab.sh", "infrastructure/aws/deploy.sh",
                "infrastructure/aws/configure_host.py", "infrastructure/aws/compose.dashboard.cloud.yaml",
                "infrastructure/aws/hospital-a.env.example", "infrastructure/aws/hospital-b.env.example",
                "infrastructure/aws/compose.cloud.override.yaml", "infrastructure/aws/compose.hospital.override.yaml",
                "infrastructure/aws/compose.metaguard.cloud.yaml", "infrastructure/aws/compose.metaguard.hospital.yaml",
                "infrastructure/docker/compose.cloud.yaml", "infrastructure/docker/compose.hospital.yaml",
                "infrastructure/scripts/audit_deployment.py", "infrastructure/scripts/database_backup.py",
                "infrastructure/scripts/init_local_env.py",
                "infrastructure/scripts/verify_aws_recovery.py",
                "edge/capture_api/main.py", "cloud/patient_service/main.py",
                "cloud/emergency_service/main.py", "cloud/admin_service/main.py", "dashboard/main.py"}
    with tarfile.open(path) as archive:
        names = set()
        for member in archive.getmembers():
            name = member.name
            parts = PurePosixPath(name).parts
            if (name in names or name.startswith("/") or "\\" in name or ":" in name
                    or ".." in parts or member.issym() or member.islnk()
                    or not (member.isfile() or member.isdir())):
                raise ValueError("Unsafe source archive entry")
            if any(part in {".git", ".aws", ".venv", ".venv311", "backups", "db-snapshots",
                            "node_modules", "__pycache__", ".pytest_cache", ".project-test-results"}
                   for part in parts):
                raise ValueError("Source archive contains private/runtime directories")
            if (name.endswith((".env", ".pem", ".ppk", ".key", ".db", ".sqlite", ".sqlite3", ".dump", ".private.json"))
                    or (parts and PurePosixPath(name).name.startswith('.env.') and not name.endswith('.env.example'))
                    or (parts and PurePosixPath(name).name == 'credentials') or re.search(r"\.(db|sqlite|sqlite3)-", name)):
                raise ValueError("Source archive contains private configuration, keys or database files")
            names.add(name)
    if not required <= names:
        raise ValueError("Source archive is missing required deployment files")
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_template():
    return {"AWSTemplateFormatVersion": "2010-09-09", "Resources": {"Source": {
        "Type": "AWS::S3::Bucket", "DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain",
        "Properties": {"PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "BlockPublicPolicy": True,
            "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
            "BucketEncryption": {"ServerSideEncryptionConfiguration": [
                {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}}},
        "Outputs": {"Bucket": {"Value": {"Ref": "Source"}}}}


def user_data(role, logical_id, bucket, key, sha):
    # IAM credentials are retrieved by boto3 from the existing LabInstanceProfile.
    # Never put local temporary credentials or the PostgreSQL password in user data.
    download = f'''import boto3,hashlib,tarfile
from pathlib import Path
p=Path('/opt/crisisguard-source.tar')
boto3.client('s3').download_file({bucket!r},{key!r},str(p))
assert hashlib.sha256(p.read_bytes()).hexdigest()=={sha!r}, 'Source checksum mismatch'
target=Path('/home/ubuntu/CrisisGuard'); target.mkdir(parents=True,exist_ok=True)
with tarfile.open(p) as archive:
    for member in archive.getmembers():
        assert not member.issym() and not member.islnk()
        assert (target/member.name).resolve().is_relative_to(target)
    archive.extractall(target)
'''
    signal = f'''import boto3,os
boto3.client('cloudformation',region_name='${{AWS::Region}}').signal_resource(
 StackName='${{AWS::StackName}}',LogicalResourceId={logical_id!r},
 UniqueId={logical_id!r},Status='SUCCESS' if os.environ['BOOT_RC']=='0' else 'FAILURE')
'''
    # No credentials, private keys, or cross-host dependency in user data.
    shell = f'''#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-boto3
export AWS_DEFAULT_REGION='${{AWS::Region}}'
signal_result() {{
  export BOOT_RC="$?"
  python3 - <<'CG_SIGNAL'
{signal}CG_SIGNAL
}}
trap signal_result EXIT
python3 - <<'CG_DOWNLOAD'
{download}CG_DOWNLOAD
bash /home/ubuntu/CrisisGuard/infrastructure/aws/bootstrap_lab.sh prepare
'''
    return {"Fn::Base64": {"Fn::Sub": shell}}


def template(args, vpc, subnet, ami, bucket, key, sha):
    resources = {}
    for name in ["CloudSecurity", "HospitalSecurity"]:
        resources[name] = {"Type": "AWS::EC2::SecurityGroup", "Properties": {
            "GroupDescription": "CrisisGuard private APIs and restricted SSH", "VpcId": vpc,
            "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 22,
                                      "ToPort": 22, "CidrIp": args.ssh_cidr}]}}
    # Application ports are never reachable between hosts directly. Only the
    # authenticated SSH transport traverses the VPC; forwarding ACLs are per site.
    resources["HospitalTunnelSSH"] = {"Type": "AWS::EC2::SecurityGroupIngress", "Properties": {
            "GroupId": {"Fn::GetAtt": ["CloudSecurity", "GroupId"]}, "IpProtocol": "tcp",
            "FromPort": 22, "ToPort": 22,
            "SourceSecurityGroupId": {"Fn::GetAtt": ["HospitalSecurity", "GroupId"]}}}
    for logical, role in [("Cloud", "cloud"), ("HospitalA", "hospital-a"), ("HospitalB", "hospital-b")]:
        group = "CloudSecurity" if role == "cloud" else "HospitalSecurity"
        resources[logical] = {"Type": "AWS::EC2::Instance",
            # Avoid rollback terminating newly created hosts/disks after a bootstrap failure.
            "Properties": {"ImageId": ami, "InstanceType": "t3.small" if role == "cloud" else "t3.micro",
                "KeyName": args.key_name, "IamInstanceProfile": args.instance_profile,
                "CreditSpecification": {"CPUCredits": "standard"},
                "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
                "NetworkInterfaces": [{"DeviceIndex": "0", "AssociatePublicIpAddress": True,
                    "SubnetId": subnet, "GroupSet": [{"Fn::GetAtt": [group, "GroupId"]}]}],
                "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {
                    "VolumeType": "gp3", "VolumeSize": 20 if role == "cloud" else 16,
                    "Encrypted": True, "DeleteOnTermination": False}}],
                "Tags": [{"Key": "Name", "Value": args.stack + "-" + role}],
                "UserData": user_data(role, logical, bucket, key, sha)},
            "CreationPolicy": {"ResourceSignal": {"Count": 1, "Timeout": "PT30M"}}}
    outputs = {}
    for logical in ["Cloud", "HospitalA", "HospitalB"]:
        outputs[logical + "Id"] = {"Value": {"Ref": logical}}
        for attr in ["PublicIp", "PrivateIp"]:
            outputs[logical + attr] = {"Value": {"Fn::GetAtt": [logical, attr]}}
    return {"AWSTemplateFormatVersion": "2010-09-09", "Resources": resources, "Outputs": outputs}


def wait_stack(cf, name):
    deadline = time.monotonic() + 2400
    previous = None
    while time.monotonic() < deadline:
        state = cf.describe_stacks(StackName=name)["Stacks"][0]
        status = state["StackStatus"]
        if status != previous:
            print(name + ": " + status, flush=True)
            previous = status
        if status == "CREATE_COMPLETE":
            return {item["OutputKey"]: item["OutputValue"] for item in state.get("Outputs", [])}
        if status.endswith(("FAILED", "ROLLBACK_COMPLETE")):
            raise RuntimeError("Stack failed. Inspect CloudFormation Events and cloud-init logs; retained resources may still cost credit. Do not launch a second copy.")
        time.sleep(20)
    raise RuntimeError("Timed out; resources may still be running. Use status/stop and inspect the console.")


def refuse_existing(cf, names):
    # Include every page and every live/failed status; ignore only deleted stacks.
    for page in cf.get_paginator("list_stacks").paginate():
        for stack in page["StackSummaries"]:
            if stack["StackName"] in names and stack["StackStatus"] != "DELETE_COMPLETE":
                raise ValueError("Stack already exists; use status/resume or inspect it. Existing deployments are never replaced.")


def discover(ec2, args):
    if args.subnet_id:
        subnet = ec2.describe_subnets(SubnetIds=[args.subnet_id])["Subnets"][0]
    else:
        candidates = ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
        if not candidates:
            raise ValueError("No default subnet; supply --subnet-id for your course-approved public subnet")
        subnet = None
        for candidate in sorted(candidates, key=lambda s: s['SubnetId']):
            if supported_types(ec2, candidate.get('AvailabilityZone', '')):
                subnet = candidate
                break
        if subnet is None:
            raise ValueError('No default subnet AZ offers both t3.small and t3.micro; supply a supported --subnet-id')
    routes = ec2.describe_route_tables(Filters=[{"Name": "association.subnet-id", "Values": [subnet["SubnetId"]]}])["RouteTables"]
    if not routes:
        routes = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [subnet["VpcId"]]},
                                                   {"Name": "association.main", "Values": ["true"]}])["RouteTables"]
    if not any(r.get("DestinationCidrBlock") == "0.0.0.0/0" and r.get("GatewayId", "").startswith("igw-") and r.get("State") == "active"
               for table in routes for r in table["Routes"]):
        raise ValueError("Selected subnet lacks an active internet-gateway route; use the manual/bastion guide")
    if not supported_types(ec2, subnet.get('AvailabilityZone', '')):
        raise ValueError('Selected subnet AZ does not offer both t3.small and t3.micro')
    ec2.describe_key_pairs(KeyNames=[args.key_name])
    images = ec2.describe_images(Owners=["099720109477"], Filters=[
        {"Name": "name", "Values": ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]},
        {"Name": "state", "Values": ["available"]}])["Images"]
    if not images:
        raise ValueError("Ubuntu 24.04 image not found in this region")
    return subnet["VpcId"], subnet["SubnetId"], max(images, key=lambda i: i["CreationDate"])["ImageId"]


def supported_types(ec2, zone):
    if not zone:
        return False
    types = set()
    for page in ec2.get_paginator('describe_instance_type_offerings').paginate(
            LocationType='availability-zone', Filters=[
                {'Name': 'location', 'Values': [zone]},
                {'Name': 'instance-type', 'Values': ['t3.small', 't3.micro']}]):
        types.update(item['InstanceType'] for item in page['InstanceTypeOfferings'])
    return {'t3.small', 't3.micro'} <= types


def console_host_key(ec2, instance_id):
    """Trust only the authenticated EC2 API for the initial SSH host key pin."""
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        output = ec2.get_console_output(InstanceId=instance_id, Latest=True).get('Output', '')
        if 'CG_SSH_HOST_KEY ' not in output:
            try:
                output = base64.b64decode(output).decode('utf-8', errors='replace')
            except (ValueError, UnicodeError):
                output = ''
        # cloud-init/journald can prefix console lines with timestamps/PIDs.
        matches = re.findall(r'CG_SSH_HOST_KEY (ssh-ed25519 [A-Za-z0-9+/=]+)', output)
        if len(set(matches)) == 1:
            return matches[0]
        if matches:
            raise ValueError('Ambiguous EC2 console host key; refusing SSH')
        time.sleep(10)
    raise ValueError('Cannot pin SSH host key from EC2 console output; no secrets sent')


def configure_over_ssh(host, host_key, operator_key, payload, *, verify=False):
    import paramiko
    client = paramiko.SSHClient()
    key = paramiko.Ed25519Key(data=base64.b64decode(host_key.split()[1]))
    client.get_host_keys().add(host, 'ssh-ed25519', key)
    # Default RejectPolicy is deliberate. No agent, key discovery or TOFU.
    try:
        client.connect(host, username='ubuntu', pkey=operator_key, allow_agent=False,
                       look_for_keys=False, timeout=15, banner_timeout=30, auth_timeout=30)
        stdin, stdout, stderr = client.exec_command(
            'sudo -n python3 /home/ubuntu/CrisisGuard/infrastructure/aws/configure_host.py' + (' verify' if verify else ''), timeout=1800)
        stdin.write(json.dumps(payload))
        stdin.flush()
        stdin.channel.shutdown_write()
        # Drain without echoing private remote output. Bounded Docker logs stay
        # on the host; errors contain only the role, never payload/SDK traces.
        channel = stdout.channel
        deadline = time.monotonic() + 1800
        while not channel.exit_status_ready():
            if time.monotonic() >= deadline:
                raise RuntimeError('SSH configuration timed out; inspect privately, do not relaunch')
            if channel.recv_ready():
                channel.recv(65536)
            if channel.recv_stderr_ready():
                channel.recv_stderr(65536)
            time.sleep(.1)
        if channel.recv_exit_status() != 0:
            raise RuntimeError('Host configuration failed for ' + payload['role'] + '; inspect privately, do not relaunch')
    finally:
        client.close()


def provision_hosts(ec2, outputs, operator_key):
    import paramiko
    keys, pins = {}, {}
    for logical in ('Cloud', 'HospitalA', 'HospitalB'):
        pins[logical] = console_host_key(ec2, outputs[logical + 'Id'])
    for logical in ('HospitalA', 'HospitalB'):
        key = paramiko.RSAKey.generate(3072)
        private = io.StringIO()
        key.write_private_key(private)
        keys[logical] = (key.get_name() + ' ' + key.get_base64(), private.getvalue())
    tokens = {name: secrets.token_hex(32) for name in ('database', 'operator', 'HospitalA', 'HospitalB', 'live', 'replay')}
    cloud_ip = outputs['CloudPrivateIp']
    cloud = dict(role='cloud', cloud_ip=cloud_ip, private_ip=cloud_ip,
                 database_password=tokens['database'], operator_token=tokens['operator'],
                 hospital_a_token=tokens['HospitalA'], hospital_b_token=tokens['HospitalB'],
                 live_telemetry_token=tokens['live'], replay_telemetry_token=tokens['replay'],
                 hospital_a_key=keys['HospitalA'][0], hospital_b_key=keys['HospitalB'][0])
    configure_over_ssh(outputs['CloudPublicIp'], pins['Cloud'], operator_key, cloud)
    for logical, role in (('HospitalA', 'hospital-a'), ('HospitalB', 'hospital-b')):
        payload = dict(role=role, cloud_ip=cloud_ip, private_ip=outputs[logical + 'PrivateIp'],
                       hospital_token=tokens[logical], tunnel_private_key=keys[logical][1],
                       replay_telemetry_token=tokens['replay'],
                       cloud_host_key=pins['Cloud'])
        configure_over_ssh(outputs[logical + 'PublicIp'], pins[logical], operator_key, payload)
    configure_over_ssh(outputs['CloudPublicIp'], pins['Cloud'], operator_key, {'role': 'cloud verification'}, verify=True)
    print('All eight containers configured; verify both hospital event paths through the Dashboard SSH tunnel.')


def check_capacity(ec2):
    """Conservative preflight against this course's 9-instance/32-vCPU ceilings."""
    active=[]
    for page in ec2.get_paginator('describe_instances').paginate(Filters=[{
        'Name':'instance-state-name','Values':['pending','running','stopping','shutting-down']} ]):
        active.extend(host for reservation in page['Reservations'] for host in reservation['Instances'])
    if len(active)+3>9:
        raise ValueError('Pilot would exceed the course limit of nine active instances; no resources created')
    vcpus=0
    for host in active:
        cpu=host.get('CpuOptions',{})
        if not cpu.get('CoreCount') or not cpu.get('ThreadsPerCore'):
            raise ValueError('Cannot verify current vCPU usage; inspect EC2 before launching')
        vcpus+=cpu['CoreCount']*cpu['ThreadsPerCore']
    if vcpus+6>32:
        raise ValueError('Pilot would exceed the course limit of 32 vCPUs; no resources created')
    return {'active_instances':len(active),'active_vcpus':vcpus,'planned_instances':3,'planned_vcpus':6}


def cleanup_plan(cf, ec2, stack, account, region):
    """Read-only, deliberately incomplete until an operator reviews the inventory.

    Requires launcher Project tags and CloudFormation instance ownership. Attached
    disks are observations, not deletion authorization. Capture before deleting
    stacks because retained disks lose their instance mapping after termination.
    """
    def selected(name):
        state = cf.describe_stacks(StackName=name)['Stacks'][0]
        prefix = f'arn:aws:cloudformation:{region}:{account}:stack/{name}/'
        if (not state['StackId'].startswith(prefix)
                or {t['Key']: t['Value'] for t in state.get('Tags', [])}.get('Project') != stack
                or state['StackStatus'].endswith('_IN_PROGRESS') or state['StackStatus'] == 'DELETE_COMPLETE'):
            raise ValueError('Stack ownership/state is not a stable launcher deployment; inspect manually')
        items = [item for page in cf.get_paginator('list_stack_resources').paginate(StackName=state['StackId'])
                 for item in page['StackResourceSummaries']]
        return state, items

    main, resources = selected(stack)
    source, source_resources = selected(stack + '-source')
    expected = {'Cloud': 'AWS::EC2::Instance', 'HospitalA': 'AWS::EC2::Instance',
                'HospitalB': 'AWS::EC2::Instance', 'CloudSecurity': 'AWS::EC2::SecurityGroup',
                'HospitalSecurity': 'AWS::EC2::SecurityGroup',
                **{f'Api{port}': 'AWS::EC2::SecurityGroupIngress' for port in (8001, 8003, 8004)}}
    current = {key: value for key, value in expected.items() if not key.startswith('Api')}
    current['HospitalTunnelSSH'] = 'AWS::EC2::SecurityGroupIngress'
    actual = {r['LogicalResourceId']: r['ResourceType'] for r in resources}
    if (actual not in (expected, current)
            or len(resources) != len(actual)
            or len(source_resources) != 1 or source_resources[0]['LogicalResourceId'] != 'Source'
            or source_resources[0]['ResourceType'] != 'AWS::S3::Bucket'
            or any(not r.get('PhysicalResourceId') for r in [*resources, *source_resources])):
        raise ValueError('Unexpected/incomplete stack resources; no cleanup assumptions made')
    ids = {r['PhysicalResourceId'] for r in resources if r['ResourceType'] == 'AWS::EC2::Instance'}
    hosts = [host for reservation in ec2.describe_instances(InstanceIds=sorted(ids))['Reservations']
             for host in reservation['Instances']]
    if len(ids) != 3 or len(hosts) != 3 or {host['InstanceId'] for host in hosts} != ids:
        raise ValueError('Expected exactly the selected stack\'s three instances')
    disks, instances = [], []
    for host in hosts:
        tags = {t['Key']: t['Value'] for t in host.get('Tags', [])}
        if tags.get('aws:cloudformation:stack-id') != main['StackId']:
            raise ValueError('Instance ownership differs from the selected stack')
        instances.append({'instance_id': host['InstanceId'], 'state': host['State']['Name']})
        if not host.get('BlockDeviceMappings'):
            raise ValueError('Missing per-instance disk inventory; inspect manually')
        for device in host.get('BlockDeviceMappings', []):
            ebs = device.get('Ebs', {})
            if not ebs.get('VolumeId'):
                raise ValueError('Unidentified instance storage; inspect manually')
            disks.append({'volume_id': ebs['VolumeId'], 'instance_id': host['InstanceId'],
                          'device': device['DeviceName'], 'delete_on_termination': ebs['DeleteOnTermination'],
                          'provenance': 'Attached to selected instance at inventory time; verify before deletion'})
    if not disks or len({disk['volume_id'] for disk in disks}) != len(disks):
        raise ValueError('Missing/ambiguous attached disk inventory')
    volumes = ec2.describe_volumes(VolumeIds=sorted(disk['volume_id'] for disk in disks))['Volumes']
    if {v['VolumeId'] for v in volumes} != {disk['volume_id'] for disk in disks}:
        raise ValueError('Attached disk metadata is incomplete')
    by_id = {v['VolumeId']: v for v in volumes}
    for disk in disks:
        volume = by_id[disk['volume_id']]
        if ({a['InstanceId'] for a in volume.get('Attachments', [])} != {disk['instance_id']}):
            raise ValueError('Disk attachments changed during inventory; rerun and review')
        disk.update(size_gib=volume['Size'], encrypted=volume['Encrypted'], state=volume['State'])
    return {'result': 'inventory_only', 'account': account, 'region': region, 'stack': stack,
            'stack_id': main['StackId'], 'source_stack_id': source['StackId'],
            'instances': sorted(instances, key=lambda v: v['instance_id']),
            'attached_volumes': sorted(disks, key=lambda v: v['volume_id']),
            'source_bucket': source_resources[0]['PhysicalResourceId'],
            'security_groups': sorted(r['PhysicalResourceId'] for r in resources if r['ResourceType'] == 'AWS::EC2::SecurityGroup'),
            'changes_made': False, 'deletion_authorized': False,
            'limitations': ['Capture before deleting either stack; not a post-deletion orphan finder',
                            'Does not search for detached disks, snapshots, copied data or unrelated resources',
                            'Does not read source objects or database contents; no application health claim'],
            'next_step': 'Review docs/runbooks/AWS_TEARDOWN.md, backups, ownership and exact IDs before any manual deletion'}


def selected_instances(cf, ec2, stack, account, region):
    """Resolve only this launcher's three tagged hosts before stop or resume."""
    state = cf.describe_stacks(StackName=stack)["Stacks"][0]
    prefix = f"arn:aws:cloudformation:{region}:{account}:stack/{stack}/"
    tags = {item["Key"]: item["Value"] for item in state.get("Tags", [])}
    if not state["StackId"].startswith(prefix) or tags.get("Project") != stack:
        raise ValueError("Selected stack is not owned by this launcher; no changes made")
    resources = [item for page in cf.get_paginator("list_stack_resources").paginate(StackName=state["StackId"])
                 for item in page["StackResourceSummaries"]]
    instances = {item["LogicalResourceId"]: item["PhysicalResourceId"] for item in resources
                 if item["ResourceType"] == "AWS::EC2::Instance" and item.get("PhysicalResourceId")}
    if set(instances) != {"Cloud", "HospitalA", "HospitalB"} or len(set(instances.values())) != 3:
        raise ValueError("Selected stack does not have exactly this launcher's three hosts; no changes made")
    ids = [instances[name] for name in ("Cloud", "HospitalA", "HospitalB")]
    reservations = ec2.describe_instances(InstanceIds=ids)["Reservations"]
    hosts = [host for reservation in reservations for host in reservation["Instances"]]
    if len(hosts) != 3 or {host["InstanceId"] for host in hosts} != set(ids) or any(
        {tag["Key"]: tag["Value"] for tag in host.get("Tags", [])}.get("aws:cloudformation:stack-id") != state["StackId"]
        for host in hosts
    ):
        raise ValueError("Instance ownership differs from the selected stack; no changes made")
    return ids, hosts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preview", "plan", "launch", "status", "stop", "resume", "cleanup-plan"])
    parser.add_argument("--region", choices=["us-east-1", "us-west-2"], default="us-east-1")
    parser.add_argument('--profile', default='crisisguard-lab')
    parser.add_argument('--expected-account', default='565337149326')
    parser.add_argument('--ssh-key', type=Path, help='Unencrypted OpenSSH/PEM key for fresh-host provisioning; launch only')
    parser.add_argument("--stack", default="crisisguard")
    parser.add_argument("--ssh-cidr", default="")
    parser.add_argument("--subnet-id")
    parser.add_argument("--key-name", default="vockey")
    parser.add_argument("--instance-profile", default="LabInstanceProfile")
    parser.add_argument("--archive", type=Path, default=Path("/source.tar"))
    parser.add_argument("--confirm-account", help="Required account ID for launch/stop/resume")
    args = parser.parse_args()
    if args.command in {"preview", "plan", "launch"}:
        validate_inputs(args)
    elif not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.stack):
        raise ValueError("Invalid stack name")
    if args.command == "preview":
        body = template(args, "vpc-00000000000000000", "subnet-00000000000000000", "ami-00000000000000000",
                        "source-bucket-preview", "source.tar", "0" * 64)
        body["Metadata"] = {"PreviewOnly": True, "Note": "Zero IDs are placeholders, not discovered AWS resources",
                            "ServiceScope": "Eight containers on three hosts; SSH-pinned configuration follows host preparation"}
        print(json.dumps(body, indent=2))
        return
    import boto3
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != args.expected_account:
        raise ValueError('AWS account mismatch; no changes made')
    if args.command == 'cleanup-plan':
        print(json.dumps(cleanup_plan(session.client('cloudformation'), session.client('ec2'),
                                      args.stack, account, args.region), indent=2))
        return
    print(json.dumps({"account": account, "region": args.region, "stack": args.stack}), flush=True)
    if args.command in {"launch", "stop", "resume"} and args.confirm_account != account:
        raise ValueError("Use --confirm-account with the account ID printed by plan/status; no changes made")
    operator_key = None
    if args.command == 'launch':
        if not args.ssh_key or not args.ssh_key.is_file():
            raise ValueError('Launch requires --ssh-key for pinned SSH provisioning; no changes made')
        import paramiko
        operator_key = paramiko.PKey.from_path(args.ssh_key)
    ec2, cf = session.client("ec2"), session.client("cloudformation")
    if args.command in {"status", "stop", "resume"}:
        ids, _ = selected_instances(cf, ec2, args.stack, account, args.region)
        if args.command == "stop":
            ec2.stop_instances(InstanceIds=ids)
            ec2.get_waiter("instance_stopped").wait(InstanceIds=ids)
        elif args.command == "resume":
            ec2.start_instances(InstanceIds=ids)
            ec2.get_waiter("instance_status_ok").wait(InstanceIds=ids)
        reservations = ec2.describe_instances(InstanceIds=ids)["Reservations"]
        for reservation in reservations:
            for host in reservation["Instances"]:
                print(json.dumps({"id": host["InstanceId"], "state": host["State"]["Name"],
                    "name": next((t["Value"] for t in host.get("Tags", []) if t["Key"] == "Name"), ""),
                    "private_ip": host.get("PrivateIpAddress"), "public_ip": host.get("PublicIpAddress")}))
        print("EC2 status only; verify application health through SSH. End Lab separately in AWS Academy.")
        return
    capacity = check_capacity(ec2)
    sha = check_archive(args.archive)
    vpc, subnet, ami = discover(ec2, args)
    summary = {"vpc": vpc, "subnet": subnet, "ami": ami, "ssh_source": args.ssh_cidr,
        "instances": ["t3.small", "t3.micro", "t3.micro"], "total_ebs_gib": 52,
        "source_sha256": sha, "source_bucket_stack": args.stack + "-source",
        "service_scope": "Eight containers: PostgreSQL, three APIs, Controller/MetaGuard, Dashboard, two hospitals",
        "existing_instance_profile": args.instance_profile, "capacity": capacity,
        "note": "Requires course permission for these services/types and iam:PassRole for the existing profile. No IAM resources are created."}
    print(json.dumps(summary, indent=2), flush=True)
    body = template(args, vpc, subnet, ami, "source-bucket-preview", "source.tar", sha)
    cf.validate_template(TemplateBody=json.dumps(body))
    if args.command == "plan":
        print("Read-only plan complete. No resources created or source uploaded.")
        return
    # Refuse updates/duplicates before creating/uploading anything.
    refuse_existing(cf, {args.stack, args.stack + "-source"})
    cf.create_stack(StackName=args.stack + "-source", TemplateBody=json.dumps(source_template()),
                    OnFailure="DO_NOTHING", Tags=[{"Key": "Project", "Value": args.stack}])
    bucket = wait_stack(cf, args.stack + "-source")["Bucket"]
    key = "source/" + sha + ".tar"
    session.client("s3").upload_file(str(args.archive), bucket, key)
    body = template(args, vpc, subnet, ami, bucket, key, sha)
    cf.create_stack(StackName=args.stack, TemplateBody=json.dumps(body), OnFailure="DO_NOTHING",
                    Tags=[{"Key": "Project", "Value": args.stack}])
    outputs = wait_stack(cf, args.stack)
    print(json.dumps(outputs, indent=2))
    provision_hosts(ec2, outputs, operator_key)
    print('Fresh-host configuration completed. CloudFormation CREATE_COMPLETE alone means host preparation, not application readiness. Verify hospital capture, grants and Dashboard event history through SSH.')


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not dump credential-bearing SDK traces.
        print(f"Launcher failed ({type(error).__name__}): {error}", file=sys.stderr)
        sys.exit(1)
