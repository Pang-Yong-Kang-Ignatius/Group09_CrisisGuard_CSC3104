"""Root-only, one-time configuration from JSON on pinned SSH stdin.

No SDK, credentials in arguments, shell interpolation, or existing env overwrite.
The launcher never uploads this private payload to S3 or CloudFormation.
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPO = Path('/home/ubuntu/CrisisGuard')


def address(value):
    ip = ipaddress.IPv4Address(value)
    if not any(ip in ipaddress.ip_network(cidr) for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('Require RFC1918 host address')
    return str(ip)


def token(value):
    if not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ValueError('Require a generated 256-bit token')
    return value


def public_key(value):
    if not re.fullmatch(r'(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/=]+', value):
        raise ValueError('Invalid SSH public key')
    return value


def settings(data):
    role = data['role']
    host, cloud = address(data['private_ip']), address(data['cloud_ip'])
    if role == 'cloud':
        secrets = [token(data[key]) for key in ('database_password', 'operator_token', 'hospital_a_token', 'hospital_b_token',
                                               'live_telemetry_token', 'replay_telemetry_token')]
        if len(set(secrets)) != 6:
            raise ValueError('Credentials must be distinct')
        return dict(CLOUD_PROJECT='crisisguard-aws-cloud', CLOUD_BIND_ADDRESS=host,
                    POSTGRES_USER='crisisguard', POSTGRES_DB='crisisguard', POSTGRES_PASSWORD=secrets[0],
                    CONTROLLER_CLOUD_TOKEN=secrets[1], HOSPITAL_A_CONTROLLER_TOKEN=secrets[2],
                    HOSPITAL_B_CONTROLLER_TOKEN=secrets[3], CLOUD_LIVE_TELEMETRY_TOKEN=secrets[4],
                    CLOUD_REPLAY_TELEMETRY_TOKEN=secrets[5])
    if role not in ('hospital-a', 'hospital-b'):
        raise ValueError('Invalid role')
    # The private address belongs to THIS hospital. SSH encrypts the onward hop.
    return dict(HOSPITAL_PROJECT='crisisguard-aws-' + role, HOSPITAL_BIND_ADDRESS='127.0.0.1',
                HOSPITAL_PORT='8000', SITE_ID='Hospital_A' if role == 'hospital-a' else 'Hospital_B',
                AUTO_REPLAY='1', CLOUD_URL=f'http://{host}:18001/events',
                EMERGENCY_URL=f'http://{host}:18003/events', ADMIN_URL=f'http://{host}:18004/events',
                CONTROLLER_URL=f'http://{host}:18005', HOSPITAL_CONTROLLER_TOKEN=token(data['hospital_token']),
                CLOUD_REPLAY_TELEMETRY_TOKEN=token(data['replay_telemetry_token']))


def authorized_key(key, cloud, reverse_port):
    cloud = address(cloud)
    if reverse_port not in (18000, 18002):
        raise ValueError('Invalid reverse port')
    options = ['restrict', 'port-forwarding', 'command="/usr/sbin/nologin"',
               f'permitlisten="{cloud}:{reverse_port}"']
    options += [f'permitopen="{cloud}:{port}"' for port in (8001, 8003, 8004, 8005)]
    return ','.join(options) + ' ' + public_key(key) + '\n'


def tunnel_unit(data):
    cloud, host = address(data['cloud_ip']), address(data['private_ip'])
    reverse = 18000 if data['role'] == 'hospital-a' else 18002
    forwards = ' '.join(f'-L {host}:{port + 10000}:{cloud}:{port}' for port in (8001, 8003, 8004, 8005))
    return f'''[Unit]
Description=CrisisGuard encrypted private Cloud and hospital channels
After=network-online.target
Wants=network-online.target
[Service]
User=crisisguard-tunnel
ExecStart=/usr/bin/ssh -NT -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/var/lib/crisisguard-tunnel/known_hosts -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -i /var/lib/crisisguard-tunnel/id_rsa {forwards} -R {cloud}:{reverse}:127.0.0.1:8000 crisisguard-tunnel@{cloud}
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
[Install]
WantedBy=multi-user.target
'''


def sshd_settings():
    return ('Match User crisisguard-tunnel\n    GatewayPorts clientspecified\n'
            '    AllowTcpForwarding yes\n    AllowStreamLocalForwarding no\n'
            '    AllowAgentForwarding no\n    X11Forwarding no\n    PermitTTY no\n'
            '    PasswordAuthentication no\n    KbdInteractiveAuthentication no\n'
            '    MaxSessions 0\nMatch all\n')


def write_new(path, text, uid=0, gid=0):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(text)
        os.fchown(stream.fileno(), uid, gid)


def configure(data):
    import pwd
    env = settings(data)  # Validate before any configuration mutation.
    cloud = data['role'] == 'cloud'
    env_path = REPO / 'infrastructure/aws' / ('cloud.env' if cloud else 'hospital.env')
    if env_path.exists():
        raise ValueError('Already configured; existing secrets will not be overwritten')
    # Validate SSH inputs before writing. Every hospital gets a different key.
    if cloud:
        keys = [public_key(data[key]) for key in ('hospital_a_key', 'hospital_b_key')]
        if keys[0] == keys[1]:
            raise ValueError('Tunnel keys must be distinct')
        authorized = ''.join(authorized_key(key, data['cloud_ip'], port)
                             for key, port in zip(keys, (18000, 18002)))
    else:
        pin = public_key(data['cloud_host_key'])
        unit = tunnel_unit(data)
        if not data['tunnel_private_key'].startswith('-----BEGIN RSA PRIVATE KEY-----\n'):
            raise ValueError('Invalid tunnel private key')
    subprocess.run(['useradd', '--system', '--create-home', '--home-dir', '/var/lib/crisisguard-tunnel',
                    '--shell', '/usr/sbin/nologin', '--password', '*', 'crisisguard-tunnel'], check=True)
    user = pwd.getpwnam('crisisguard-tunnel')
    home = Path(user.pw_dir)
    if cloud:
        ssh = home / '.ssh'
        ssh.mkdir(mode=0o700)
        os.chown(ssh, user.pw_uid, user.pw_gid)
        write_new(ssh / 'authorized_keys', authorized, user.pw_uid, user.pw_gid)
        write_new('/etc/ssh/sshd_config.d/60-crisisguard-tunnel.conf',
                  sshd_settings())
        subprocess.run(['sshd', '-t'], check=True)
        subprocess.run(['systemctl', 'reload', 'ssh'], check=True)
    else:
        write_new(home / 'id_rsa', data['tunnel_private_key'], user.pw_uid, user.pw_gid)
        write_new(home / 'known_hosts', f"{data['cloud_ip']} {pin}\n", user.pw_uid, user.pw_gid)
        write_new('/etc/systemd/system/crisisguard-tunnel.service', unit)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', '--now', 'crisisguard-tunnel'], check=True)
        # Prove the encrypted forward works before enabling replay.
        for port in (18001, 18003, 18004, 18005):
            subprocess.run(['curl', '--fail', '--silent', '--retry', '30', '--retry-all-errors',
                            '--retry-delay', '2', '--connect-timeout', '3', '--max-time', '5',
                            f"http://{data['private_ip']}:{port}/health"], check=True, stdout=subprocess.DEVNULL)
    write_new(env_path, ''.join(f'{key}={value}\n' for key, value in env.items()))
    subprocess.run(['bash', str(REPO / 'infrastructure/aws/deploy.sh'),
                    'cloud' if cloud else 'hospital', str(env_path), '--full-system'], check=True)
    Path('/var/lib/crisisguard-bootstrap-complete').touch()


def verify_cloud():
    # Query Dashboard's real aggregation path: HTTP 200 alone can hide an
    # unreachable hospital. Require both distinct sites and event-list schemas.
    code = ("import json,urllib.request; "
            "d=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/hospitals', timeout=20)); "
            "assert set(d)=={'hospital-a','hospital-b'}; "
            "assert all(v.get('status')=='healthy' and isinstance(v.get('events'),list) for v in d.values())")
    command = ['docker', 'compose', '--env-file', str(REPO / 'infrastructure/aws/cloud.env')]
    for name in ('infrastructure/docker/compose.cloud.yaml', 'infrastructure/aws/compose.cloud.override.yaml',
                 'infrastructure/aws/compose.metaguard.cloud.yaml', 'infrastructure/aws/compose.dashboard.cloud.yaml'):
        command += ['-f', str(REPO / name)]
    subprocess.run(command + ['exec', '-T', 'dashboard', 'python', '-c', code], check=True)


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['verify']:
            verify_cloud()
        elif not sys.argv[1:]:
            configure(json.load(sys.stdin))
        else:
            raise ValueError('Invalid mode')
    except Exception:
        # No tracebacks/input/configuration values in cloud-init or SSH logs.
        print('Host configuration failed; inspect service state privately. Existing settings retained.', file=sys.stderr)
        sys.exit(1)
