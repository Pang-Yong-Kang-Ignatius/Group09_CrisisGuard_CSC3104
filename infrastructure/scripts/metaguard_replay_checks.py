"""Checkpoint 4 live scenarios, invoked only inside smoke_metaguard's fresh project."""
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx

if __package__:
    from . import smoke_deployment as smoke
else:
    import smoke_deployment as smoke


def verify_replay(call, api, query, settings, project, passed):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from cloud.metaguard import event_digest

    hospital_ports = [int(settings[key]) for key in ('HOSPITAL_A_PORT', 'HOSPITAL_B_PORT')]
    sites = ['Hospital_A', 'Hospital_B']
    names = ['hospital-a', 'hospital-b']
    tokens = [settings['HOSPITAL_A_CONTROLLER_TOKEN'], settings['HOSPITAL_B_CONTROLLER_TOKEN']]
    chains = [smoke.event_chain(project, 'offline', site) for site in sites]
    chains[0][0]['event_id'] = '..'
    chains[0][1]['prerequisite_ids'] = ['..']

    def request(service, method, path, body=None, *, token=None, gid=None, expected=200):
        port = settings['CONTROLLER_PORT'] if service == 'controller' else settings[service.upper() + '_PORT']
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        if gid:
            headers['X-MetaGuard-Grant'] = gid
        response = httpx.request(method, f'http://127.0.0.1:{port}{path}', json=body, headers=headers, timeout=15)
        assert response.status_code == expected, (service, path, response.status_code, response.text)
        return response.json()

    call('stop', 'controller', 'patient', 'emergency', 'admin', 'postgres')
    for port, chain in zip(hospital_ports, chains):
        for event in reversed(chain):
            smoke.request(port, '/capture', event, expected=201)
        smoke.check_records(smoke.request(port, '/events'), chain, 'pending')
    # Recreate hospital processes against their own persisted SQLite volumes.
    call('up', '-d', '--no-build', '--no-deps', '--force-recreate', '--wait', *names)
    for port, chain in zip(hospital_ports, chains):
        smoke.check_records(smoke.request(port, '/events'), chain, 'pending')
    passed('Hospital A/B capture and SQLite persistence while controller, PostgreSQL and Cloud are unavailable')

    call('up', '-d', '--no-build', '--wait', '--wait-timeout', '120', 'patient', 'emergency', 'admin')
    time.sleep(7)
    assert query('SELECT COUNT(*) FROM events') == [(0,)]
    assert query('SELECT COUNT(*) FROM controller_grants') == [(0,)]
    for port, chain in zip(hospital_ports, chains):
        smoke.check_records(smoke.request(port, '/events'), chain, 'pending')
    passed('Healthy Cloud alone cannot bypass unavailable MetaGuard; no ungranted event application')

    call('up', '-d', '--no-build', '--wait', 'controller')
    for port, chain in zip(hospital_ports, chains):
        smoke.wait_for_recovery(port, True, 0, drained=True, timeout=120)
        smoke.check_records(smoke.request(port, '/events'), chain, 'applied')
    assert query("SELECT hospital_id, COUNT(*) FROM controller_grants WHERE status='completed' GROUP BY hospital_id ORDER BY hospital_id") == [('Hospital_A', 4), ('Hospital_B', 4)]
    assert query("SELECT COUNT(*) FROM controller_grant_audit WHERE new_status='acknowledged'") == [(8,)]
    smoke.verify_dashboard(int(settings['DASHBOARD_PORT']), chains, 'applied', event_traffic=True)
    passed('Simultaneous A/B automatic grant registration, allocation, ACK, Cloud apply and authoritative completion: eight events')

    b_grant = query("SELECT grant_id::text FROM controller_grants WHERE hospital_id='Hospital_B' LIMIT 1")[0][0]
    for method, path, body in [
        ('POST', '/tickets', {'hospital_id': 'Hospital_B', 'event_id': 'FORGED', 'priority': 5}),
        ('POST', f'/grants/{b_grant}/acknowledge', {'hospital_id': 'Hospital_A'}),
        ('POST', f'/grants/{b_grant}/cancel', {'hospital_id': 'Hospital_A'}),
        ('POST', f'/grants/{b_grant}/complete', {'hospital_id': 'Hospital_A'}),
        ('POST', f'/grants/{b_grant}/reconcile', {'hospital_id': 'Hospital_A'}),
        ('GET', f'/grants/by-id/{b_grant}', None),
        ('POST', '/hospitals/Hospital_B/grants/next', None),
    ]:
        request('controller', method, path, body, token=tokens[0], expected=403)
    request('controller', 'GET', '/tickets', token='wrong', expected=401)
    request('patient', 'POST', '/events', chains[1][0], token=tokens[0], gid=b_grant, expected=403)
    request('patient', 'POST', '/events', chains[0][0], expected=401)
    request('patient', 'POST', '/events', chains[0][0], token=tokens[0], gid=str(uuid4()), expected=409)
    passed('Real controller and Cloud reject cross-hospital ownership, missing/wrong credentials and absent grants')

    before = query('SELECT event_id, applied_at FROM events ORDER BY event_id')
    call('restart', 'controller')
    call('up', '-d', '--no-build', '--wait', 'controller')
    assert before == query('SELECT event_id, applied_at FROM events ORDER BY event_id')
    passed('Controller container restart retains completed grants and immutable Cloud application records')

    # Model two crashes after ACK: A applied with a lost response; B never applied.
    call('stop', 'patient', 'emergency', 'admin')
    extra = [smoke.event_chain(project, 'uncertain', site) for site in sites]
    for port, chain in zip(hospital_ports, extra):
        smoke.wait_for_recovery(port, False, 0)
        for event in reversed(chain):
            smoke.request(port, '/capture', event, expected=201)
    call('stop', *names)
    for chain in extra:
        event = chain[0]
        assert api('POST', '/tickets', {'hospital_id': event['site_id'], 'event_id': event['event_id'],
                   'priority': 5, 'event_digest': event_digest(event)})['added']
    grants = {grant['hospital_id']: grant for grant in [api('POST', '/grants/next'), api('POST', '/grants/next')]}
    for site, token in zip(sites, tokens):
        request('controller', 'POST', '/grants/' + grants[site]['grant_id'] + '/acknowledge',
                {'hospital_id': site}, token=token)
    call('up', '-d', '--no-build', '--wait', 'patient', 'emergency', 'admin')
    request('patient', 'POST', '/events', extra[0][0], token=tokens[0],
            gid=grants['Hospital_A']['grant_id'], expected=201)
    for name, site, chain in zip(names, sites, extra):
        gid = grants[site]['grant_id']
        # A one-off owned container writes the same durable journal/expired lease
        # that a process killed immediately after its send would leave behind.
        program = (
            'import os,sqlite3; c=sqlite3.connect(os.environ["HOSPITAL_DB_PATH"]); '
            'c.execute("INSERT OR REPLACE INTO metaguard_attempts VALUES (?,?,?)",' +
            repr((chain[0]['event_id'], gid, 'sent')) + '); '
            'c.execute("UPDATE recovery_state SET in_flight=1, claim_token=\'crashed\', '
            'lease_expires_at=\'2000-01-01T00:00:00+00:00\' WHERE event_id=?",' +
            repr((chain[0]['event_id'],)) + '); c.commit(); c.close()')
        call('run', '--rm', '--no-deps', '-T', name, 'python', '-c', program)
        query("UPDATE controller_grants SET expires_at=NOW()-INTERVAL '1 second' WHERE grant_id=%s", (gid,))
    call('restart', 'controller')
    call('up', '-d', '--no-build', '--wait', 'controller')
    call('up', '-d', '--no-build', '--wait', *names)
    for port, first, second in zip(hospital_ports, chains, extra):
        smoke.wait_for_recovery(port, True, 0, timeout=120)
        smoke.check_records(smoke.request(port, '/events'), first + second, 'applied')
    for site, count, old_state in [('Hospital_A', 1, 'completed'), ('Hospital_B', 2, 'expired')]:
        old = grants[site]
        assert query('SELECT COUNT(*) FROM controller_grants WHERE hospital_id=%s AND event_id=%s',
                     (site, old['event_id'])) == [(count,)]
        assert query('SELECT status FROM controller_grants WHERE grant_id=%s', (old['grant_id'],)) == [(old_state,)]
        request('patient', 'POST', '/events', extra[sites.index(site)][0], token=tokens[sites.index(site)],
                gid=old['grant_id'], expected=409)
    assert query('SELECT COUNT(*) FROM events') == [(16,)]
    assert query("SELECT COUNT(*) FROM controller_grants WHERE status='completed'") == [(16,)]
    smoke.verify_dashboard(int(settings['DASHBOARD_PORT']), [a+b for a,b in zip(chains, extra)], 'applied', event_traffic=True)
    passed('Lost-response A reconciles without reissue; absent B is fenced/reissued after controller/hospital restart; 16 events applied once')
