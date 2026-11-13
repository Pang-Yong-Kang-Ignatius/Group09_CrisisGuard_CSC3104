"""Checkpoint 6 faults and unequal backlogs in the verifier's fresh project only."""
from collections import Counter
from datetime import datetime
import json
import subprocess
import time

import httpx

if __package__:
    from . import smoke_deployment as smoke
else:
    import smoke_deployment as smoke


def verify_reliability(call, api, query, settings, project, passed):
    controller = 'http://127.0.0.1:' + settings['CONTROLLER_PORT']
    proxy = 'http://127.0.0.1:' + settings['FAULT_PROXY_PORT']
    sites, names = ['Hospital_A', 'Hospital_B'], ['hospital-a', 'hospital-b']
    ports = [int(settings[key]) for key in ('HOSPITAL_A_PORT', 'HOSPITAL_B_PORT')]
    tokens = [settings['HOSPITAL_A_CONTROLLER_TOKEN'], settings['HOSPITAL_B_CONTROLLER_TOKEN']]
    expected = [[], []]
    evidence = {'faults': [], 'samples': []}

    def until(check, timeout=100):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            try:
                if check():
                    return
            except (httpx.HTTPError, OSError):
                pass
            time.sleep(.25)
        raise RuntimeError('Reliability scenario timed out')

    def event(index, label):
        return {'event_id': project+'-'+label, 'site_id': sites[index], 'event_type': 'patient_registration',
                'base_priority': 5 if index == 0 else 1, 'prerequisite_ids': [], 'payload': {'patient_id': 'synthetic'}}

    def capture(index, item):
        smoke.request(ports[index], '/capture', item, expected=201)
        expected[index].append(item)

    def applied(index, item):
        return any(row['event_id'] == item['event_id'] and row['status'] == 'applied'
                   for row in smoke.request(ports[index], '/events'))

    def faults(update=None):
        response = httpx.request('POST' if update is not None else 'GET', proxy+'/__faults', json=update, timeout=10)
        assert response.status_code == 200
        return response.json()

    def status():
        response = httpx.get(controller+'/admission/status',
                            headers={'X-Cloud-Token': settings['CONTROLLER_CLOUD_TOKEN']}, timeout=5)
        response.raise_for_status()
        return response.json()

    def safe(response):
        for secret in (settings['POSTGRES_PASSWORD'], settings['CONTROLLER_CLOUD_TOKEN'], *tokens):
            assert secret not in response.text
        assert 'Traceback' not in response.text and 'postgresql://' not in response.text

    # Existing container start bypasses Compose's initial dependency gate on purpose.
    call('stop', 'controller', 'postgres')
    subprocess.run(['docker', 'start', project+'-controller-1'], check=True, capture_output=True)
    stopped = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', project+'-postgres-1'],
                             text=True, capture_output=True, check=True)
    assert stopped.stdout.strip() == 'false'
    start_event = event(0, 'startup-db-down')
    capture(0, start_event)
    time.sleep(4)
    try:
        response = httpx.get(controller+'/health', timeout=1)
        assert response.status_code != 200
    except httpx.HTTPError:
        pass
    result = subprocess.run(['docker', 'inspect', '--format', '{{.RestartCount}}', project+'-controller-1'],
                            text=True, capture_output=True, check=True)
    assert int(result.stdout.strip()) > 0
    call('start', 'postgres')
    call('up', '-d', '--no-build', '--wait', '--wait-timeout', '120', 'controller')
    until(lambda: applied(0, start_event))
    evidence['faults'].append('startup_without_postgres_then_recovery')
    passed('Controller startup fails closed without PostgreSQL; restart policy recovers and offline capture survives')

    ack_event = event(0, 'lost-ack')
    faults({'drop_ack_for': ack_event['event_id']})
    capture(0, ack_event)
    until(lambda: applied(0, ack_event))
    stats = faults()
    assert stats['cloud_posts'][ack_event['event_id']] == 1
    assert stats['acknowledgements'][ack_event['event_id']] == 2
    assert query('SELECT status FROM controller_grants WHERE event_id=%s ORDER BY updated_at',
                 (ack_event['event_id'],)) == [('expired',), ('completed',)]
    evidence['faults'].append('actual_lost_ack_response_no_blind_cloud_send')

    drop_event = event(0, 'lost-cloud-response')
    faults({'drop_cloud_for': drop_event['event_id']})
    capture(0, drop_event)
    until(lambda: applied(0, drop_event))
    assert faults()['cloud_posts'][drop_event['event_id']] == 1
    evidence['faults'].append('actual_lost_cloud_response_authoritative_completion')
    passed('Actual TCP response loss after ACK and Cloud COMMIT reconciles safely; one Cloud send per event')

    held_event = event(0, 'crash-inflight')
    faults({'hold_cloud_for': held_event['event_id']})
    capture(0, held_event)
    until(lambda: held_event['event_id'] in faults()['held'])
    assert query('SELECT COUNT(*) FROM events WHERE event_id=%s', (held_event['event_id'],)) == [(1,)]
    call('kill', '-s', 'SIGKILL', 'hospital-a')
    program = ('import os,sqlite3; c=sqlite3.connect(os.environ["HOSPITAL_DB_PATH"]); '
               'print(c.execute("SELECT phase FROM metaguard_attempts WHERE event_id=?",'
               +repr((held_event['event_id'],))+').fetchone()[0])')
    assert call('run', '--rm', '--no-deps', '-T', 'hospital-a', 'python', '-c', program).strip() == 'sent'
    faults({'hold_cloud_for': None})
    resumed = time.monotonic()
    call('start', 'hospital-a')
    until(lambda: applied(0, held_event), timeout=75)
    assert faults()['cloud_posts'][held_event['event_id']] == 1
    evidence['inflight_restart_recovery_seconds'] = round(time.monotonic()-resumed, 3)
    evidence['faults'].append('actual_hospital_sigkill_after_cloud_commit')
    passed('Hospital SIGKILL with a persisted sent/in-flight attempt recovers without a second Cloud POST')

    # Exhaustive role/input checks use generated credentials, never local .env.
    with httpx.Client(timeout=10) as client:
        for header in ({}, {'Authorization': 'Bearer invalid'}, {'Authorization': tokens[0]}):
            response = client.get(controller+'/tickets', headers=header)
            assert response.status_code == 401
            safe(response)
        b_gid = query("SELECT grant_id::text FROM controller_grants WHERE hospital_id='Hospital_A' LIMIT 1")[0][0]
        for action in ('acknowledge','cancel','complete','reconcile','abandon'):
            response = client.post(controller+'/grants/'+b_gid+'/'+action,
                headers={'Authorization':'Bearer '+tokens[1]}, json={'hospital_id':'Hospital_A'})
            assert response.status_code == 403
            safe(response)
        for path, body in [('/grants/reconcile-expired', None), ('/grants/'+b_gid+'/resolve', {'applied':True})]:
            response = client.post(controller+path, headers={'Authorization':'Bearer '+tokens[0]}, json=body)
            assert response.status_code == 403
            safe(response)
        response = client.post(controller+'/grants/bad/complete', headers={'X-Cloud-Token':settings['CONTROLLER_CLOUD_TOKEN']}, json={'hospital_id':'Hospital_A'})
        assert response.status_code == 422
        safe(response)
    passed('Real authentication, cross-hospital manipulation, operator-only reconciliation and malformed UUID checks remain sanitized')

    # 2:1 sustained imbalance: 80 high-priority A events vs 40 low-priority B.
    call('stop', 'controller')
    chains = [[], []]
    for index, groups in enumerate((20, 10)):
        for group in range(groups):
            chain = smoke.event_chain(project, 'bulk'+str(group), sites[index])
            for item in chain:
                item['base_priority'] = 5 if index == 0 else 1
            chains[index].extend(chain)
        for item in reversed(chains[index]):
            capture(index, item)
    call('up', '-d', '--no-build', '--no-deps', '--force-recreate', '--wait', *names)
    for index in range(2):
        rows = smoke.request(ports[index], '/events')
        assert sum(row['status']=='pending' for row in rows) == len(chains[index])
    started = time.monotonic()
    call('up', '-d', '--no-build', '--wait', 'controller')
    flags = set()
    previous_report = -1
    while True:
        rows = query("SELECT site_id,COUNT(*) FROM events WHERE event_id LIKE %s GROUP BY site_id", (project+'_bulk%',))
        # event_chain joins fields with underscores.
        counts = dict(rows)
        total = sum(counts.values())
        if total//10 != previous_report:
            snapshot = status()
            evidence['samples'].append({'elapsed_seconds':round(time.monotonic()-started,3),
                'completed':counts, 'state':snapshot['state'], 'rate_rps':snapshot['rate_rps']})
            print('BACKLOG '+str(total)+'/120, '+snapshot['state']+', '+str(round(time.monotonic()-started,1))+'s', flush=True)
            previous_report = total//10
        if total >= 15 and 'cloud' not in flags:
            flags.add('cloud')
            call('stop', 'emergency')
            time.sleep(6)
            call('up', '-d', '--no-build', '--wait', 'emergency')
            evidence['faults'].append('cloud_api_outage_mid_backlog')
        if total >= 35 and 'database' not in flags:
            flags.add('database')
            before = status()['allowed']
            call('stop', 'postgres')
            response = httpx.get(controller+'/admission/status', headers={'X-Cloud-Token':settings['CONTROLLER_CLOUD_TOKEN']},timeout=10)
            assert response.status_code == 503
            safe(response)
            for index in range(2):
                capture(index, event(index, 'database-down-'+sites[index]))
            time.sleep(3)
            call('start', 'postgres')
            until(lambda: status()['allowed'] >= before)
            evidence['faults'].append('postgres_outage_mid_backlog_with_capture')
        if total >= 55 and 'controller' not in flags:
            flags.add('controller')
            call('kill', '-s', 'SIGKILL', 'controller')
            call('up', '-d', '--no-build', '--wait', 'controller')
            evidence['faults'].append('controller_sigkill_mid_backlog')
        if total >= 75 and 'hospital' not in flags:
            flags.add('hospital')
            assert any(row['status']=='pending' for row in smoke.request(ports[0], '/events'))
            call('kill', '-s', 'SIGKILL', 'hospital-a')
            call('start', 'hospital-a')
            evidence['faults'].append('hospital_sigkill_pending_backlog')
        if total == 120:
            break
        assert time.monotonic()-started < 600, 'Large backlog stalled'
        time.sleep(1)
    assert flags == {'cloud','database','controller','hospital'}
    for index in range(2):
        until(lambda index=index: all(row['status']=='applied' for row in smoke.request(ports[index], '/events')))
        smoke.check_records(smoke.request(ports[index], '/events'), expected[index], 'applied')
    duration = time.monotonic()-started
    records = query('SELECT event_id,site_id,prerequisite_ids,applied_at FROM events')
    lookup = {row[0]:row for row in records}
    assert len(records) == len(expected[0])+len(expected[1]) == 126
    for eid, site, parents, applied_at in records:
        assert all(parent in lookup and lookup[parent][1]==site and lookup[parent][3]<=applied_at for parent in parents)
    stats = faults()
    # A real DB outage can reject a first send. Retries then require a distinct
    # authoritatively retired attempt, rather than assuming every send succeeded.
    for eid, count in stats['cloud_posts'].items():
        if count > 1:
            retired = query("""SELECT COUNT(*) FROM controller_grant_audit a
                JOIN controller_grants g ON g.grant_id=a.grant_id
                WHERE g.event_id=%s AND a.new_status='expired'
                AND a.action='authoritative_cloud_query'""", (eid,))[0][0]
            assert retired >= count-1, 'A patient event was blindly sent again'
    bulk = [row for row in records if '_bulk' in row[0]]
    completions = Counter(row[1] for row in bulk)
    assert completions == {'Hospital_A':80,'Hospital_B':40}
    fractions = [completions[sites[index]]/len(chains[index]) for index in range(2)]
    grants = query("""SELECT a.created_at,g.hospital_id FROM controller_grant_audit a
        JOIN controller_grants g ON a.grant_id=g.grant_id WHERE a.action='allocate' AND g.event_id LIKE %s
        ORDER BY a.id""", (project+'_bulk%',))
    low_times = [row[0] for row in grants if row[1]=='Hospital_B']
    evidence.update(backlog_events=120, total_events=126, backlog_duration_seconds=round(duration,3),
        completion_throughput_rps=round(120/duration,4),
        completions_by_hospital={'Hospital_A':80,'Hospital_B':40},
        max_low_priority_grant_gap_seconds=max((b-a).total_seconds() for a,b in zip(low_times,low_times[1:])),
        fairness_completion_fraction_jain=sum(fractions)**2/(2*sum(value**2 for value in fractions)),
        patient_events=len(stats['cloud_posts']), patient_posts=sum(stats['cloud_posts'].values()),
        metrics={name:sample for name,sample in status()['metrics'].items()})
    for path in ('/health','/api/services','/api/hospitals','/api/history?limit=100','/api/metrics'):
        smoke.request(int(settings['DASHBOARD_PORT']), path, timeout=35)
    passed('120-event unequal backlogs drain through four mid-replay faults; 126 total events retained, dependencies and patient send counts verified')
    print('RELIABILITY MEASURE '+json.dumps({key:value for key,value in evidence.items() if key not in ('metrics','samples')}, default=str), flush=True)
    return evidence
