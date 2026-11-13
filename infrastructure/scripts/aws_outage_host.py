"""Scoped EC2 Docker network fault for a synthetic Hospital A recovery test.

Run with sudo on the hospital host. Requires iptables and systemd. The rollback
timer is installed before the rule; only one container's three cloud API ports
are affected. Never flush chains or block SSH. See docs/reports/AWS_OUTAGE_TEST.md.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
PORTS = (8001, 8003, 8004)


def run(args, check=True):
    return subprocess.run(args, cwd=ROOT, stdin=subprocess.DEVNULL,
                          text=True, capture_output=True, timeout=30, check=check)


def validate(test_id, cloud):
    if not re.fullmatch(r"CG_OUTAGE_[A-Z0-9_]{1,40}", test_id):
        raise ValueError("Use a short CG_OUTAGE_ test identifier")
    address = ipaddress.IPv4Address(cloud)
    if not any(address in ipaddress.ip_network(net) for net in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("Cloud destination must be an RFC1918 private IPv4 address")


def container():
    compose = ['docker', 'compose', '--env-file', 'infrastructure/aws/hospital.env',
               '-f', 'infrastructure/docker/compose.hospital.yaml',
               '-f', 'infrastructure/aws/compose.hospital.override.yaml']
    cid = run(compose + ['ps', '-q', 'hospital']).stdout.strip()
    if not re.fullmatch(r'[a-f0-9]{12,64}', cid):
        raise RuntimeError('Expected one hospital container')
    info = json.loads(run(['docker', 'inspect', cid]).stdout)[0]
    env = dict(item.split('=', 1) for item in info['Config']['Env'] if '=' in item)
    addresses = [v['IPAddress'] for v in info['NetworkSettings']['Networks'].values() if v['IPAddress']]
    if len(addresses) != 1 or not info['State']['Running']:
        raise RuntimeError('Expected one network on a running hospital')
    return cid, addresses[0], env


def rule_args(source, cloud, test_id):
    # These exact selectors are shared by insert/check/delete and rollback.
    ipaddress.IPv4Address(source)
    validate(test_id, cloud)
    return ['-s', source + '/32', '-d', cloud + '/32', '-p', 'tcp', '-m',
            'multiport', '--dports', ','.join(map(str, PORTS)), '-m', 'comment',
            '--comment', test_id, '-j', 'REJECT', '--reject-with', 'tcp-reset']


def paths(test_id):
    return Path('/run') / (test_id + '.json'), Path('/run') / (test_id + '.sh')


def arm(test_id, cloud):
    if os.geteuid() != 0:
        raise RuntimeError('Run with sudo')
    cid, source, env = container()
    if env.get('SITE_ID') != 'Hospital_A' or env.get('AUTO_REPLAY') != '1':
        raise RuntimeError('Fault injection requires Hospital_A with automatic replay')
    if any(env.get(key) != f'http://{cloud}:{port}/events' for key, port in
           [('CLOUD_URL', 8001), ('EMERGENCY_URL', 8003), ('ADMIN_URL', 8004)]):
        raise RuntimeError('Container API destinations do not match requested cloud')
    run(['iptables', '-w', '5', '-S', 'DOCKER-USER'])
    state, script = paths(test_id)
    if state.exists() or script.exists():
        raise RuntimeError('Test already armed; restore it first')
    selectors = rule_args(source, cloud, test_id)
    unit = test_id.lower().replace('_', '-') + '-rollback'
    check = ['iptables', '-w', '5', '-C', 'DOCKER-USER'] + selectors
    delete = ['iptables', '-w', '5', '-D', 'DOCKER-USER'] + selectors
    script.write_text('#!/bin/sh\n' + shlex.join(check) + ' 2>/dev/null && ' + shlex.join(delete) + '\nexit 0\n')
    script.chmod(0o700)
    state.write_text(json.dumps({'selectors': selectors, 'unit': unit, 'container': cid}))
    state.chmod(0o600)
    try:
        run(['systemd-run', '--unit=' + unit, '--on-active=300s', '/bin/sh', str(script)])
        run(['systemctl', 'is-active', unit + '.timer'])
        run(['iptables', '-w', '5', '-I', 'DOCKER-USER', '1'] + selectors)
    except Exception:
        restore(test_id, cloud)
        raise
    return {'armed': True, 'rollback_seconds': 300, 'destination_ports': PORTS,
            'source': source, 'cloud': cloud, 'container': cid}


def restore(test_id, cloud):
    state, script = paths(test_id)
    if not state.exists():
        raise RuntimeError('No saved rule; inspect the host before proceeding')
    saved = json.loads(state.read_text())
    selectors = saved['selectors']
    # Refuse tampered state or destination mismatches.
    if selectors != rule_args(selectors[1].split('/')[0], cloud, test_id):
        raise RuntimeError('Saved rule differs from this test')
    base = ['iptables', '-w', '5']
    def present():
        result = run(base + ['-C', 'DOCKER-USER'] + selectors, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError('Cannot verify firewall state; rollback timer retained')
        return result.returncode == 0
    if present():
        run(base + ['-D', 'DOCKER-USER'] + selectors)
    absent = not present()
    if not absent:
        raise RuntimeError('Outage rule is still present')
    run(['systemctl', 'stop', saved['unit'] + '.timer', saved['unit'] + '.service'], check=False)
    script.unlink(missing_ok=True)
    state.unlink()
    return {'restored': True, 'rule_absent': absent}


def request(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=8) as response:
            return {'code': response.status, 'body': json.load(response)}
    except HTTPError as error:
        return {'code': error.code, 'body': json.loads(error.read())}


def snapshot(test_id, cloud):
    cid, _, env = container()
    code = """import json,urllib.request
