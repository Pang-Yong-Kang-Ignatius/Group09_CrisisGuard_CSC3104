"""Real OpenSSH contract test, ONLY inside an isolated Ubuntu test container.

Synthetic keys and HTTP responders; no AWS, host Docker socket, or real secrets.
The caller retains the exited container. This does not provision application data.
"""
import http.server
import json
import os
import pwd
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infrastructure.aws import configure_host as config


def main():
    if sys.argv[1:] != ['--isolated-container'] or not Path('/.dockerenv').exists():
        raise RuntimeError('Run only in the dedicated Ubuntu Docker test fixture')
    host = socket.gethostbyname(socket.gethostname())
    work = Path(tempfile.mkdtemp(prefix='cg-ssh-'))
    processes, servers = [], []
    def run(argv):
        return subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    def key(name):
        path = work/name
        run(['ssh-keygen', '-q', '-t', 'rsa', '-b', '3072', '-N', '', '-f', str(path)])
        return path, ' '.join(path.with_suffix('.pub').read_text().split()[:2])
    host_key = work/'host'
    run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(host_key)])
    a, a_public = key('a')
    b, b_public = key('b')
    run(['useradd', '--system', '--create-home', '--home-dir', '/var/lib/crisisguard-tunnel',
         '--shell', '/usr/sbin/nologin', '--password', '*', 'crisisguard-tunnel'])
    ssh = Path('/var/lib/crisisguard-tunnel/.ssh')
    ssh.mkdir(mode=0o700)
    (ssh/'authorized_keys').write_text(config.authorized_key(a_public, host, 18000)
                                       + config.authorized_key(b_public, host, 18002))
    os.chmod(ssh/'authorized_keys', 0o600)
    user = pwd.getpwnam('crisisguard-tunnel')
    os.chown(ssh, user.pw_uid, user.pw_gid)
    os.chown(ssh/'authorized_keys', user.pw_uid, user.pw_gid)
    Path('/etc/ssh/sshd_config.d/60-crisisguard-tunnel.conf').write_text(config.sshd_settings())
    Path('/run/sshd').mkdir(exist_ok=True)
    run(['/usr/sbin/sshd', '-t'])
    known = work/'known_hosts'
    known.write_text(f'[{host}]:2222 ' + ' '.join(host_key.with_suffix('.pub').read_text().split()[:2]) + '\n')
    def responder(address, port, label):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(label.encode())
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer((address,port), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    for port in (8001,8003,8004,8005):
        responder(host, port, 'Cloud-'+str(port))
    responder('127.0.0.1',8000,'Hospital_A')
    responder('127.0.0.1',8002,'Hospital_B')
    sshd = subprocess.Popen(['/usr/sbin/sshd','-D','-e','-p','2222','-h',str(host_key)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    processes.append(sshd)
    def get(port, loopback=False):
        # Deliberately ignore any proxy environment in this synthetic container.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{'127.0.0.1' if loopback else host}:{port}/", timeout=1) as response:
            return response.read().decode()
    def wait(check):
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                if check(): return
            except (OSError, ValueError): pass
            time.sleep(.1)
        raise RuntimeError('SSH transport readiness failed')
    def base(identity):
        return ['ssh','-T','-p','2222','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                '-o',f'UserKnownHostsFile={known}','-o','ExitOnForwardFailure=yes',
                '-o','ConnectTimeout=3','-i',str(identity)]
    destination = 'crisisguard-tunnel@'+host
    try:
        wait(lambda: socket.create_connection((host,2222),timeout=1).close() is None)
        good_pin = known.read_text()
        known.write_text(f'[{host}]:2222 {b_public}\n')
        assert subprocess.run(base(a)+['-N',destination], capture_output=True, timeout=8).returncode != 0
        known.write_text(good_pin)
        # The wrong hospital cannot acquire B's unoccupied reverse port.
        denied = subprocess.run(base(a)+['-N','-R',f'{host}:18002:127.0.0.1:8002',destination],
                                capture_output=True, timeout=8)
        assert denied.returncode != 0 and b'remote port forwarding failed' in denied.stderr, denied.stderr.decode()
        assert subprocess.run(base(a)+[destination,'true'], capture_output=True, timeout=8).returncode != 0
        # Exercise the production unit's exact four forwards and reverse route.
        data = dict(role='hospital-a',private_ip=host,cloud_ip=host)
        unit = config.tunnel_unit(data)
        unit_path = work/'crisisguard-tunnel.service'
        unit_path.write_text(unit)
        run(['systemd-analyze','verify',str(unit_path)])
        command = next(line.split('=',1)[1] for line in unit.splitlines() if line.startswith('ExecStart='))
        argv = shlex.split(command.replace('/var/lib/crisisguard-tunnel/id_rsa',str(a))
                          .replace('/var/lib/crisisguard-tunnel/known_hosts',str(known)))
        argv[1:1] = ['-p','2222']
        client_a = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        processes.append(client_a)
        wait(lambda: get(18000)=='Hospital_A')
        for port in (8001,8003,8004,8005):
            assert get(port+10000)=='Cloud-'+str(port)
        # B shares this TEST namespace, so only its reverse leg is needed here.
        # Real hospitals each have their own namespace and all four forwards.
        client_b = subprocess.Popen(base(b)+['-N','-R',f'{host}:18002:127.0.0.1:8002',destination],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        processes.append(client_b)
        wait(lambda: get(18002)=='Hospital_B')
        client_a.terminate()
        client_a.wait(timeout=5)
        assert get(8000,loopback=True)=='Hospital_A'  # Local capture survives.
        wait(lambda: get(18002)=='Hospital_B')  # Peer hospital stays online.
        restarted = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        processes.append(restarted)
        wait(lambda: get(18000)=='Hospital_A')
        print(json.dumps({'result':'passed','transport':'real Ubuntu OpenSSH',
                          'checks':['four Cloud forwards','two distinct hospital reverse routes',
                                    'wrong host key denied','cross-hospital reverse listener denied','shell denied',
                                    'systemd unit verified','local capture during disconnect','reconnect'],
                          'aws_used':False,'scope':'Synthetic transport in one isolated network namespace; not three EC2 hosts'}))
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        for server in servers:
            server.shutdown()


if __name__ == '__main__':
    main()
