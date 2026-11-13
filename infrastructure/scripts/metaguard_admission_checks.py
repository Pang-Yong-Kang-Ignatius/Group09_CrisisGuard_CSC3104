"""Real Cloud load/fault scenarios inside smoke_metaguard's owned Docker project.

No mocked metrics: temporary PostgreSQL triggers delay/fail actual event writes.
The existing production 60-second telemetry window is left unchanged.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import sys
import threading
import time

import httpx

if __package__:
    from . import smoke_deployment as smoke
else:
    import smoke_deployment as smoke


def verify_admission(call, api, query, settings, project, passed):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from cloud.metaguard import event_digest
    evidence = {'fault_method': 'temporary PostgreSQL trigger; actual Cloud HTTP/metrics; unchanged 60s window',
                'phases': {}, 'transitions': []}
    controller = 'http://127.0.0.1:' + settings['CONTROLLER_PORT']
    patient = 'http://127.0.0.1:' + settings['PATIENT_PORT']
    headers = {'X-Cloud-Token': settings['CONTROLLER_CLOUD_TOKEN']}
    sites = ['Hospital_A', 'Hospital_B']
    tokens = [settings['HOSPITAL_A_CONTROLLER_TOKEN'], settings['HOSPITAL_B_CONTROLLER_TOKEN']]

    def status():
        return api('GET', '/admission/status')

    def until(predicate, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.3)
        raise RuntimeError('Admission scenario timed out: ' + str(status()))

    def wait_state(expected, timeout=100):
        last = None
        def check():
            nonlocal last
            snapshot = status()
            state = snapshot['state']
            if state != last:
                evidence['transitions'].append({'state': state, 'at': snapshot['server_time'], 'reason': snapshot['reason']})
                print('STATE ' + state + ': ' + snapshot['reason'], flush=True)
                last = state
            return state == expected
        until(check, timeout)

    wait_state('Normal')
    assert status()['enabled']
    passed('Fresh real telemetry requires stepwise cold-start recovery before Normal')

    # Real hospital processes recover together; the journal and auth code are used.
    call('stop', 'controller')
    chains = [smoke.event_chain(project, 'admission', site) for site in sites]
    ports = [int(settings[key]) for key in ('HOSPITAL_A_PORT', 'HOSPITAL_B_PORT')]
    for port, chain in zip(ports, chains):
        for event in reversed(chain):
            smoke.request(port, '/capture', event, expected=201)
    time.sleep(6)
    assert query('SELECT COUNT(*) FROM events') == [(0,)]
    call('up', '-d', '--no-build', '--wait', 'controller')
    for port, chain in zip(ports, chains):
        smoke.wait_for_recovery(port, True, 0, timeout=120, expected_events=chain)
        smoke.check_records(smoke.request(port, '/events'), chain, 'applied')
    assert query("SELECT COUNT(*) FROM controller_grants WHERE status='completed'") == [(8,)]
    passed('Simultaneous authenticated A/B automatic recovery is gated and completes eight dependency-ordered events')
    call('stop', 'hospital-a', 'hospital-b')
    wait_state('Normal')

    query("CREATE TABLE cp5_fault (mode TEXT NOT NULL)")
    query("INSERT INTO cp5_fault VALUES ('healthy')")
    query("""CREATE FUNCTION cp5_fault() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE mode TEXT; BEGIN SELECT f.mode INTO mode FROM cp5_fault f;
        IF mode='error' THEN RAISE EXCEPTION 'isolated write failure' USING ERRCODE='08006'; END IF;
        IF mode='delay' THEN PERFORM pg_sleep(0.6); END IF;
        RETURN NEW; END $$""")
    query('CREATE TRIGGER cp5_fault BEFORE INSERT ON events FOR EACH ROW EXECUTE FUNCTION cp5_fault()')

    def drive(label, per_site, *, failures=False):
        barrier = threading.Barrier(2)
        responses = []
        response_lock = threading.Lock()
        with httpx.Client(timeout=15) as client:
            def own(index, method, path, body=None, *, cloud=False, gid=None):
                credential = {'Authorization': 'Bearer ' + tokens[index]}
                if gid:
                    credential['X-MetaGuard-Grant'] = gid
                return client.request(method, (patient if cloud else controller) + path,
                                      headers=credential, json=body)
            def worker(index):
                site = sites[index]
                for number in range(per_site):
                    event = {'event_id': f'{project}-{label}-{site}-{number}', 'site_id': site,
                             'event_type': 'patient_registration', 'base_priority': 5 if index == 0 else 1,
                             'prerequisite_ids': [], 'payload': {'patient_id': 'synthetic', 'phase': label}}
                    response = own(index, 'POST', '/tickets', {'hospital_id': site, 'event_id': event['event_id'],
                        'priority': event['base_priority'], 'event_digest': event_digest(event)})
                    assert response.status_code == 200 and response.json()['added']
                    if number == 0:
                        barrier.wait(timeout=10)
                    deadline = time.monotonic() + 100
                    while True:
                        lookup = own(index, 'GET', f'/hospitals/{site}/grant?event_id=' + event['event_id'])
                        assert lookup.status_code == 200
                        grant = lookup.json()['grant']
                        if grant:
                            gid = grant['grant_id']
                            response = own(index, 'POST', f'/grants/{gid}/acknowledge', {'hospital_id': site})
                            if response.status_code == 200:
                                break
                            assert response.status_code == 429
                        else:
                            assert own(index, 'POST', f'/hospitals/{site}/grants/next').status_code == 200
                        assert time.monotonic() < deadline
                        time.sleep(.05)
                    response = own(index, 'POST', '/events', event, cloud=True, gid=gid)
                    assert response.status_code == (503 if failures else 201), response.text
                    action = 'abandon' if failures else 'complete'
                    result = own(index, 'POST', f'/grants/{gid}/{action}', {'hospital_id': site})
                    assert result.status_code == 200
                    assert result.json()['status'] == ('rejected' if failures else 'completed')
                    with response_lock:
                        responses.append(response.status_code)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(worker, range(2)))
        rows = query("""SELECT a.created_at, g.hospital_id FROM controller_grant_audit a
            JOIN controller_grants g ON g.grant_id=a.grant_id WHERE a.action='allocate'
            AND g.event_id LIKE %s ORDER BY a.id""", (project + '-' + label + '-%',))
        gaps = [(later[0]-earlier[0]).total_seconds() for earlier,later in zip(rows, rows[1:])]
        snapshot = status()
        observed = snapshot['metrics']['patient']
        result = {'events': len(rows), 'state': snapshot['state'], 'configured_rps': snapshot['rate_rps'],
                  'achieved_rps': (len(rows)-1)/(rows[-1][0]-rows[0][0]).total_seconds(),
                  'minimum_grant_gap_seconds': min(gaps), 'hospital_order': [row[1] for row in rows],
                  'cloud_statuses': responses,
                  'latency_ms': observed['average_latency_ms'], 'error_rate': observed['error_rate'],
                  'throughput_rps': observed['throughput_rps'], 'database_healthy': observed['database_healthy']}
        evidence['phases'][label] = result
        print('MEASURE ' + label + ': ' + str(result), flush=True)
        assert len(rows) == per_site*2 and rows[0][1] == 'Hospital_A'
        assert all(result['hospital_order'].count(site) == per_site for site in sites)
        return result

    normal = drive('normal', 4)
    assert normal['state'] == 'Normal' and normal['minimum_grant_gap_seconds'] >= .47
    query("UPDATE cp5_fault SET mode='delay'")
    drive('latency_prime', 3)
    wait_state('Caution')
    caution = drive('caution', 2)
    assert caution['state'] == 'Caution' and caution['minimum_grant_gap_seconds'] >= 1.95
    passed('Real PostgreSQL write latency enters Caution and lowers global admission; critical A and waiting B both progress')

    query("UPDATE cp5_fault SET mode='error'")
    drive('error_prime', 3, failures=True)
    wait_state('Recovery')
    query("UPDATE cp5_fault SET mode='healthy'")
    recovery = drive('recovery', 2)
    assert recovery['state'] == 'Recovery' and recovery['minimum_grant_gap_seconds'] >= 4.95
    assert recovery['achieved_rps'] < caution['achieved_rps'] < normal['achieved_rps']
    passed('Actual Cloud HTTP 503 errors enter Recovery; measured replay pacing becomes severely restricted')

    # Cooling the real rolling window takes ~60s; no telemetry mutation/reset.
    wait_state('Normal', timeout=105)
    recovered = drive('restored', 2)
    assert recovered['state'] == 'Normal' and recovered['achieved_rps'] > recovery['achieved_rps']
    passed('Real 60-second telemetry window cools; admission recovers Recovery -> Caution -> Normal without flapping')

    call('stop', 'admin')
    wait_state('Recovery', timeout=15)
    missing = status()
    assert 'error' in missing['metrics']['admin']
    before = {key: missing[key] for key in ('next_issue_at', 'next_ack_at', 'allowed', 'denied')}
    call('restart', 'controller')
    until(lambda: status()['state'] == 'Recovery')
    after = status()
    assert all(after[key] == value for key, value in before.items())
    evidence['restart'] = {'preserved': before, 'state': after['state']}
    passed('Failed telemetry is conservative; controller restart preserves both durable cooldowns and counters')
    call('up', '-d', '--no-build', '--wait', 'admin')
    wait_state('Normal')

    query('DROP TRIGGER cp5_fault ON events')
    query('DROP FUNCTION cp5_fault()')
    query('DROP TABLE cp5_fault')
    call('start', 'hospital-a', 'hospital-b')
    call('stop', 'postgres')
    api('GET', '/admission/status', status=503)
    extra = []
    for index, port in enumerate(ports):
        event = {'event_id': project + '-db-outage-' + sites[index], 'site_id': sites[index],
                 'event_type': 'patient_registration', 'base_priority': 5, 'prerequisite_ids': [], 'payload': {}}
        smoke.request(port, '/capture', event, expected=201)
        extra.append(event)
    call('start', 'postgres')
    until(lambda: status()['enabled'])
    for port, chain, event in zip(ports, chains, extra):
        smoke.wait_for_recovery(port, True, 0, timeout=120, expected_events=chain+[event])
        smoke.check_records(smoke.request(port, '/events'), chain+[event], 'applied')
        stored = query('''SELECT event_id,site_id,event_type,base_priority,
            prerequisite_ids,payload,status FROM events WHERE event_id=%s''', (event['event_id'],))
        assert stored == [tuple(event[key] for key in (
            'event_id', 'site_id', 'event_type', 'base_priority', 'prerequisite_ids', 'payload')) + ('applied',)]
        until(lambda: query('SELECT status FROM controller_grants WHERE event_id=%s',
                            (event['event_id'],)) == [('completed',)])
    passed('PostgreSQL outage keeps capture local; restored hospitals resume without resetting admission budgets')
    return evidence
