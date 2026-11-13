"""Fresh-host provisioning contracts; all AWS/SSH operations are mocked."""
import base64
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from infrastructure.aws import configure_host as host, launch_lab as lab
from infrastructure.scripts import audit_deployment as audit
from tests.unit.test_metaguard_deployment import cloud_config
from tests.unit.test_deployment_audit import service


def payload(role='cloud'):
    return dict(role=role, private_ip='172.31.1.2' if role == 'cloud' else '172.31.1.3',
                cloud_ip='172.31.1.2', database_password='a'*64, operator_token='b'*64,
                hospital_a_token='c'*64, hospital_b_token='d'*64, hospital_token='c'*64,
                live_telemetry_token='e'*64, replay_telemetry_token='f'*64)


@pytest.mark.parametrize('role,site', [('hospital-a', 'Hospital_A'), ('hospital-b', 'Hospital_B')])
def test_each_hospital_settings_only_include_own_identity(role, site):
    env = host.settings(payload(role))
    assert env['SITE_ID'] == site
    assert env['HOSPITAL_BIND_ADDRESS'] == '127.0.0.1'
    assert env['HOSPITAL_CONTROLLER_TOKEN'] == 'c'*64
    assert env['CLOUD_REPLAY_TELEMETRY_TOKEN'] == 'f'*64
    assert 'CLOUD_LIVE_TELEMETRY_TOKEN' not in env
    assert 'POSTGRES_PASSWORD' not in env and 'CONTROLLER_CLOUD_TOKEN' not in env
    assert 'HOSPITAL_A_CONTROLLER_TOKEN' not in env and 'HOSPITAL_B_CONTROLLER_TOKEN' not in env
    for key, port in [('CLOUD_URL',18001), ('EMERGENCY_URL',18003), ('ADMIN_URL',18004), ('CONTROLLER_URL',18005)]:
        assert env[key].startswith(f'http://172.31.1.3:{port}')
    assert env['AUTO_REPLAY'] == '1'


@pytest.mark.parametrize('fault', ['public_ip', 'reused_token', 'weak_token', 'newline'])
def test_configuration_rejects_unsafe_values_before_writes(fault):
    data = payload()
    if fault == 'public_ip': data['private_ip'] = '8.8.8.8'
    elif fault == 'reused_token': data['operator_token'] = data['hospital_a_token']
    elif fault == 'weak_token': data['operator_token'] = 'short'
    else: data['hospital_b_token'] += '\nINJECTED=true'
    with pytest.raises(ValueError): host.settings(data)


def test_key_acl_limits_hospital_to_own_reverse_listener_and_cloud_services():
    acl = host.authorized_key('ssh-rsa YWJjZA==', '172.31.1.2', 18000)
    assert 'restrict,port-forwarding,command="/usr/sbin/nologin"' in acl
    assert 'permitlisten="172.31.1.2:18000"' in acl and '18002' not in acl
    assert acl.count('permitopen=') == 4
    assert '5432' not in acl and '8080' not in acl and '0.0.0.0' not in acl
    with pytest.raises(ValueError): host.authorized_key('ssh-rsa YWJjZA==\nother', '172.31.1.2', 18000)


def test_tunnel_survives_partition_and_never_uses_public_host_or_unpinned_key():
    unit = host.tunnel_unit(payload('hospital-b'))
    assert '-R 172.31.1.2:18002:127.0.0.1:8000' in unit
    assert '-L 172.31.1.3:18005:172.31.1.2:8005' in unit
    assert 'StrictHostKeyChecking=yes' in unit and 'ExitOnForwardFailure=yes' in unit
    assert 'ServerAliveInterval=15' in unit and 'Restart=always' in unit
    assert 'crisisguard-tunnel@172.31.1.2' in unit
    assert 'User=crisisguard-tunnel' in unit and '0.0.0.0' not in unit


def test_private_file_creation_never_overwrites(tmp_path, monkeypatch):
    path = tmp_path/'existing.env'
    path.write_text('original')
    with pytest.raises(FileExistsError): host.write_new(path, 'replacement')
    assert path.read_text() == 'original'


@pytest.mark.parametrize('encoded', [True, False])
def test_console_pin_uses_authenticated_instance_id(encoded):
    ec2 = Mock()
    output = 'boot\n[123.4] cloud-init[100]: CG_SSH_HOST_KEY ssh-ed25519 YWJjZA==\n'
    ec2.get_console_output.return_value = {'Output': base64.b64encode(output.encode()).decode() if encoded else output}
    assert lab.console_host_key(ec2, 'i-owned') == 'ssh-ed25519 YWJjZA=='
    ec2.get_console_output.assert_called_once_with(InstanceId='i-owned', Latest=True)


