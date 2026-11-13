"""Bounded real-system scenarios; all mutations use the owned Stack adapter.

No experiments, unit doubles or production algorithm changes. SQL time/journal
fixtures are explicitly labeled; network loss uses the existing fault proxy.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import math
import time
from uuid import uuid4

from cloud.metaguard import event_digest
from infrastructure.scripts.project_e2e import require
from infrastructure.scripts.smoke_deployment import event_chain, check_records, check_application_order

HOSPITALS = ('hospital-a', 'hospital-b')
SITES = ('Hospital_A', 'Hospital_B')
ROUTES = {'patient_registration': 'patient', 'encounter_creation': 'patient',
          'emergency_record': 'emergency', 'patient_discharge': 'admin'}


def event(stack, label, site='Hospital_A', priority=2, prerequisites=(), kind='patient_registration'):
    return {'event_id': stack.project + '-' + label, 'site_id': site, 'event_type': kind,
            'base_priority': priority, 'prerequisite_ids': list(prerequisites),
            'payload': {'patient_id': 'SYNTHETIC-' + stack.nonce[:8]}}


def capture(stack, hospital, item):
    response = stack.http(hospital, 'POST', '/capture', item, expected=201)
    require(response['event_id'] == item['event_id'] and response['site_id'] == item['site_id']
            and response['status'] == 'pending', 'Capture acknowledgement mismatch')
    rows = stack.sqlite(hospital, 'SELECT event_id,site_id,status FROM events WHERE event_id=?', [item['event_id']])
    permitted = ('pending', 'applied') if stack.config['services'][hospital]['environment'].get('AUTO_REPLAY') == '1' else ('pending',)
    require(len(rows) == 1 and rows[0][:2] == [item['event_id'], item['site_id']] and rows[0][2] in permitted,
            'Capture not durable in correct SQLite')
    stack.report['events'].append({'event_id': item['event_id'], 'site_id': item['site_id'],
                                   'event_type': item['event_type'], 'capture': 201, 'sqlite_after_capture': 'pending'})


def replay(stack, hospital):
    return stack.http(hospital, 'POST', '/replay', headers={'Authorization': 'Bearer ' + stack.settings['MANUAL_REPLAY_TOKEN']}, timeout=60)


@contextmanager
def proxy_fault(stack, controls):
    stack.fault_gate()
    stack.active_faults.append({'kind': 'test proxy', 'controls': controls})
    stack.save()
    try:
        stack.http('fault-proxy', 'POST', '/__faults', controls)
        yield
    finally:
        stack.fault_gate()
        stack.http('fault-proxy', 'POST', '/__faults', {key: None for key in controls})
        stack.active_faults.pop()
        stack.save()


def drain(stack, hospital, items, *, automatic=False, timeout=100):
    wanted = {e['event_id'] for e in items}
    def completed():
        if not automatic:
            replay(stack, hospital)
        rows = stack.http(hospital, 'GET', '/events')
        return wanted <= {r['event_id'] for r in rows if r['status'] == 'applied'}
    stack.until(completed, timeout=timeout, description=hospital + ' applied IDs')
    return lifecycle(stack, items)


def lifecycle(stack, items):
    ids = [item['event_id'] for item in items]
    rows = stack.query('''SELECT event_id,site_id,event_type,base_priority,prerequisite_ids,payload,status,applied_at
        FROM events WHERE event_id=ANY(%s)''', (ids,))
    require(len(rows) == len(ids) and {r[0] for r in rows} == set(ids), 'Authoritative event count/IDs mismatch')
    stored = {r[0]: r for r in rows}
    stack.until(lambda: stack.query("SELECT count(*) FROM controller_grants WHERE event_id=ANY(%s) AND status='completed'",
                                    (ids,)) == [(len(ids),)], description='authoritative grant completion')
    grant_rows = stack.query('SELECT event_id,grant_id::text,status FROM controller_grants WHERE event_id=ANY(%s) ORDER BY updated_at', (ids,))
    audit_rows = stack.query('''SELECT g.event_id,a.grant_id::text,a.new_status FROM controller_grant_audit a
        JOIN controller_grants g USING(grant_id) WHERE g.event_id=ANY(%s) ORDER BY a.id''', (ids,))
    result = []
    for item in items:
        row = stored[item['event_id']]
        require(tuple(row[:7]) == tuple(item[k] for k in
                ('event_id', 'site_id', 'event_type', 'base_priority', 'prerequisite_ids', 'payload')) + ('applied',),
                'Authoritative event count/content mismatch for ' + item['event_id'])
        grants = [(gid, status) for eid, gid, status in grant_rows if eid == item['event_id']]
        complete = [gid for gid, status in grants if status == 'completed']
        require(len(complete) == 1, 'Expected exactly one completed grant per event')
        audit = [status for eid, gid, status in audit_rows if eid == item['event_id'] and gid == complete[0]]
        require(audit.count('acknowledged') == 1 and audit[-1] == 'completed', 'Grant ACK/completion audit mismatch')
        result.append({'event_id': item['event_id'], 'site_id': item['site_id'], 'event_type': item['event_type'],
                       'cloud_service': ROUTES[item['event_type']], 'postgres_count': 1, 'status': 'applied',
                       'applied_at': row[-1].isoformat(), 'grants': [list(g) for g in grants],
                       'grant_audit': audit})
        response = stack.http(ROUTES[item['event_type']], 'GET', '/events/' + item['event_id'])
        require(response['event_id'] == item['event_id'] and response['status'] == 'applied', 'Owning Cloud API lookup mismatch')
    for item in items:
        own = next(r for r in result if r['event_id'] == item['event_id'])
        for parent in item['prerequisite_ids']:
            earlier = stored[parent][-1] if parent in stored else stack.query('SELECT applied_at FROM events WHERE event_id=%s', (parent,))[0][0]
            require(earlier <= datetime.fromisoformat(own['applied_at']), 'Prerequisite order violated')
    stack.report.setdefault('lifecycles', []).extend(result)
    return result


def smoke(stack):
    chains = [event_chain(stack.project, 'smoke', site) for site in SITES]
    expected = [e for chain in chains for e in chain]
    # A failed harness/preflight can be resumed without capturing duplicate IDs or
    # replacing retained evidence. Reuse only this run's exact synthetic chain.
    existing_cloud = {r[0] for r in stack.query('SELECT event_id FROM events')}
    require(existing_cloud <= {e['event_id'] for e in expected}, 'Unexpected events exist before smoke')
    for hospital, chain in zip(HOSPITALS, chains):
        existing = {row['event_id']: row for row in stack.http(hospital, 'GET', '/events')}
        require(set(existing) <= {e['event_id'] for e in chain}, 'Unexpected hospital records before smoke')
        for item in reversed(chain):
            if item['event_id'] not in existing:
                capture(stack, hospital, item)
            else:
                require(all(existing[item['event_id']].get(k) == v for k, v in item.items()), 'Resumed smoke record differs from submitted event')
        if not existing_cloud:
            check_records(stack.http(hospital, 'GET', '/events'), chain, 'pending')
            preview = stack.http(hospital, 'GET', '/scheduler/inspect')
            require(preview['next_event_id'] == chain[0]['event_id'], 'Inspector must select the prerequisite root')
        other = HOSPITALS[1 - HOSPITALS.index(hospital)]
        ids = {r[0] for r in stack.sqlite(other, 'SELECT event_id FROM events')}
        require(not ids.intersection(e['event_id'] for e in chain), 'Hospital SQLite identity isolation failed')
    if not existing_cloud:
        require(stack.query('SELECT count(*) FROM events') == [(0,)], 'Manual-only queues replayed unexpectedly')
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda pair: drain(stack, *pair), zip(HOSPITALS, chains)))
    history = stack.http('dashboard', 'GET', '/api/history?limit=50')
    check_records(history['events'], expected, 'applied')
    require(history['count'] == len(expected), 'Dashboard history count mismatch')
    # Replay again through the public protected path, not direct SQL writes.
    for hospital in HOSPITALS:
        replay(stack, hospital)
    require(stack.query('SELECT count(*) FROM events') == [(8,)], 'Duplicate replay changed ledger cardinality')
    for service, minimum in (('patient', 4), ('emergency', 2), ('admin', 2)):
        metrics = stack.http(service, 'GET', '/metrics')
        require(type(metrics['replay_latency_sample_count']) is int and metrics['database_healthy'] is True,
                'Owning service telemetry/database health unavailable')
        # Docker exec overhead can outlast the real 60s window. Do not require
        # historical replay samples to survive until the end of a slow audit.
        stack.report.setdefault('smoke_telemetry', {})[service] = {
            'replay_samples_in_current_60s_window': metrics['replay_latency_sample_count'],
            'minimum_expected_replay_posts_from_event_types': minimum,
            'limitation': 'Window count is not a durable per-event request receipt'}
    stack.state['smoke_ids'] = [e['event_id'] for e in expected]
    component_checks = [
        ('Hospital A: capture, SQLite persistence and applied ACK', '4 exact synthetic IDs and content; own SQLite volume'),
        ('Hospital B: capture, SQLite persistence and applied ACK', '4 exact synthetic IDs and content; own SQLite volume'),
        ('Scheduler: dependency-chain replay', 'Prerequisite eligibility and authoritative application order; Inspector is a separate prediction'),
        ('MetaGuard: registration/grant/ACK/completion pipeline', '8 completed grant rows; acknowledgement/completion audit transitions'),
        ('Cloud Patient/Emergency/Admin: protected application', 'All 4 schema-supported event types; exact PostgreSQL records and owning-service lookups'),
        ('Dashboard: applied history matches PostgreSQL', 'All 8 exact synthetic IDs/content returned by actual Dashboard API'),
        ('Duplicate manual replay: no extra application', 'A second protected replay retained exactly 8 Cloud rows'),
    ]
    for name, detail in component_checks:
        stack.record(name, 'PASS', detail)
    stack.save()
    return {'detail': '8 synthetic events, all 4 types, distinct SQLite stores; concurrent protected replay; SQL/grant audits and Dashboard exact IDs',
            'event_ids': stack.state['smoke_ids']}


def authorized_grant(stack, item):
    site = item['site_id']
    stack.http('controller', 'POST', '/hospitals/' + site + '/candidate',
               {'event_id': item['event_id'], 'priority': item['base_priority'], 'event_digest': event_digest(item)},
               headers=stack.hospital_auth(site))
    chosen = {}
    def allocate():
        selected = stack.api('POST', '/grants/next')
        if selected.get('event_id') == item['event_id']:
            chosen.update(selected)
            return True
        return False
    stack.until(allocate, timeout=30, description='grant allocation')
    gid = chosen['grant_id']
    stack.until(lambda: stack.http('controller', 'POST', f'/grants/{gid}/acknowledge', {'hospital_id': site},
                headers=stack.hospital_auth(site), expected=(200, 429)).get('status') == 'acknowledged', timeout=30,
                description='grant acknowledgement')
    return gid


def cloud_contracts(stack):
    outcomes = []
    for service, kind in (('patient', 'patient_registration'), ('emergency', 'emergency_record'), ('admin', 'patient_discharge')):
        item = event(stack, 'contract-' + service, kind=kind)
        before = stack.query('SELECT count(*) FROM events')[0][0]
        for headers, status in (({}, 401), ({'Authorization': 'Bearer invalid'}, 401),
                    (stack.hospital_auth('Hospital_B'), 403), (stack.hospital_auth('Hospital_A'), 403)):
            stack.http(service, 'POST', '/events', item, headers=headers, expected=status)
        stack.http(service, 'POST', '/events', {**item, 'base_priority': 0}, expected=422)
        require(stack.query('SELECT count(*) FROM events') == [(before,)], 'Denied request wrote data')
        missing = {**item, 'event_id': item['event_id'] + '-missing', 'prerequisite_ids': [stack.project + '-never-captured']}
        gid = authorized_grant(stack, missing)
        headers = {**stack.hospital_auth('Hospital_A'), 'X-MetaGuard-Grant': gid}
        stack.http(service, 'POST', '/events', missing, headers=headers, expected=409)
        stack.http('controller', 'POST', f'/grants/{gid}/abandon', {'hospital_id': 'Hospital_A'}, headers=stack.hospital_auth('Hospital_A'))
        gid = authorized_grant(stack, item)
        headers['X-MetaGuard-Grant'] = gid
        stack.http(service, 'POST', '/events', item, headers=headers, expected=201)
        stack.http(service, 'POST', '/events', item, headers=headers, expected=200)
        stack.http(service, 'POST', '/events', {**item, 'payload': {'patient_id': 'CONFLICT-SYNTHETIC'}}, headers=headers, expected=409)
        stack.http('controller', 'POST', f'/grants/{gid}/complete', {'hospital_id': 'Hospital_A'}, headers=stack.hospital_auth('Hospital_A'))
        stack.http(service, 'POST', '/events', item, headers=headers, expected=409)  # Completed grant is fenced.
        require(stack.query('SELECT count(*) FROM events WHERE event_id=%s', (item['event_id'],)) == [(1,)], 'Duplicate or conflict caused extra application')
        outcomes.append({'service': service, 'event_id': item['event_id'], 'success': 201, 'identical_retry': 200,
                         'invalid': 422, 'missing_auth': 401, 'wrong_hospital': 403, 'missing_prerequisite': 409,
                         'conflict': 409, 'completed_grant_fenced': 409, 'postgres_count': 1})
    for hospital in HOSPITALS:
        stack.http(hospital, 'POST', '/replay', expected=403)
        stack.http(hospital, 'POST', '/replay', headers={'Authorization': 'Bearer invalid'}, expected=403)
    wrong = event(stack, 'wrong-site-capture', 'Hospital_B')
    stack.http('hospital-a', 'POST', '/capture', wrong, expected=403)
    stack.http('hospital-a', 'POST', '/capture', {**wrong, 'site_id': 'Hospital_A', 'base_priority': 0}, expected=422)
    require(stack.query('SELECT count(*) FROM events WHERE event_id=%s', (wrong['event_id'],)) == [(0,)], 'Invalid hospital capture reached Cloud')
    duplicate = event(stack, 'unused-duplicate')
    duplicate['event_id'] = stack.state['smoke_ids'][0]
    stack.http('hospital-a', 'POST', '/capture', duplicate, expected=409)
    stack.http('controller', 'GET', '/tickets', expected=401)
    stack.http('controller', 'GET', '/tickets', headers={'Authorization': 'Bearer invalid'}, expected=401)
    b_id = stack.query("SELECT grant_id::text FROM controller_grants WHERE hospital_id='Hospital_B' LIMIT 1")[0][0]
    stack.http('controller', 'GET', '/grants/by-id/' + b_id, headers=stack.hospital_auth('Hospital_A'), expected=403)
    return {'detail': 'Actual auth/validation/conflict/dependency/duplicate/fencing HTTP responses; SQL unchanged after denials', 'results': outcomes}


def scheduler(stack):
    evidence = []
    for index, active in enumerate((True, False)):
        hospital, site = HOSPITALS[index], SITES[index]
        if not active:
            stack.configure({hospital: {'SCHEDULER_ENABLE_INHERITANCE': '0'}})
        a = event(stack, 'DEMO_A-' + site, site, 1)
        b = event(stack, 'DEMO_B-' + site, site, 5, [a['event_id']])
        c = event(stack, 'DEMO_C-' + site, site, 3)
        for item in (c, a, b):
            capture(stack, hospital, item)
        on = stack.http(hospital, 'GET', '/scheduler/inspect?inheritance=true')
        off = stack.http(hospital, 'GET', '/scheduler/inspect?inheritance=false')
        require(on['next_event_id'] == a['event_id'] and off['next_event_id'] == c['event_id'], 'DEMO Inspector ON/OFF prediction differs')
        results = drain(stack, hospital, [a, b, c])
        order = [r[0] for r in stack.query('''SELECT g.event_id FROM controller_grant_audit a JOIN controller_grants g USING(grant_id)
            WHERE a.action='allocate' AND g.event_id=ANY(%s) ORDER BY a.id''', ([a['event_id'], b['event_id'], c['event_id']],))]
        expected = [a['event_id'], b['event_id'], c['event_id']] if active else [c['event_id'], a['event_id'], b['event_id']]
        require(order == expected, 'Actual grant/replay order differs from active inheritance policy')
        evidence.append({'inheritance': active, 'inspector_on': on['next_event_id'], 'inspector_off': off['next_event_id'], 'actual_grant_order': order})
    stack.configure({'hospital-b': {'SCHEDULER_ENABLE_INHERITANCE': '1'}})
    # SQL time fixtures exercise real Inspector/replay with controlled timestamps.
    tie1, tie2 = [event(stack, 'tie-' + n) for n in ('a', 'b')]
    for item in (tie2, tie1):
        capture(stack, 'hospital-a', item)
    stamp = datetime.now(timezone.utc).isoformat()
    for item in (tie1, tie2):
        stack.sqlite('hospital-a', 'UPDATE events SET created_at=? WHERE event_id=?', [stamp, item['event_id']], fixture=True)
    require(stack.http('hospital-a', 'GET', '/scheduler/inspect')['next_event_id'] == tie1['event_id'], 'Equal timestamps not tied by ID')
    old = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
    stack.sqlite('hospital-a', 'UPDATE events SET created_at=? WHERE event_id=?', [old, tie2['event_id']], fixture=True)
    preview = stack.http('hospital-a', 'GET', '/scheduler/inspect')
    require(preview['next_event_id'] == tie2['event_id'], 'Ageing did not promote older event')
    drain(stack, 'hospital-a', [tie1, tie2])
    return {'detail': 'Inspector comparison and actual grant order separately verified ON/OFF; tie/ageing use labeled SQLite timestamp fixtures', 'demonstrations': evidence}


def policies(stack):
    evidence = []
    variants = [('fifo', 1, 'Hospital_A'), ('priority', 1, 'Hospital_B'),
                ('bounded', 0, 'Hospital_A'), ('bounded', 1, 'Hospital_B'), ('drr', 1, 'Hospital_A')]
    for policy, limit, expected in variants:
        stack.configure({'controller': {'CRISISGUARD_EXPERIMENTAL_POLICIES': '1', 'CONTROLLER_SCHEDULING_POLICY': policy,
                                       'CONTROLLER_BYPASS_LIMIT': str(limit)}})
        chosen_items = [event(stack, f'policy-{policy}-{limit}-{site}', site, 1 if site == 'Hospital_A' else 5) for site in SITES]
        for item in chosen_items:
            stack.http('controller', 'POST', '/hospitals/' + item['site_id'] + '/candidate',
                {'event_id': item['event_id'], 'priority': item['base_priority'], 'event_digest': event_digest(item)},
                headers=stack.hospital_auth(item['site_id']))
        actual = stack.api('POST', '/grants/next')
        require(actual['hospital_id'] == expected, 'Durable scheduling policy chose unexpected hospital')
        gid, owner = actual['grant_id'], actual['hospital_id']
        if policy == 'bounded' and limit == 1:
            # Requeue urgent B while A is continuously waiting: bounded bypass
            # must choose A next, even though a fresh B is urgent again.
            stack.http('controller', 'POST', f'/grants/{gid}/cancel', {'hospital_id': owner}, headers=stack.hospital_auth(owner))
            newer = event(stack, 'bounded-requeue-B', 'Hospital_B', 5)
            stack.http('controller', 'POST', '/hospitals/Hospital_B/candidate',
                       {'event_id': newer['event_id'], 'priority': 5, 'event_digest': event_digest(newer)}, headers=stack.hospital_auth('Hospital_B'))
            second = stack.api('POST', '/grants/next')
            require(second['hospital_id'] == 'Hospital_A', 'Bounded priority starved the continuously waiting head')
            stack.http('controller', 'POST', f"/grants/{second['grant_id']}/cancel", {'hospital_id': 'Hospital_A'}, headers=stack.hospital_auth('Hospital_A'))
        else:
            stack.http('controller', 'POST', f'/grants/{gid}/cancel', {'hospital_id': owner}, headers=stack.hospital_auth(owner))
        for site in SITES:
            stack.http('controller', 'POST', '/hospitals/' + site + '/candidate', {}, headers=stack.hospital_auth(site))
        stack.api('POST', '/grants/reconcile-expired')
        require(not any(ticket['event_id'].startswith(stack.project + '-policy-')
                        for ticket in stack.api('GET', '/tickets')),
                'Withdrawn policy fixture was requeued by reconciliation')
        evidence.append({'policy': policy, 'bypass_limit': limit, 'selected': expected})
    stack.configure({'controller': {'CONTROLLER_SCHEDULING_POLICY': 'bounded', 'CONTROLLER_BYPASS_LIMIT': '1'}})
    return {'detail': 'Real HTTP candidate registration + PostgreSQL allocation; RR uses bounded B=0; finite bounded-override example, not a fairness experiment', 'selections': evidence}


def partitions(stack):
    stack.configure({hospital: {'AUTO_REPLAY': '1'} for hospital in HOSPITALS})
    evidence = []
    for offline in (('hospital-a',), ('hospital-b',), HOSPITALS):
        waiting = []
        with stack.disconnected(offline):
            for hospital in offline:
                item = event(stack, 'partition-' + '-'.join(offline) + '-' + hospital, SITES[HOSPITALS.index(hospital)])
                capture(stack, hospital, item)
                waiting.append((hospital, item))
            # Capture HTTP remains reachable via separate control network.
            if len(offline) == 1:
                online = HOSPITALS[1 - HOSPITALS.index(offline[0])]
                item = event(stack, 'online-during-' + offline[0], SITES[HOSPITALS.index(online)])
                capture(stack, online, item)
                drain(stack, online, [item], automatic=True)
            for hospital, item in waiting:
                require(stack.query('SELECT count(*) FROM events WHERE event_id=%s', (item['event_id'],)) == [(0,)], 'Partitioned event reached Cloud')
                require(stack.sqlite(hospital, 'SELECT status FROM events WHERE event_id=?', [item['event_id']]) == [['pending']], 'Pending event lost')
        for hospital, item in waiting:
            drain(stack, hospital, [item], automatic=True)
            stack.until(lambda hospital=hospital: stack.http(hospital, 'GET', '/recovery/status')['replayable_backlog'] == 0,
                        description='recovery backlog zero')
        stack.wait_ready()
        evidence.append({'disconnected': list(offline), 'restored': True, 'pending_durable_then_applied_once': [e['event_id'] for _, e in waiting]})
    for services, label in ((('patient', 'emergency', 'admin'), 'Cloud'), (('controller',), 'controller')):
        waiting = []
        with stack.stopped(services):
            for hospital, site in zip(HOSPITALS, SITES):
                item = event(stack, label + '-outage-' + site, site)
                capture(stack, hospital, item)
                waiting.append((hospital, item))
            if label == 'Cloud':
                views = stack.http('dashboard', 'GET', '/api/services')
                require(all(v['http_status'] is None for v in views.values()), 'Dashboard failed to surface upstream outage')
            else:
                require(stack.http('dashboard', 'GET', '/api/recovery')['controller']['status'] == 'unavailable', 'Dashboard did not surface controller outage')
            for _, item in waiting:
                require(stack.query('SELECT count(*) FROM events WHERE event_id=%s', (item['event_id'],)) == [(0,)], 'Unavailable dependency was bypassed')
        for hospital, item in waiting:
            drain(stack, hospital, [item], automatic=True)
        stack.wait_ready()
    stack.configure({hospital: {'AUTO_REPLAY': '0'} for hospital in HOSPITALS})
    return {'detail': 'Independent A/B/both backend partitions preserve capture; unaffected site progresses; controlled Cloud/controller outages restored', 'partitions': evidence}


def restarts_and_faults(stack):
    evidence = []
    pending = event(stack, 'pending-restart')
    capture(stack, 'hospital-a', pending)
    proof = stack.fault_gate()
    before = stack.sqlite('hospital-a', 'SELECT event_id,site_id,status FROM events WHERE event_id=?', [pending['event_id']])
    stack.compose('restart', 'hospital-a')
    stack.wait_ready(['hospital-a'])
    require(stack.sqlite('hospital-a', 'SELECT event_id,site_id,status FROM events WHERE event_id=?', [pending['event_id']]) == before,
            'Pending SQLite state did not survive restart')
    drain(stack, 'hospital-a', [pending])
    # An issued grant survives controller restart and expires under a labeled
    # expiry-clock SQL fixture, using real reconciliation/fencing endpoints.
    item = event(stack, 'grant-restart')
    stack.http('controller', 'POST', '/hospitals/Hospital_A/candidate',
               {'event_id': item['event_id'], 'priority': 2, 'event_digest': event_digest(item)}, headers=stack.hospital_auth('Hospital_A'))
    stack.compose('restart', 'controller')
    stack.wait_ready(['controller'])
    require(any(t['event_id'] == item['event_id'] for t in stack.api('GET', '/tickets')), 'Ticket lost across controller restart')
    grant = stack.api('POST', '/grants/next')
    stack.compose('restart', 'controller')
    stack.wait_ready(['controller'])
    require(stack.api('GET', '/grants/by-id/' + grant['grant_id'])['status'] == 'issued', 'Outstanding grant lost')
    stack.query("UPDATE controller_grants SET expires_at=clock_timestamp()-INTERVAL '1 second' WHERE grant_id=%s", (grant['grant_id'],), fixture=True)
    stack.api('POST', '/grants/reconcile-expired')
    stack.http('controller', 'POST', '/grants/' + grant['grant_id'] + '/acknowledge', {'hospital_id': 'Hospital_A'}, headers=stack.hospital_auth('Hospital_A'), expected=409)
    stack.http('controller', 'POST', '/hospitals/Hospital_A/candidate', {}, headers=stack.hospital_auth('Hospital_A'))
    evidence.append({'ticket_persisted': item['event_id'], 'issued_grant_persisted_then_expired': grant['grant_id'], 'expiry_method': 'SQL clock fixture'})
    for mode in ('drop_ack_for', 'drop_cloud_for'):
        item = event(stack, mode)
        with proxy_fault(stack, {mode: item['event_id']}):
            capture(stack, 'hospital-a', item)
            drain(stack, 'hospital-a', [item], timeout=120)
            stats = stack.http('fault-proxy', 'GET', '/__faults')
            require(stats['cloud_posts'].get(item['event_id']) == 1, 'Lost response caused duplicate Cloud transmission')
            evidence.append({'actual_tcp_response_loss': mode, 'event_id': item['event_id'], 'cloud_posts': 1})
    crash = event(stack, 'crash-after-commit')
    capture(stack, 'hospital-a', crash)
    with proxy_fault(stack, {'hold_cloud_for': crash['event_id']}):
        cid = stack.fault_gate()['containers']['hospital-a']
        with ThreadPoolExecutor(max_workers=1) as pool:
            attempt = pool.submit(replay, stack, 'hospital-a')
            stack.until(lambda: crash['event_id'] in stack.http('fault-proxy', 'GET', '/__faults')['held'],
                        description='Cloud commit held behind fault proxy')
            require(stack.query('SELECT count(*) FROM events WHERE event_id=%s', (crash['event_id'],)) == [(1,)], 'Crash boundary has no committed event')
            stack.active_faults.append({'kind': 'hospital SIGKILL', 'container_id': cid})
            stack.save()
            try:
                stack.docker('kill', '--signal', 'KILL', cid)
                try:
                    attempt.result(timeout=15)
                except Exception as exc:
                    # The interrupted HTTP replay is an expected observed fault,
                    # not silently ignored as a successful request.
                    evidence.append({'event_id': crash['event_id'], 'expected': 'interrupted replay HTTP', 'actual': type(exc).__name__})
            finally:
                stack.fault_gate()
                stack.docker('start', cid)
                stack.wait_ready(['hospital-a'])
                stack.active_faults.pop()
    drain(stack, 'hospital-a', [crash], timeout=90)
    require(stack.http('fault-proxy', 'GET', '/__faults')['cloud_posts'][crash['event_id']] == 1, 'Crash recovery resent an applied event')
    # Test-only wrong-service routing obtains an actual Cloud HTTP 422 while
    # capture still validates a supported schema. Production code is unchanged.
    bad = event(stack, 'permanent-invalid', kind='emergency_record')
    dependent = event(stack, 'quarantine-descendant', prerequisites=[bad['event_id']])
    capture(stack, 'hospital-a', bad)
    capture(stack, 'hospital-a', dependent)
    stack.configure({'hospital-a': {'EMERGENCY_URL': 'http://patient:8000/events'}})
    try:
        replay(stack, 'hospital-a')
    finally:
        stack.configure({'hospital-a': {'EMERGENCY_URL': 'http://emergency:8000/events'}})
    require(stack.sqlite('hospital-a', 'SELECT quarantined FROM recovery_state WHERE event_id=?', [bad['event_id']]) == [[1]], 'Permanent error not quarantined')
    missing = event(stack, 'missing-prerequisite', prerequisites=[stack.project + '-missing-parent'])
    capture(stack, 'hospital-a', missing)
    preview = stack.http('hospital-a', 'GET', '/scheduler/inspect')
    by_id = {e['event_id']: e for e in preview['events']}
    require(not by_id[dependent['event_id']]['eligible'] and not by_id[missing['event_id']]['eligible'], 'Blocked/missing workflow selected')
    # Genuine transient HTTP 503 via a short-lived PostgreSQL trigger.
    transient = event(stack, 'transient-db-error', site='Hospital_B')
    capture(stack, 'hospital-b', transient)
    stack.query("""CREATE FUNCTION e2e_transient() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF NEW.event_id LIKE '%transient-db-error' THEN RAISE EXCEPTION 'isolated transient write failure' USING ERRCODE='08006'; END IF; RETURN NEW; END $$""", fixture=True)
    stack.query('CREATE TRIGGER e2e_transient BEFORE INSERT ON events FOR EACH ROW EXECUTE FUNCTION e2e_transient()', fixture=True)
    try:
        replay(stack, 'hospital-b')
        retry = stack.sqlite('hospital-b', 'SELECT retry_count,retry_after FROM recovery_state WHERE event_id=?', [transient['event_id']])
        require(retry and retry[0][0] >= 1 and retry[0][1], 'Transient error did not persist backoff')
        # Docker exec overhead may exceed the actual 1–2 s first retry delay.
        # A labeled deadline fixture isolates Inspector eligibility from that
        # observer overhead; the genuine persisted retry journal is retained.
        future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        stack.sqlite('hospital-b', 'UPDATE recovery_state SET retry_after=? WHERE event_id=?', [future, transient['event_id']], fixture=True)
        preview = stack.http('hospital-b', 'GET', '/scheduler/inspect')
        row = next(e for e in preview['events'] if e['event_id'] == transient['event_id'])
        require(not row['eligible'], 'Retry deadline was ignored')
        stack.sqlite('hospital-b', 'UPDATE recovery_state SET retry_after=? WHERE event_id=?', [retry[0][1], transient['event_id']], fixture=True)
    finally:
        stack.query('DROP TRIGGER e2e_transient ON events', fixture=True)
        stack.query('DROP FUNCTION e2e_transient()', fixture=True)
    drain(stack, 'hospital-b', [transient])
    return {'detail': 'Real restarts, persisted tickets/grants, expired fencing, TCP ACK/COMMIT response loss, quarantine/missing dependencies and transactional error/backoff fixtures', 'faults': evidence,
            'intentionally_unresolved': [bad['event_id'], dependent['event_id'], missing['event_id']]}


def admission(stack):
    # Small, bounded probes of the actual telemetry-driven state machine. No
    # H1/H2 load experiment, fabricated metrics or production threshold changes.
    settings = {'CONTROLLER_ADMISSION_ENABLED': '1', 'CONTROLLER_ADMISSION_POLICY': 'fixed',
        'CONTROLLER_ADMISSION_POLL_SECONDS': '1', 'CONTROLLER_ADMISSION_MAX_AGE_SECONDS': '5',
        'CONTROLLER_ADMISSION_TIMEOUT_SECONDS': '1', 'CONTROLLER_ADMISSION_DWELL_SECONDS': '1',
        'CONTROLLER_ADMISSION_HEALTHY_SAMPLES': '1', 'CONTROLLER_ADMISSION_NORMAL_RPS': '2',
        'CONTROLLER_ADMISSION_CAUTION_RPS': '1', 'CONTROLLER_ADMISSION_RECOVERY_RPS': '.5',
        'CONTROLLER_ADMISSION_NORMAL_INFLIGHT': '2', 'CONTROLLER_ADMISSION_CAUTION_INFLIGHT': '1',
        'CONTROLLER_ADMISSION_RECOVERY_INFLIGHT': '1', 'CONTROLLER_ADMISSION_HEALTHY_THROUGHPUT_RPS': '100',
        'CONTROLLER_ADMISSION_CAUTION_THROUGHPUT_RPS': '200', 'CONTROLLER_ADMISSION_RECOVERY_THROUGHPUT_RPS': '400'}
    # Record actual controller state changes in a test-only observation table.
    # This avoids inferring missed transitions from slow Docker exec polling.
    stack.query('CREATE TABLE e2e_state_observations (state TEXT, observed_at TIMESTAMPTZ DEFAULT clock_timestamp())', fixture=True)
    stack.query("""CREATE FUNCTION e2e_observe_state() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        INSERT INTO e2e_state_observations(state) VALUES(NEW.state); RETURN NEW; END $$""", fixture=True)
    stack.query('''CREATE TRIGGER e2e_observe_state AFTER UPDATE OF state ON controller_admission
        FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state) EXECUTE FUNCTION e2e_observe_state()''', fixture=True)
    # Restart Cloud processes only in this test stack to obtain a fresh real
    # 60-second window after deliberate earlier errors. No telemetry table reset.
    stack.compose('restart', 'patient', 'emergency', 'admin')
    stack.wait_ready(['patient', 'emergency', 'admin'])
    stack.configure({'controller': settings})
    normal = stack.until(lambda: (s if (s := stack.api('GET', '/admission/status'))['state'] == 'Normal' and s['telemetry_fresh'] else None),
                         description='fresh Normal admission')
    a, b = event(stack, 'admission-cap-A'), event(stack, 'admission-cap-B', 'Hospital_B')
    for item in (a, b):
        stack.http('controller', 'POST', '/hospitals/' + item['site_id'] + '/candidate',
            {'event_id': item['event_id'], 'priority': 2, 'event_digest': event_digest(item)}, headers=stack.hospital_auth(item['site_id']))
    first, immediate = stack.http_batch('controller', [
        {'method': 'POST', 'path': '/grants/next', 'headers': stack.operator()},
        {'method': 'POST', 'path': '/grants/next', 'headers': stack.operator()}])
    require('grant_id' in first, 'Fresh admission denied first grant')
    require('grant_id' not in immediate, 'Fixed pacing permitted immediate second allocation')
    second = {}
    def next_grant():
        value = stack.api('POST', '/grants/next')
        second.update(value)
        return value.get('grant_id')
    stack.until(next_grant, timeout=10, description='paced second allocation')
    gap = stack.query("""SELECT extract(epoch FROM max(created_at)-min(created_at)) FROM controller_grant_audit
        WHERE grant_id=ANY(%s::uuid[]) AND action='allocate'""", ([first['grant_id'], second['grant_id']],))[0][0]
    require(float(gap) >= .45, 'Actual allocation timestamps violate configured pacing')
    extra = event(stack, 'admission-cap-extra')
    # ACK both before testing the active-cap branch; never replace an issued
    # candidate while constructing this cap oracle.
    for grant in (first, second):
        stack.until(lambda grant=grant: stack.http('controller', 'POST', f"/grants/{grant['grant_id']}/acknowledge",
            {'hospital_id': grant['hospital_id']}, headers=stack.hospital_auth(grant['hospital_id']), expected=(200, 429)).get('status') == 'acknowledged',
            timeout=10, description='paced ACK')
    # Use operator ticket on a synthetic third identity; no third hospital service
    # is implied, just a controlled durable queue candidate for cap verification.
    stack.api('POST', '/tickets', {'hospital_id': 'E2E_CAP_PROBE', 'event_id': extra['event_id'], 'priority': 2, 'event_digest': event_digest(extra)})
    time.sleep(.6)
    require('grant_id' not in stack.api('POST', '/grants/next'), 'In-flight cap admitted a third active grant')
    for grant in (first, second):
        stack.http('controller', 'POST', f"/grants/{grant['grant_id']}/abandon", {'hospital_id': grant['hospital_id']}, headers=stack.hospital_auth(grant['hospital_id']))
    stack.api('POST', '/hospitals/E2E_CAP_PROBE/candidate', {})
    transitions = [normal['state']]
    stack.api('POST', '/tickets', {'hospital_id': 'E2E_STALE_PROBE', 'event_id': extra['event_id'] + '-stale', 'priority': 2})
    with stack.stopped(['admin']):
        recovery = stack.until(lambda: (s if (s := stack.api('GET', '/admission/status'))['state'] == 'Recovery' else None),
                               description='Recovery on unavailable telemetry')
        transitions.append(recovery['state'])
        require(not recovery['telemetry_fresh'], 'Missing telemetry was reported fresh')
        require('grant_id' not in stack.api('POST', '/grants/next'), 'Stale telemetry admitted work')
    def observe_restore():
        snapshot = stack.api('GET', '/admission/status')
        if transitions[-1] != snapshot['state']:
            transitions.append(snapshot['state'])
        return snapshot if snapshot['state'] == 'Normal' else None
    restored = stack.until(observe_restore, description='Recovery to Normal')
    observed = stack.query('SELECT state FROM e2e_state_observations ORDER BY observed_at')
    transitions = [row[0] for row in observed]
    require(any(transitions[i:i+3] == ['Recovery', 'Caution', 'Normal'] for i in range(max(0, len(transitions)-2))),
            'Stepwise Recovery -> Caution -> Normal transition not observed in durable state-change evidence')
    stack.api('POST', '/hospitals/E2E_STALE_PROBE/candidate', {})
    transitions.append(restored['state'])
    stack.configure({'controller': {'CONTROLLER_ADMISSION_POLICY': 'aimd', 'CONTROLLER_ADMISSION_ADDITIVE_RPS': '.5'}})
    before = stack.until(lambda: (s if (s := stack.api('GET', '/admission/status'))['rate_rps'] >= 1 else None), description='AIMD healthy increase')
    with stack.stopped(['admin']):
        reduced = stack.until(lambda: (s if (s := stack.api('GET', '/admission/status'))['rate_rps'] < before['rate_rps'] else None), description='AIMD multiplicative reduction')
    recovered = stack.until(lambda: (s if (s := stack.api('GET', '/admission/status'))['state'] == 'Normal' and s['rate_rps'] > reduced['rate_rps'] else None), description='AIMD additive recovery')
    stack.configure({'controller': {'CONTROLLER_ADMISSION_ENABLED': '0', 'CONTROLLER_ADMISSION_POLICY': 'fixed'}})
    stack.query('DROP TRIGGER e2e_observe_state ON controller_admission', fixture=True)
    stack.query('DROP FUNCTION e2e_observe_state()', fixture=True)
    stack.query('DROP TABLE e2e_state_observations', fixture=True)
    return {'detail': 'Actual fresh/missing telemetry, fixed allocation pacing, in-flight cap, durable ACK pacing and opt-in AIMD increase/decrease',
            'allocation_gap_seconds': float(gap), 'states': transitions,
            'aimd_rates': [before['rate_rps'], reduced['rate_rps'], recovered['rate_rps']],
            'limitation': 'Observed functional state transitions and pacing only; no performance superiority claim'}


def experimental_admission(stack):
    # Both modes are enabled only in the generated disposable configuration.
    stack.configure({'controller': {'CRISISGUARD_EXPERIMENTAL_POLICIES': '1',
        'CONTROLLER_ADMISSION_ENABLED': '1', 'CONTROLLER_ADMISSION_POLICY': 'aimd',
        'CONTROLLER_ADMISSION_HEALTH_POLICY': 'pressure-experimental',
        'CONTROLLER_ADMISSION_NORMAL_RPS': '1', 'CONTROLLER_ADMISSION_CAUTION_RPS': '.5',
        'CONTROLLER_ADMISSION_RECOVERY_RPS': '.25'},
        'hospital-b': {'CRISISGUARD_EXPERIMENTAL_POLICIES': '1', 'METAGUARD_RETRY_POLICY': 'bounded-deferral'}})
    state = stack.until(lambda: (s if (s := stack.api('GET', '/admission/status'))['state'] == 'Normal'
                        and s['telemetry_fresh'] and s['health_policy'] == 'pressure-experimental' else None),
                        description='opt-in pressure-health with fresh measured tails/queue')
    items = [event(stack, 'deferral-' + str(index), 'Hospital_B', priority=5) for index in range(3)]
    for item in items:
        capture(stack, 'hospital-b', item)
    response = replay(stack, 'hospital-b')
    require(any(r['status'] == 'admission_deferred' for r in response['results']), 'Bounded-deferral policy did not exercise a real paced denial')
    rows = stack.sqlite('hospital-b', 'SELECT event_id,reason,retry_after FROM metaguard_deferrals ORDER BY id')
    require(rows and all(r[1] in ('rate_limit', 'inflight_limit') for r in rows), 'No durable definite deferral proof')
    deferred = rows[-1][0]
    retries = stack.sqlite('hospital-b', 'SELECT retry_count FROM recovery_state WHERE event_id=?', [deferred])
    require(retries == [[0]], 'Admission-only deferral incorrectly became a failure retry')
    drain(stack, 'hospital-b', items, timeout=100)
    stack.configure({'controller': {'CONTROLLER_ADMISSION_ENABLED': '0', 'CONTROLLER_ADMISSION_POLICY': 'fixed',
                                   'CONTROLLER_ADMISSION_HEALTH_POLICY': 'throughput'},
                     'hospital-b': {'METAGUARD_RETRY_POLICY': 'failure-backoff'}})
    return {'detail': 'Actual pressure-health telemetry validation and bounded-deferral HTTP hints/journal; three events apply once without failure-counter inflation',
            'health_policy': state['health_policy'], 'deferrals': rows}


def dashboard(stack):
    services = stack.http('dashboard', 'GET', '/api/services')
    require(all(v['http_status'] == 200 and v['response']['database'] == stack.settings['POSTGRES_DB'] for v in services.values()), 'Dashboard service/database identity mismatch')
    hospitals = stack.http('dashboard', 'GET', '/api/hospitals')
    for hospital in HOSPITALS:
        rows = stack.http(hospital, 'GET', '/events')
        require({r['event_id'] for r in hospitals[hospital]['events']} == {r['event_id'] for r in rows}, 'Dashboard queue IDs differ from hospital')
        preview = stack.http('dashboard', 'GET', f'/api/hospitals/{hospital}/scheduler/inspect')
        direct = stack.http(hospital, 'GET', '/scheduler/inspect')
        require(preview['next_event_id'] == direct['next_event_id'], 'Dashboard Inspector differs')
    history = stack.http('dashboard', 'GET', '/api/history?limit=500')
    expected = stack.query('SELECT event_id FROM events ORDER BY event_id')
    require({e['event_id'] for e in history['events']} == {r[0] for r in expected}, 'Dashboard history != authoritative SQL IDs')
    recovery = stack.http('dashboard', 'GET', '/api/recovery')
    require(recovery['controller']['state'] == stack.api('GET', '/admission/status')['state'], 'Dashboard controller state mismatch')
    # Exactly ten authenticated existing-record reads per API, no event writes.
    before = stack.query('SELECT count(*) FROM events')
    stack.http('dashboard', 'POST', '/api/test-traffic', {'count': 10}, expected=403)
    headers = {'X-CrisisGuard-Local-Action': 'test-traffic'}
    stack.http('dashboard', 'POST', '/api/test-traffic', {'count': 1}, headers=headers, expected=422)
    started, denied = stack.http_batch('dashboard', [
        {'method': 'POST', 'path': '/api/test-traffic', 'body': {'count': 10}, 'headers': headers, 'expected': 202},
        {'method': 'POST', 'path': '/api/test-traffic', 'body': {'count': 10}, 'headers': headers, 'expected': 409}])
    job = stack.until(lambda: (s if not (s := stack.http('dashboard', 'GET', '/api/test-traffic'))['active'] else None),
                      timeout=45, description='bounded dashboard read generator')
    require(job['run']['status'] == 'completed' and all(v['succeeded'] == 10 and v['failed'] == 0 for v in job['run']['services'].values()), 'Dashboard test traffic did not complete 30 verified reads')
    require(stack.query('SELECT count(*) FROM events') == before, 'Read generator mutated event ledger')
    metrics = stack.http('dashboard', 'GET', '/api/metrics')
    measured = {}
    for service in ('patient', 'emergency', 'admin'):
        sample = metrics[service]['response']
        require(metrics[service]['http_status'] == 200 and sample['database_healthy'] is True
                and sample['live_latency_sample_count'] >= 10 and isinstance(sample['live_p95_latency_ms'], (float, int)),
                'Dashboard live p95/population/DB health missing')
        require(0 <= sample['live_slo_violation_count'] <= sample['live_latency_sample_count'] and
                math.isclose(sample['live_slo_violation_rate'], sample['live_slo_violation_count'] / sample['live_latency_sample_count'], abs_tol=.000051),
                'Live SLO counters/rate do not reconcile')
        measured[service] = {k: sample[k] for k in ('live_p95_latency_ms', 'live_latency_sample_count', 'live_slo_violation_count', 'live_slo_violation_rate', 'database_healthy')}
    return {'detail': 'Actual Dashboard HTTP views reconciled to hospital/SQL/controller; 30 bounded read-only generator requests with validation/concurrency; real p95/SLO arithmetic', 'metrics': measured}


def comprehensive(stack):
    scenarios = [('Cloud APIs: validation/auth/duplicate/dependency/fencing', cloud_contracts),
                 ('Scheduler: actual inheritance/ties/ageing', scheduler),
                 ('MetaGuard: durable experimental scheduling policies', policies),
                 ('Independent network outages and automatic recovery', partitions),
                 ('Restart, response loss, retry and quarantine recovery', restarts_and_faults),
                 ('MetaGuard: fixed pacing/caps/telemetry/AIMD', admission),
                 ('Experimental pressure-health and bounded-deferral', experimental_admission),
                 ('Dashboard API, history, recovery and live test traffic', dashboard)]
    for index, (name, action) in enumerate(scenarios):
        passed = stack.scenario(name, lambda action=action: action(stack))
        require(not stack.active_faults, 'A fault failed restoration; further work/cleanup blocked')
        if not passed:
            for later, _ in scenarios[index + 1:]:
                stack.record(later, 'SKIP', 'Earlier dependent E2E scenario failed; evidence retained before owned cleanup')
            break
    stack.record('Performance acceptance / sustained fairness / full D2', 'NOT VERIFIED',
                 'Functional E2E scenarios do not establish performance targets or replace historical experiment evidence')
    stack.save()
