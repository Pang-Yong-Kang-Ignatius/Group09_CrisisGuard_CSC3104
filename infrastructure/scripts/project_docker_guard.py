"""Read-only, direct-daemon transport and versioned preservation snapshots.

Windows Docker Desktop inventory is obtained by its bundled Linux CLI through
the actual dockerd Unix socket, not the Windows API cache. No helper container,
dependency installation, service restart or network mutation is used.
"""
from datetime import datetime
import json
import os
import re
import subprocess


class DockerGuardError(RuntimeError):
    pass


# Read process identity around EVERY request. A daemon restart during an API
# call invalidates its result even when the persisted Docker daemon ID is equal.
DIRECT_SCRIPT = r'''
set -eu
unset DOCKER_CONTEXT DOCKER_HOST DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_API_VERSION
epoch() {
    pids=$(pidof dockerd) || exit 71
    case "$pids" in ''|*[!0-9]*) exit 71 ;; esac
    [ "$(cat /proc/"$pids"/comm)" = dockerd ] || exit 71
    boot=$(cat /proc/sys/kernel/random/boot_id)
    ticks=$(awk '{print $22}' /proc/"$pids"/stat)
    printf '%s|%s|%s' "$boot" "$pids" "$ticks"
}
before=$(epoch)
pid=$(printf '%s' "$before" | cut -d '|' -f 2)
socket=/proc/$pid/root/run/docker.sock
[ -S "$socket" ] || exit 72
if [ -x /mnt/host/wsl/docker-desktop/cli-tools/usr/bin/docker ]; then
    client=/mnt/host/wsl/docker-desktop/cli-tools/usr/bin/docker
else
    client=$(command -v docker) || exit 73
fi
printf '%s\000' "$before"
"$client" --host "unix://$socket" "$@"
printf '\000%s' "$(epoch)"
'''


def readonly_command(args):
    if not args:
        return False
    if args[0] in ('info', 'ps', 'inspect'):
        return True
    return len(args) > 1 and args[0] in ('network', 'volume', 'image') and args[1] in ('ls', 'inspect')


def parse_epoch(value):
    pieces = value.strip().split('|')
    if len(pieces) != 3 or not re.fullmatch(r'[a-f0-9-]{36}', pieces[0]) or not all(p.isdecimal() for p in pieces[1:]):
        raise DockerGuardError('Unverifiable live dockerd process identity; investigation required')
    return {'boot_id': pieces[0], 'pid': pieces[1], 'start_ticks': pieces[2]}


class LiveDocker:
    def __init__(self, environment, *, execute=None, platform=None):
        self.environment = environment
        self.execute = execute or subprocess.run
        self.platform = platform or os.name
        self.identity = None

    def read(self, *args):
        if not readonly_command(args):
            raise DockerGuardError('Direct Docker transport permits read-only requests only')
        command = (['wsl.exe', '-d', 'docker-desktop', '-e'] if self.platform == 'nt' else [])
        command += ['sh', '-c', DIRECT_SCRIPT, 'crisisguard-live-reader', *args]
        result = self.execute(command, env=self.environment, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=15)
        if result.returncode:
            # stderr can contain private Docker configuration. Do not publish it.
            raise DockerGuardError('Direct live Docker engine unavailable; no cached inventory fallback')
        parts = result.stdout.split('\0')
        if len(parts) != 3:
            raise DockerGuardError('Missing direct-daemon response proof; investigation required')
        before, after = parse_epoch(parts[0]), parse_epoch(parts[2])
        if before != after or (self.identity and before != self.identity['epoch']):
            raise DockerGuardError('Docker engine restarted during verification; investigation required')
        return parts[1].strip(), before

    def ready(self):
        first, epoch = self.read('info', '--format', '{{json .}}')
        second, again = self.read('info', '--format', '{{json .}}')
        try:
            a, b = json.loads(first), json.loads(second)
            valid = (epoch == again and a['ID'] == b['ID'] and bool(a['ID'])
                     and datetime.fromisoformat(b['SystemTime'].replace('Z', '+00:00')) >
                     datetime.fromisoformat(a['SystemTime'].replace('Z', '+00:00')))
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise DockerGuardError('Live Docker readiness did not stabilize; investigation required')
        observed = {'daemon_id': b['ID'], 'epoch': again, 'transport': 'direct-dockerd-unix-socket'}
        if self.identity and self.identity != observed:
            raise DockerGuardError('Docker engine identity changed; investigation required')
        self.identity = observed
        return observed

    def output(self, *args):
        return self.read(*args)[0]


