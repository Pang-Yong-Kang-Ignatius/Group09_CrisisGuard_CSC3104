import argparse
import importlib.util
import io
from pathlib import Path
import tarfile
import sys
import json
import re
from unittest.mock import Mock

import pytest


spec = importlib.util.spec_from_file_location("launch_lab", Path(__file__).parents[2] / "infrastructure/aws/launch_lab.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def args(**overrides):
    return argparse.Namespace(**dict(stack="crisisguard", ssh_cidr="8.8.8.8/32",
                                    key_name="vockey", instance_profile="LabInstanceProfile", **overrides))


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "8.8.8.0/24", "192.168.1.2/32", "::1/128"])
def test_rejects_broad_or_nonpublic_ssh(cidr):
    options = args()
    options.ssh_cidr = cidr
    with pytest.raises(ValueError):
        launcher.validate_inputs(options)


def test_private_api_rules_and_bounded_resources():
    body = launcher.template(args(), "vpc-test", "subnet-test", "ami-test", "bucket", "source.tar", "a"*64)
    resources = body["Resources"]
    hosts = [r for r in resources.values() if r["Type"] == "AWS::EC2::Instance"]
    assert len(hosts) == 3
    assert sum(r["Properties"]["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] for r in hosts) == 52
    assert not any(r["Type"].startswith("AWS::IAM::") for r in resources.values())
    for host in hosts:
        assert host["Properties"]["MetadataOptions"]["HttpTokens"] == "required"
        assert host["Properties"]["BlockDeviceMappings"][0]["Ebs"]["DeleteOnTermination"] is False
        assert host["CreationPolicy"]["ResourceSignal"]["Timeout"] == "PT30M"
    rule = resources['HospitalTunnelSSH']['Properties']
    assert rule['FromPort'] == rule['ToPort'] == 22
    assert 'CidrIp' not in rule
    assert rule['SourceSecurityGroupId'] == {"Fn::GetAtt": ["HospitalSecurity", "GroupId"]}
    assert all(r['Properties']['FromPort'] == 22 for r in resources.values()
               if r['Type'] == 'AWS::EC2::SecurityGroupIngress')
    data = resources["HospitalA"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    assert 'bootstrap_lab.sh prepare' in data
    assert "AWS_SECRET_ACCESS_KEY" not in data
    assert "POSTGRES_PASSWORD" not in data
    assert "hashlib.sha256" in data
    assert len(data.encode()) < 16384


def test_source_bucket_blocks_public_access():
    bucket = launcher.source_template()["Resources"]["Source"]
    assert all(bucket["Properties"]["PublicAccessBlockConfiguration"].values())
    assert bucket["DeletionPolicy"] == "Retain"


@pytest.mark.parametrize("name,kind", [("../escape", "file"), ("infrastructure/aws/cloud.env", "file"),
    (".aws/credentials", "file"), ("hospital.db-wal", "file"), ("key.pem", "file"),
    ("safe-link", "symlink"), ("C:\\escape", "file"), ("local.private.json", "file"),
    (".env.production", "file"), ("credentials", "file")])
def test_archive_rejects_private_or_unsafe_members(tmp_path,name,kind):
    path = tmp_path / "source.tar"
    with tarfile.open(path,"w") as archive:
        entry = tarfile.TarInfo(name)
        if kind=="symlink":
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/etc/passwd"
        archive.addfile(entry,io.BytesIO(b""))
    with pytest.raises(ValueError):
        launcher.check_archive(path)


def test_existing_stack_on_later_page_is_rejected():
    cf = Mock()
    cf.get_paginator.return_value.paginate.return_value = [
        {"StackSummaries": []},
        {"StackSummaries": [{"StackName":"crisisguard-source","StackStatus":"CREATE_FAILED"}]}]
    with pytest.raises(ValueError):
        launcher.refuse_existing(cf,{"crisisguard","crisisguard-source"})
    cf.create_stack.assert_not_called()


def test_rejects_subnet_without_internet_gateway():
    ec2 = Mock()
    options = args(subnet_id="subnet-test")
    ec2.describe_subnets.return_value={"Subnets":[{"SubnetId":"subnet-test","VpcId":"vpc-test"}]}
    ec2.describe_route_tables.return_value={"RouteTables":[{"Routes":[{
        "DestinationCidrBlock":"0.0.0.0/0","NatGatewayId":"nat-test","State":"active"}]}]}
    with pytest.raises(ValueError,match="internet-gateway"):
        launcher.discover(ec2,options)
    ec2.describe_images.assert_not_called()


def test_launch_account_mismatch_cannot_create_or_upload(monkeypatch):
    sdk = Mock()
    session = sdk.Session.return_value
    session.client.return_value.get_caller_identity.return_value = {"Account":"123456789012"}
    monkeypatch.setitem(sys.modules,"boto3",sdk)
    monkeypatch.setattr(sys,"argv",["launch_lab.py","launch","--ssh-cidr","8.8.8.8/32",
                                    "--confirm-account","999999999999"])
    with pytest.raises(ValueError,match="no changes"):
        launcher.main()
    assert session.client.call_count == 1  # STS only, no provisioning clients.
    session.client.return_value.create_stack.assert_not_called()
    session.client.return_value.upload_file.assert_not_called()


def test_preview_uses_no_aws_sdk(monkeypatch,capsys):
    sdk=Mock()
    monkeypatch.setitem(sys.modules,"boto3",sdk)
    monkeypatch.setattr(sys,"argv",["launch_lab.py","preview","--ssh-cidr","8.8.8.8/32"])
    launcher.main()
    sdk.Session.assert_not_called()
    body=json.loads(capsys.readouterr().out)
    assert body['Metadata']['PreviewOnly'] is True
    for resource in body['Resources'].values():
        if resource['Type']=='AWS::EC2::Instance':
            assert re.fullmatch(r'ami-[a-f0-9]{17}',resource['Properties']['ImageId'])
            assert re.fullmatch(r'subnet-[a-f0-9]{17}',resource['Properties']['NetworkInterfaces'][0]['SubnetId'])


def test_source_archive_accepts_required_committed_files(tmp_path):
    from infrastructure.aws.package_source import SOURCE_FILES
    source = Path(__file__).parents[2]
    path=tmp_path/'source.tar'
    with tarfile.open(path,'w') as archive:
        for relative in SOURCE_FILES:
            archive.add(source / relative, arcname=relative)
    assert len(launcher.check_archive(path))==64


def test_plan_is_read_only_even_with_valid_source(monkeypatch):
    sdk=Mock()
    sts,ec2,cf,s3=Mock(),Mock(),Mock(),Mock()
    sdk.Session.return_value.client.side_effect={'sts':sts,'ec2':ec2,'cloudformation':cf,'s3':s3}.__getitem__
    sts.get_caller_identity.return_value={'Account':'565337149326'}
    monkeypatch.setitem(sys.modules,'boto3',sdk)
    monkeypatch.setattr(sys,'argv',['launch_lab.py','plan','--ssh-cidr','8.8.8.8/32'])
    monkeypatch.setattr(launcher,'check_archive',lambda path: 'a'*64)
    monkeypatch.setattr(launcher,'check_capacity',lambda client: {'active_instances':0,'active_vcpus':0})
    monkeypatch.setattr(launcher,'discover',lambda client,options: ('vpc-test','subnet-test','ami-test'))
    launcher.main()
    cf.validate_template.assert_called_once()
    sdk.Session.assert_called_once_with(profile_name='crisisguard-lab', region_name='us-east-1')
    cf.create_stack.assert_not_called()
    s3.upload_file.assert_not_called()


def test_embedded_bootstrap_python_compiles():
    for role,logical in [('cloud','Cloud'),('hospital-a','HospitalA'),('hospital-b','HospitalB')]:
        script=launcher.user_data(role,logical,'bucket','source.tar','a'*64)['Fn::Base64']['Fn::Sub']
        for label in ['CG_SIGNAL','CG_DOWNLOAD']:
            code=script.split("<<'"+label+"'\n",1)[1].split('\n'+label,1)[0]
            compile(code,'bootstrap-'+label,'exec')


@pytest.mark.parametrize('count,cores,message',[(7,1,'nine active'),(1,16,'32 vCPUs'),(1,0,'Cannot verify')])
def test_capacity_guard_blocks_before_creation(count,cores,message):
    client=Mock()
    hosts=[{'CpuOptions':{'CoreCount':cores,'ThreadsPerCore':2}} for _ in range(count)]
    client.get_paginator.return_value.paginate.return_value=[{'Reservations':[{'Instances':hosts}]}]
    with pytest.raises(ValueError,match=message):
        launcher.check_capacity(client)
    client.run_instances.assert_not_called()


def test_capacity_counts_all_pages():
    client=Mock()
    host={'CpuOptions':{'CoreCount':1,'ThreadsPerCore':2}}
    client.get_paginator.return_value.paginate.return_value=[{'Reservations':[{'Instances':[host]}]},
                                                          {'Reservations':[{'Instances':[host,host]}]}]
    assert launcher.check_capacity(client)=={'active_instances':3,'active_vcpus':6,'planned_instances':3,'planned_vcpus':6}


def test_stop_target_requires_launcher_stack_and_instance_ownership():
    cf, ec2 = Mock(), Mock()
    stack_id = 'arn:aws:cloudformation:us-east-1:123456789012:stack/cg-prof-test/abc'
    cf.describe_stacks.return_value = {'Stacks': [{
        'StackId': stack_id, 'Tags': [{'Key': 'Project', 'Value': 'cg-prof-test'}]
    }]}
    cf.get_paginator.return_value.paginate.return_value = [{
        'StackResourceSummaries': [
            {'LogicalResourceId': name, 'ResourceType': 'AWS::EC2::Instance', 'PhysicalResourceId': f'i-{name}'}
            for name in ('Cloud', 'HospitalA', 'HospitalB')
        ]
    }]
    ec2.describe_instances.return_value = {'Reservations': [{'Instances': [
        {'InstanceId': f'i-{name}', 'Tags': [{'Key': 'aws:cloudformation:stack-id', 'Value': stack_id}]}
        for name in ('Cloud', 'HospitalA', 'HospitalB')
    ]}]}
    ids, _ = launcher.selected_instances(cf, ec2, 'cg-prof-test', '123456789012', 'us-east-1')
    assert ids == ['i-Cloud', 'i-HospitalA', 'i-HospitalB']
    cf.describe_stacks.return_value['Stacks'][0]['Tags'][0]['Value'] = 'other-project'
    with pytest.raises(ValueError, match='not owned'):
        launcher.selected_instances(cf, ec2, 'cg-prof-test', '123456789012', 'us-east-1')
    cf.describe_stacks.return_value['Stacks'][0]['Tags'][0]['Value'] = 'cg-prof-test'
    ec2.describe_instances.return_value['Reservations'][0]['Instances'][0]['Tags'][0]['Value'] = 'other-stack'
    with pytest.raises(ValueError, match='Instance ownership'):
        launcher.selected_instances(cf, ec2, 'cg-prof-test', '123456789012', 'us-east-1')
    ec2.stop_instances.assert_not_called()
    ec2.start_instances.assert_not_called()