def test_console_pin_does_not_fall_back_to_ssh_keyscan(monkeypatch):
    ec2 = Mock()
    ec2.get_console_output.return_value = {'Output': ''}
    monkeypatch.setattr(lab.time, 'monotonic', Mock(side_effect=[0,0,301]))
    monkeypatch.setattr(lab.time, 'sleep', Mock())
    with pytest.raises(ValueError, match='no secrets sent'): lab.console_host_key(ec2, 'i-owned')


def test_az_discovery_skips_unsupported_first_subnet():
    ec2 = Mock()
    ec2.describe_subnets.return_value = {'Subnets': [
        {'SubnetId':'subnet-a','VpcId':'vpc-test','AvailabilityZone':'us-east-1a'},
        {'SubnetId':'subnet-b','VpcId':'vpc-test','AvailabilityZone':'us-east-1b'}]}
    def pages(**kwargs):
        zone = kwargs['Filters'][0]['Values'][0]
        types = ['t3.micro'] if zone.endswith('a') else ['t3.micro','t3.small']
        return [{'InstanceTypeOfferings':[{'InstanceType': kind} for kind in types]}]
    ec2.get_paginator.return_value.paginate.side_effect = pages
    ec2.describe_route_tables.return_value = {'RouteTables':[{'Routes':[{
        'DestinationCidrBlock':'0.0.0.0/0','GatewayId':'igw-test','State':'active'}]}]}
    ec2.describe_images.return_value = {'Images':[{'ImageId':'ami-test','CreationDate':'2026-11-01'}]}
    assert lab.discover(ec2, Mock(subnet_id=None, key_name='vockey')) == ('vpc-test','subnet-b','ami-test')


def test_explicit_unsupported_az_fails_without_provisioning():
    ec2 = Mock()
    ec2.describe_subnets.return_value = {'Subnets':[{
        'SubnetId':'subnet-a','VpcId':'vpc-test','AvailabilityZone':'us-east-1a'}]}
    ec2.describe_route_tables.return_value = {'RouteTables':[{'Routes':[{
        'DestinationCidrBlock':'0.0.0.0/0','GatewayId':'igw-test','State':'active'}]}]}
    ec2.get_paginator.return_value.paginate.return_value = [{'InstanceTypeOfferings':[]}]
    with pytest.raises(ValueError, match='does not offer'):
        lab.discover(ec2, Mock(subnet_id='subnet-a'))
    ec2.describe_images.assert_not_called()


def test_secret_distribution_pins_all_hosts_before_sending_and_separates_credentials(monkeypatch):
    paramiko = Mock()
    def key_factory(bits):
        key = Mock()
        key.get_name.return_value = 'ssh-rsa'
        key.get_base64.return_value = 'key-' + str(key_factory.count)
        key.write_private_key.side_effect = lambda stream: stream.write('private-' + key.get_base64())
        key_factory.count += 1
        return key
    key_factory.count = 0
    paramiko.RSAKey.generate.side_effect = key_factory
    monkeypatch.setitem(sys.modules, 'paramiko', paramiko)
    pins = []
    monkeypatch.setattr(lab, 'console_host_key', lambda ec2, instance: pins.append(instance) or 'ssh-ed25519 pin')
    calls = []
    def send(*args, **kwargs):
        assert len(pins) == 3
        calls.append((args, kwargs))
    monkeypatch.setattr(lab, 'configure_over_ssh', send)
    outputs = {name+suffix: name+suffix for name in ('Cloud','HospitalA','HospitalB')
               for suffix in ('Id','PublicIp','PrivateIp')}
    lab.provision_hosts(Mock(), outputs, Mock())
    cloud, a, b = [call[0][3] for call in calls[:3]]
    assert a['hospital_token'] == cloud['hospital_a_token']
    assert b['hospital_token'] == cloud['hospital_b_token']
    assert a['hospital_token'] != b['hospital_token']
    assert a['tunnel_private_key'] != b['tunnel_private_key']
    for hospital in (a,b):
        assert not {'database_password','operator_token','hospital_a_token','hospital_b_token'} & hospital.keys()
        assert hospital['replay_telemetry_token'] == cloud['replay_telemetry_token']
        assert 'live_telemetry_token' not in hospital
    assert calls[-1][1] == {'verify':True}