out={}
for port in (8001,8003,8004):
 try:
  with urllib.request.urlopen('http://%s:'+str(port)+'/health',timeout=2) as r: out[str(port)]=r.status
 except Exception: out[str(port)]='unreachable'
print(json.dumps(out))
""" % cloud
    probes = json.loads(run(['docker', 'exec', cid, 'python', '-c', code]).stdout)
    records = request('http://127.0.0.1:8000/events')['body']
    counters = run(['iptables-save', '-c']).stdout
    return {'site': env.get('SITE_ID'), 'capture_health': request('http://127.0.0.1:8000/')['code'],
            'cloud_health_from_container': probes,
            'test_events': [{'event_id': r['event_id'], 'status': r['status']} for r in records if r['event_id'].startswith(test_id)],
            'recovery': request('http://127.0.0.1:8000/recovery/status')['body'],
            'firewall_counters': [line for line in counters.splitlines() if test_id in line]}


def capture(test_id):
    _, _, env = container()
    site = env['SITE_ID']
    if site not in ('Hospital_A', 'Hospital_B'):
        raise RuntimeError('Expected a configured synthetic-test hospital')
    suffix = 'A' if site == 'Hospital_A' else 'B'
    previous = None
    results = []
    for index, kind in enumerate(('patient_registration', 'encounter_creation', 'emergency_record', 'patient_discharge'), 1):
        event_id = f'{test_id}_{suffix}_{index:02}'
        event = {'event_id': event_id, 'site_id': site, 'event_type': kind,
                 'base_priority': 5 if kind == 'emergency_record' else 1,
                 'prerequisite_ids': [previous] if previous else [],
                 'payload': {'patient_id': 'SYNTHETIC_' + test_id + '_' + suffix}}
        response = request('http://127.0.0.1:8000/capture', event)
        if response['code'] != 201:
            raise RuntimeError('Capture did not create a new synthetic event')
        results.append({'event_id': event_id, 'code': response['code']})
        previous = event_id
    return results


def cloud_state(test_id, cloud):
    results = []
    for suffix in ('A', 'B'):
        for index, port in enumerate((8001, 8001, 8003, 8004), 1):
            event_id = f'{test_id}_{suffix}_{index:02}'
            result = request(f'http://{cloud}:{port}/events/{event_id}')
            results.append({'event_id': event_id, **result})
    return results


def duplicates(test_id, cloud):
    ports = {'patient_registration': 8001, 'encounter_creation': 8001,
             'emergency_record': 8003, 'patient_discharge': 8004}
    records = request('http://127.0.0.1:8000/events')['body']
    results = []
    for record in records:
        if record['event_id'].startswith(test_id + '_'):
            if record['status'] != 'applied':
                raise RuntimeError('Wait for automatic recovery before duplicate checks')
            body = {key: value for key, value in record.items() if key != 'status'}
            result = request(f"http://{cloud}:{ports[record['event_type']]}/events", body)
            if result['code'] != 200 or result['body'].get('result') != 'already_applied':
                raise RuntimeError('Expected an idempotent duplicate acknowledgement')
            results.append({'event_id': record['event_id'], **result})
    if len(results) != 4:
        raise RuntimeError('Expected the four synthetic events for this hospital')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['arm', 'restore', 'snapshot', 'capture', 'cloud-state', 'duplicates'])
    parser.add_argument('--test-id', required=True)
    parser.add_argument('--cloud-host', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    validate(args.test_id, args.cloud_host)
    if args.action == 'capture':
        result = capture(args.test_id)
    else:
        result = globals()[args.action.replace('-', '_')](args.test_id, args.cloud_host)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
