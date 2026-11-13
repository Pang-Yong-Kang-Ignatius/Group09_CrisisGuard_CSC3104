"""Read-only health and optional record checks for any CrisisGuard deployment.

Run on each Docker/EC2 host. Host Python standard library only; no AWS credentials.
Does not deploy, capture, replay, restart or write database records.
See docs/runbooks/AWS_HEALTH_CHECKS.md.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("patient", "emergency", "admin")
EVENT_TYPES = ("patient_registration", "encounter_creation", "emergency_record", "patient_discharge")
EVENT_FIELDS = ("event_id", "site_id", "event_type", "base_priority", "prerequisite_ids", "payload")

# Container probes return whitelisted settings only. Never return DATABASE_URL,
# credentials, raw HTTP exceptions or recovery last_error text.
PROBE = r'''
import json, os
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
def read(url):
    with urlopen(url, timeout=config['timeout']) as response:
        if response.status != 200:
            raise ValueError('Unexpected HTTP status')
        return json.load(response)
hospital = config['hospital']
if hospital:
    from edge.capture_api.cloud_client import get_service_urls
    urls = get_service_urls()
else:
    urls = {name: 'http://' + (config['cloud_host'] + ':' + str(port) if config['cloud_host']
            else name + ':8000') + '/events'
            for name, port in zip(('patient','emergency','admin'), config['ports'])}
result = {'cloud': {}, 'endpoint_source': 'hospital_runtime_configuration' if hospital else
          'explicit_cloud_host' if config['cloud_host'] else 'cloud_service_dns'}
for name, port in zip(('patient','emergency','admin'), config['ports']):
    item = {'healthy': False}
    try:
        parsed = urlsplit(urls[name])
        if (parsed.scheme not in ('http','https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path.rstrip('/') != '/events'):
            raise ValueError('Invalid endpoint')
        actual_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if hospital and config['cloud_host'] and (parsed.hostname != config['cloud_host'] or actual_port != port):
            raise ValueError('Configured destination mismatch')
        health_url = urlunsplit((parsed.scheme, parsed.netloc, '/health', '', ''))
        item['health_url'] = health_url
        body = read(health_url)
        item['healthy'] = body.get('status') == 'healthy' and body.get('service') == name
    except Exception:
        item['error'] = 'Endpoint invalid, unreachable, mismatched or unhealthy; inspect private configuration'
    result['cloud'][name] = item
if hospital:
    result['site_id'] = os.getenv('SITE_ID', '')
    result['auto_replay_configured'] = os.getenv('AUTO_REPLAY') == '1'
    for key, path in (('capture','/'), ('records','/events'), ('recovery','/recovery/status')):
        try:
            body = read('http://127.0.0.1:8000' + path)
            if key == 'recovery':
                error_present = bool(body.get('last_error'))
                body = {field: body.get(field) for field in ('enabled','cloud_reachable','service_health',
                        'last_check_at','last_disconnected_at','last_reconnected_at','drain_completed_at','replayable_backlog')}
                body['last_error_present'] = error_present
            result[key] = body
        except Exception:
            result[key] = None
print(json.dumps(result))
'''

DATABASE = r'''
import json
from cloud.database.db import get_connection
with get_connection() as conn:
    conn.read_only = True
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '5000ms'")
        cur.execute("SELECT count(*),count(distinct event_id),count(*) FILTER (WHERE status='applied') FROM events")
        total, distinct, applied = cur.fetchone()
        records = []
        if config['event_ids']:
            cur.execute("SELECT event_id,site_id,event_type,base_priority,prerequisite_ids,payload,status FROM events WHERE event_id = ANY(%s)", (config['event_ids'],))
            names = [column.name for column in cur.description]
            records = [dict(zip(names, row)) for row in cur.fetchall()]
print(json.dumps({'total':total,'distinct':distinct,'applied':applied,'records':records}))
'''


def command(args, *, stdin=None):
    result = subprocess.run(args, cwd=ROOT, input=stdin, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"Docker command failed (exit {result.returncode}); inspect Compose/container state")
    return result.stdout.strip()


def container_json(compose, service, program, config):
    # Embed serialized data as a Python string, never executable code or SQL.
    script = 'import json\nconfig = json.loads(' + repr(json.dumps(config)) + ')\n' + program
    return json.loads(command(compose + ['exec', '-T', service, 'python', '-'], stdin=script))


def parse_containers(raw):
    if not raw.strip():
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        return [json.loads(line) for line in raw.splitlines() if line.strip()]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_containers(containers, services):
    summary = [{key: item.get(key, '') for key in ('Service', 'State', 'Health')} for item in containers]
    for service in services:
        matches = [item for item in summary if item['Service'] == service]
        require(len(matches) == 1 and matches[0]['State'] == 'running' and matches[0]['Health'] == 'healthy',
                f'Expected one running, healthy container for {service}')
    return summary


def validate_expected(expected, *, full=False):
    if not isinstance(expected, list) or not expected:
        raise ValueError('Expected-event manifest must be a nonempty JSON array')
    seen = set()
    for event in expected:
        if not isinstance(event, dict) or any(key not in event for key in (EVENT_FIELDS if full else EVENT_FIELDS[:3])):
            raise ValueError('Expected event is missing required fields')
        if any(not isinstance(event[key], str) or not event[key].strip() for key in EVENT_FIELDS[:3]):
            raise ValueError('Event ID, site ID and type must be nonempty strings')
        if event['event_id'] in seen:
            raise ValueError('Duplicate expected event ID')
        seen.add(event['event_id'])
        if event['event_type'] not in EVENT_TYPES or event.get('status', 'applied') != 'applied':
            raise ValueError('Expected events require a supported type and applied status')
        if full:
            if (type(event['base_priority']) is not int or not 1 <= event['base_priority'] <= 5
                    or not isinstance(event['payload'], dict) or not isinstance(event['prerequisite_ids'], list)
                    or any(not isinstance(v, str) or not v.strip() for v in event['prerequisite_ids'])
                    or set(event) - set(EVENT_FIELDS) - {'status'}):
                raise ValueError('Use original capture bodies with valid priority, prerequisites and payload')
    return expected


def check_events(records, expected):
    verified = []
    for event in expected:
        matches = [row for row in records if row.get('event_id') == event['event_id']]
        require(len(matches) == 1, 'An expected event is missing or duplicated')
        # Canonical JSON keeps key ordering irrelevant but distinguishes false/0
        # and true/1, which ordinary Python dictionary equality conflates.
        require(all(json.dumps(matches[0].get(k), sort_keys=True, allow_nan=False)
                    == json.dumps(v, sort_keys=True, allow_nan=False)
                    for k, v in dict(event, status='applied').items()),
                'An expected event has different content, site, type or status')
        verified.append({key: matches[0][key] for key in ('event_id', 'site_id', 'event_type', 'status')})
    return verified


def check_recovery(probe, max_age):
    state = probe.get('recovery')
    require(probe.get('auto_replay_configured') is True and isinstance(state, dict)
            and state.get('enabled') is True, 'Automatic replay must be configured and running')
    require(state.get('cloud_reachable') is True and state.get('service_health') == dict.fromkeys(SERVICES, True)
            and state.get('last_error_present') is False,
            'Automatic replay must report all three services healthy without errors')
    try:
        timestamp = datetime.fromisoformat(state.get('last_check_at') or '')
    except (ValueError, TypeError):
        raise RuntimeError('Automatic replay timestamp is unavailable or invalid') from None
    require(timestamp.tzinfo is not None and -5 <= (datetime.now(timezone.utc) - timestamp).total_seconds() <= max_age,
            'Automatic replay status is stale or has an invalid timestamp')
    require(type(state.get('replayable_backlog')) is int and state['replayable_backlog'] >= 0,
            'Automatic replay backlog is unavailable or invalid')
    return state


def checked(report, name, operation):
    try:
        result = operation()
        report['checks'].append({'name': name, 'result': 'passed'})
        return result
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        # Only our own fixed RuntimeError messages may be serialized.
        message = str(error) if type(error) is RuntimeError else f'{type(error).__name__}; inspect host/configuration'
        report['checks'].append({'name': name, 'result': 'failed', 'error': message})
        return None


def verify(args, expected, report):
    hospital = args.role != 'cloud'
    role = 'hospital' if hospital else 'cloud'
    env_file = args.env_file or ROOT / f'infrastructure/aws/{role}.env'
    files = args.compose_file or [ROOT / f'infrastructure/docker/compose.{role}.yaml',
                                  ROOT / f'infrastructure/aws/compose.{role}.override.yaml']
    require(env_file.is_file() and all(path.is_file() for path in files), 'Selected env/Compose file does not exist')
    compose = ['docker', 'compose', '--env-file', str(env_file.resolve())]
    if args.project_name:
        compose += ['--project-name', args.project_name]
    for path in files:
        compose += ['-f', str(path.resolve())]
    service = args.hospital_service if hospital else 'patient'
    services = [service] if hospital else ['postgres', *SERVICES]
    report['containers'] = checked(report, 'containers', lambda: check_containers(
        parse_containers(command(compose + ['ps', '--all', '--format', 'json'])), services))
    probe = checked(report, 'container_probe', lambda: container_json(compose, service, PROBE, {
        'hospital': hospital, 'cloud_host': args.cloud_host,
        'ports': [args.patient_port, args.emergency_port, args.admin_port], 'timeout': args.timeout}))
    if probe is None:
        return
    report['endpoint_source'] = probe['endpoint_source']
    report['cloud_health'] = probe['cloud']
    for name in SERVICES:
        checked(report, 'cloud_' + name, lambda name=name: require(probe['cloud'][name].get('healthy') is True,
                f'{name} health probe failed from the application container'))
    records = None
    if hospital:
        site = probe.get('site_id')
        report['site_id'] = site
        checked(report, 'site_identity', lambda: require(isinstance(site, str) and bool(site.strip())
            and (not args.site_id or args.site_id == site) and isinstance(probe.get('capture'), dict)
            and probe['capture'].get('site_id') == site, 'Hospital SITE_ID is missing or differs from expected/local API identity'))
        records = probe.get('records')
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            checked(report, 'hospital_events', lambda: require(False, 'Hospital events API unavailable'))
            return
        checked(report, 'hospital_events', lambda: require(all(row.get('site_id') == site
            and row.get('status') in ('pending', 'applied') for row in records),
            'Hospital contains unexpected site identities or statuses'))
        report['queue'] = {'total': len(records), 'pending': sum(r.get('status') == 'pending' for r in records),
                           'applied': sum(r.get('status') == 'applied' for r in records)}
        report['auto_replay_configured'] = probe.get('auto_replay_configured')
        report['recovery'] = probe.get('recovery')
        if args.allow_manual_replay:
            report['checks'].append({'name': 'automatic_replay', 'result': 'skipped', 'reason': 'Explicit --allow-manual-replay'})
        else:
            checked(report, 'automatic_replay', lambda: check_recovery(probe, args.max_status_age))
        if args.require_drained:
            checked(report, 'queue_drained', lambda: require(report['queue']['pending'] == 0
                and (args.allow_manual_replay or (probe.get('recovery') or {}).get('replayable_backlog') == 0),
                'Hospital queue is not drained'))
        if expected:
            own = [event for event in expected if event['site_id'] == site]
            report['expected_events_for_site'] = len(own)
            if not own:
                checked(report, 'expected_events', lambda: require(False, 'No expected events match this hospital SITE_ID'))
                return
            expected = own
    else:
        database = checked(report, 'database_read', lambda: container_json(compose, 'patient', DATABASE,
                                        {'event_ids': [event['event_id'] for event in expected]}))
        if database is not None:
            report['database'] = {key: database[key] for key in ('total', 'distinct', 'applied')}
            checked(report, 'database_counts', lambda: require(all(type(database[k]) is int and database[k] >= 0
                                                             for k in ('total', 'distinct', 'applied'))
                                                             and database['total'] == database['distinct'] == database['applied'],
                                                             'Cloud ledger has inconsistent counts/statuses'))
            records = database['records']
    if expected and records is not None:
        report['verified_events'] = checked(report, 'expected_events', lambda: check_events(records, expected))
    elif not expected:
        report['checks'].append({'name': 'expected_events', 'result': 'skipped',
                                'reason': 'No expectations supplied; not event delivery/persistence proof'})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=['cloud', 'hospital', 'hospital-a', 'hospital-b'])
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--compose-file', type=Path, action='append', help='Repeat in deployment merge order')
    parser.add_argument('--project-name')
    parser.add_argument('--hospital-service', default='hospital')
    parser.add_argument('--cloud-host', help='Expected hospital cloud host, or cloud probe destination; no scheme')
    for name, port in zip(SERVICES, (8001, 8003, 8004)):
        parser.add_argument('--' + name + '-port', type=int, default=port)
    parser.add_argument('--site-id', help='Optional expected hospital identity')
    parser.add_argument('--expect-event', nargs=3, action='append', metavar=('EVENT_ID', 'SITE_ID', 'EVENT_TYPE'),
                        help='Metadata-only applied record check; repeat for more records')
    parser.add_argument('--expect-events', type=Path, help='JSON array of complete original capture bodies')
    parser.add_argument('--allow-manual-replay', action='store_true', help='Explicitly skip automatic replay requirement')
    parser.add_argument('--require-drained', action='store_true')
    parser.add_argument('--max-status-age', type=int, default=30)
    parser.add_argument('--timeout', type=int, choices=range(1, 6), default=3, help='Per-request seconds (1-5)')
    parser.add_argument('--output', type=Path, help='New private JSON report; refuses to overwrite')
    args = parser.parse_args(argv)
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'hostname': socket.gethostname(),
              'role': args.role, 'scope': 'Read-only application-container health and optional expected records', 'checks': []}
    output = None
    try:
        if args.output:
            output = os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8')
        require(not (args.expect_event and args.expect_events), 'Choose --expect-event or --expect-events, not both')
        require(args.max_status_age > 0 and all(1 <= port <= 65535 for port in
                (args.patient_port, args.emergency_port, args.admin_port)), 'Ports/status age must be positive and valid')
        require(args.cloud_host or (args.patient_port, args.emergency_port, args.admin_port) == (8001, 8003, 8004),
                'Port overrides require --cloud-host; otherwise running URLs/service DNS are used')
        require(args.role != 'cloud' or not (args.site_id or args.require_drained or args.allow_manual_replay
                or args.hospital_service != 'hospital'), 'Hospital identity/queue/replay options require a hospital role')
        for value in (args.hospital_service, args.project_name):
            require(not value or re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', value), 'Invalid Compose service/project name')
        require(not args.cloud_host or re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', args.cloud_host),
                'Cloud host must be IPv4/DNS without URL or credentials')
        if args.cloud_host:
            args.cloud_host = args.cloud_host.lower()
        expected = (validate_expected(json.loads(args.expect_events.read_text(encoding='utf-8')), full=True)
                    if args.expect_events else validate_expected([dict(zip(EVENT_FIELDS[:3], item)) for item in args.expect_event])
                    if args.expect_event else [])
        report['record_verification'] = 'full_record' if args.expect_events else 'metadata_only' if expected else 'not_requested'
        verify(args, expected, report)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        report['checks'].append({'name': 'configuration_or_probe', 'result': 'failed',
                                'error': str(error) if type(error) is RuntimeError else f'{type(error).__name__}; inspect selected files/configuration'})
    report['result'] = 'failed' if any(check['result'] == 'failed' for check in report['checks']) else 'passed'
    encoded = json.dumps(report, indent=2) + '\n'
    print(encoded, end='')
    if output:
        with output:
            output.write(encoded)
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