def test_ssh_sends_secrets_only_on_pinned_channel_stdin(monkeypatch):
    paramiko = Mock()
    monkeypatch.setitem(sys.modules, 'paramiko', paramiko)
    client = paramiko.SSHClient.return_value
    stdin, stdout, stderr = Mock(), Mock(), Mock()
    client.exec_command.return_value = (stdin, stdout, stderr)
    stdout.channel.exit_status_ready.return_value = True
    stdout.channel.recv_exit_status.return_value = 0
    operator = Mock()
    lab.configure_over_ssh('203.0.113.2', 'ssh-ed25519 YWJjZA==', operator, {'role':'cloud', 'token':'synthetic-private'})
    client.get_host_keys.return_value.add.assert_called_once()
    client.set_missing_host_key_policy.assert_not_called()
    client.connect.assert_called_once_with('203.0.113.2', username='ubuntu', pkey=operator,
        allow_agent=False, look_for_keys=False, timeout=15, banner_timeout=30, auth_timeout=30)
    assert 'synthetic-private' not in client.exec_command.call_args.args[0]
    assert 'synthetic-private' in stdin.write.call_args.args[0]
    stdin.channel.shutdown_write.assert_called_once()
    client.close.assert_called_once()


def test_account_mismatch_stops_before_other_services(monkeypatch):
    sdk = Mock()
    sdk.Session.return_value.client.return_value.get_caller_identity.return_value = {'Account':'000000000000'}
    monkeypatch.setitem(sys.modules, 'boto3', sdk)
    monkeypatch.setattr(sys, 'argv', ['launch_lab.py', 'status'])
    with pytest.raises(ValueError, match='account mismatch'):
        lab.main()
    sdk.Session.return_value.client.assert_called_once_with('sts')


@pytest.mark.skipif(os.name != 'nt', reason='PowerShell wrapper requires Windows')
def test_powershell_named_profile_is_passed_only_at_runtime(tmp_path):
    credentials = tmp_path/'synthetic-credentials'
    credentials.write_text('[fixture-lab]\n')
    command = r'''
function docker {
    if ($args[0] -eq 'build') {
        if ($args -contains '--profile') { throw 'Profile entered build args' }
        $global:LASTEXITCODE = 0; return
    }
    if ($args[0] -eq 'run' -and $args -contains 'status') {
        $profileIndex = [Array]::IndexOf($args, '--profile')
        if ($profileIndex -lt 0 -or $args[$profileIndex + 1] -ne 'fixture-lab') { throw 'Wrong profile' }
        if ($args -notcontains '--expected-account' -or $args -notcontains '565337149326') { throw 'Missing account guard' }
        if (-not ($args | Where-Object { $_ -like '*target=/run/aws-credentials,readonly' })) { throw 'Missing readonly credential mount' }
        $global:LASTEXITCODE = 0; return 'runtime-profile-PASS'
    }
    throw 'Unexpected Docker command'
}
& .\infrastructure\aws\run-lab.ps1 -Command status -Profile fixture-lab -CredentialsFile $env:CG_TEST_CREDENTIAL_FILE
if (-not $?) { exit 1 }
'''
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
                            env={**os.environ, 'CG_TEST_CREDENTIAL_FILE':str(credentials)}, timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'runtime-profile-PASS' in result.stdout


def dashboard_config():
    config = cloud_config()
    controller = config['services']['controller']['environment']
    dashboard = service()
    dashboard.update(ports=[{'host_ip':'127.0.0.1','target':8080,'published':'8080'}], environment={
        'DATABASE_URL':controller['DATABASE_URL'], 'METAGUARD_REQUIRED':'1',
        'DASHBOARD_CONTROLLER_TOKEN':controller['CONTROLLER_CLOUD_TOKEN'],
        'DASHBOARD_CONTROLLER_URL':'http://controller:8000',
        'DASHBOARD_HOSPITAL_A_URL':'http://172.31.1.2:18000',
        'DASHBOARD_HOSPITAL_B_URL':'http://172.31.1.2:18002'})
    config['services']['dashboard'] = dashboard
    return config


@pytest.mark.parametrize('fault', [None,'public_dashboard','same_hospital','wrong_operator','unprotected'])
def test_dashboard_audit_requires_both_private_hospitals_and_protection(fault):
    config = dashboard_config()
    dashboard = config['services']['dashboard']
    if fault == 'public_dashboard': dashboard['ports'][0]['host_ip'] = '0.0.0.0'
    elif fault == 'same_hospital': dashboard['environment']['DASHBOARD_HOSPITAL_B_URL'] = 'http://172.31.1.2:18000'
    elif fault == 'wrong_operator': dashboard['environment']['DASHBOARD_CONTROLLER_TOKEN'] = 'wrong'
    elif fault == 'unprotected': dashboard['environment']['METAGUARD_REQUIRED'] = '0'
    if fault:
        with pytest.raises(ValueError): audit.audit('cloud', config)
    else:
        assert audit.audit('cloud', config)['result'] == 'passed'