def network_snapshot(row):
    # A project network named "bridge" is not automatically a built-in network.
    builtin = (row['Name'] == 'bridge' and row['Driver'] == 'bridge'
               and (row.get('Options') or {}).get('com.docker.network.bridge.default_bridge') == 'true'
               and not row.get('Labels')) or (row['Name'] in ('host', 'none')
               and row['Driver'] == {'host': 'host', 'none': 'null'}[row['Name']] and not row.get('Labels'))
    return {'id': row['Id'], 'name': row['Name'], 'created': row.get('Created'),
            'driver': row['Driver'], 'scope': row.get('Scope'), 'internal': row.get('Internal'),
            'kind': 'builtin' if builtin else 'user_defined', 'labels': row.get('Labels') or {},
            'containers': {cid: {k: endpoint.get(k) for k in ('Name', 'EndpointID', 'IPv4Address', 'IPv6Address')}
                           for cid, endpoint in (row.get('Containers') or {}).items()}}


def canonical_container_snapshot(snapshot):
    # Docker does not guarantee Mounts array order. Preserve every recorded
    # mount field and duplicate, but compare their content independently of
    # enumeration order. Return a copy so historical baselines stay untouched.
    return {**snapshot, 'mounts': sorted(snapshot['mounts'],
                                        key=lambda mount: json.dumps(mount, sort_keys=True))}


def capture_inventory(reader):
    engine = reader.ready()
    ids = reader.output('ps', '-aq', '--no-trunc').splitlines()
    rows = json.loads(reader.output('inspect', *ids)) if ids else []
    volume_names = reader.output('volume', 'ls', '-q').splitlines()
    volumes = json.loads(reader.output('volume', 'inspect', *volume_names)) if volume_names else []
    network_ids = reader.output('network', 'ls', '-q', '--no-trunc').splitlines()
    networks = json.loads(reader.output('network', 'inspect', *network_ids)) if network_ids else []
    result = {'schema_version': 2, 'engine': engine,
        'containers': {r['Id']: canonical_container_snapshot({'state': {k: r['State'].get(k) for k in
            ('Running', 'Paused', 'Restarting', 'Pid', 'StartedAt', 'FinishedAt', 'ExitCode')},
            'restarts': r['RestartCount'], 'image': r['Image'],
            'mounts': [{'type': m['Type'], 'name': m.get('Name'), 'destination': m['Destination']} for m in r['Mounts']],
            'networks': {name: {k: value.get(k) for k in ('NetworkID', 'EndpointID', 'IPAddress', 'GlobalIPv6Address')}
                         for name, value in r['NetworkSettings'].get('Networks', {}).items()}})
            for r in rows},
        'volumes': {r['Name']: {k: r.get(k) for k in ('Name', 'CreatedAt', 'Driver', 'Mountpoint', 'Labels', 'Options', 'Scope')}
                    for r in volumes},
        'networks': {r['Id']: network_snapshot(r) for r in networks}}
    if reader.ready() != engine:
        raise DockerGuardError('Docker engine changed while inventory was captured; investigation required')
    return result


def validate_baseline(baseline):
    if (not isinstance(baseline, dict) or baseline.get('schema_version') != 2
            or not baseline.get('engine', {}).get('daemon_id')
            or baseline.get('engine', {}).get('transport') != 'direct-dockerd-unix-socket'
            or set(baseline.get('engine', {}).get('epoch', {})) != {'boot_id', 'pid', 'start_ticks'}
            or not all(isinstance(baseline.get(k), dict) for k in ('containers', 'volumes', 'networks'))
            or any(not isinstance(v, dict) or not {'id', 'name', 'kind', 'created', 'driver', 'labels', 'containers'} <= v.keys()
                   or v['id'] != nid or v['kind'] not in ('builtin', 'user_defined')
                   for nid, v in baseline['networks'].items())
            or any(not {'state', 'restarts', 'image', 'mounts', 'networks'} <= v.keys() for v in baseline['containers'].values())):
        raise DockerGuardError('Legacy/incomplete Docker baseline cannot authorize resume or cleanup; investigation required; original evidence retained')


def stable_baseline(reader):
    # Readiness precedes every resource read. Never turn an unsettled snapshot
    # into a new baseline by retrying until an unexpected change disappears.
    reader.ready()
    first, second = capture_inventory(reader), capture_inventory(reader)
    if first != second:
        raise DockerGuardError('Docker baseline changed during capture; investigation required')
    validate_baseline(first)
    return first


def preservation_changes(baseline, current):
    validate_baseline(baseline)
    validate_baseline(current)
    changes = {'engine_changed': baseline['engine'] != current['engine'],
        'changed_original_container_ids': [cid for cid, value in baseline['containers'].items()
                                            if cid not in current['containers'] or
                                            canonical_container_snapshot(current['containers'][cid]) !=
                                            canonical_container_snapshot(value)],
        'changed_original_volume_names': [name for name, value in baseline['volumes'].items()
                                           if current['volumes'].get(name) != value],
        'changed_builtin_network_ids': [], 'changed_user_network_ids': []}
    for nid, before in baseline['networks'].items():
        if current['networks'].get(nid) != before:
            key = 'changed_builtin_network_ids' if before['kind'] == 'builtin' else 'changed_user_network_ids'
            changes[key].append(nid)
    return changes
