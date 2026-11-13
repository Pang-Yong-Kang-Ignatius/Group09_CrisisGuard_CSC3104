"""Durable grant attempts. Only the first confirmed ACK may authorize one send."""
import os
import sqlite3
from urllib.parse import quote

import httpx

from cloud.metaguard import event_digest
from edge.database import db
from edge.capture_api import admission_retry


def enabled():
    return os.getenv('METAGUARD_ENABLED') == '1'


def outstanding_event():
    with sqlite3.connect(db.DB_PATH) as conn:
        row = conn.execute("""SELECT a.event_id FROM metaguard_attempts a JOIN events e
            ON e.event_id=a.event_id JOIN recovery_state r ON r.event_id=e.event_id
            WHERE e.status='pending' AND r.quarantined=0 ORDER BY a.rowid LIMIT 1""").fetchone()
    return row[0] if row else None


def pinned_event():
    """Only uncertain ACK/send attempts pin local selection; unknown phases fail closed."""
    with sqlite3.connect(db.DB_PATH) as conn:
        row = conn.execute("""SELECT a.event_id FROM metaguard_attempts a
            JOIN events e ON e.event_id=a.event_id
            JOIN recovery_state r ON r.event_id=e.event_id
            WHERE e.status='pending' AND r.quarantined=0 AND a.phase!='registered'
            ORDER BY a.rowid LIMIT 1""").fetchone()
    return row[0] if row else None


def advertise_candidate(event=None):
    """Controller atomically fences an old issued grant before changing candidates."""
    hospital = os.getenv('SITE_ID')
    if not hospital or (event is not None and hospital != event['site_id']):
        raise RuntimeError('MetaGuard hospital identity mismatch')
    body = {} if event is None else {
        'event_id': event['event_id'],
        'priority': min(event.get('effective_priority', event['base_priority']), 2147483647),
        'event_digest': event_digest(event),
    }
    return _request('POST', f'/hospitals/{quote(hospital, safe="")}/candidate', body)


def _journal(event_id, grant_id, phase):
    with sqlite3.connect(db.DB_PATH, timeout=10) as conn:
        conn.execute('INSERT OR REPLACE INTO metaguard_attempts VALUES (?, ?, ?)', (event_id, grant_id, phase))


def _request(method, path, body=None):
    url, token = os.getenv('CONTROLLER_URL'), os.getenv('HOSPITAL_CONTROLLER_TOKEN')
    if not url or not token:
        raise RuntimeError('MetaGuard hospital configuration missing')
    headers = {'Authorization': 'Bearer ' + token}
    if admission_retry.enabled():
        headers['X-CrisisGuard-Retry-Policy'] = 'bounded-deferral'
    response = httpx.request(method, url.rstrip('/') + path, json=body, headers=headers, timeout=5)
    response.raise_for_status()
    return response.json()


def transmit(event, send):
    hospital = os.getenv('SITE_ID')
    if not hospital or hospital != event['site_id']:
        raise RuntimeError('MetaGuard hospital identity mismatch')
    event_id = event['event_id']
    digest = event_digest(event)
    with sqlite3.connect(db.DB_PATH) as conn:
        prior = conn.execute('SELECT grant_id, phase FROM metaguard_attempts WHERE event_id=?', (event_id,)).fetchone()
    if prior is None:
        _journal(event_id, None, 'registered')  # Durable before any request can commit.
    # Query parameters preserve arbitrary valid IDs, including '/' and '..',
    # without HTTP path normalization changing the immutable event identifier.
    lookup = f'/hospitals/{quote(hospital, safe="")}/grant?event_id={quote(event_id, safe="")}'
    grant = _request('GET', lookup)['grant']
    if prior and prior[1] != 'registered':
        # The latest grant may have changed while this durable ACK/send was
        # uncertain. Resolve the recorded grant, never infer its outcome from
        # the latest grant for the event.
        if not prior[0]:
            return {'status': 'awaiting_reconciliation'}
        recorded = _request('GET', f'/grants/by-id/{prior[0]}')
        if not recorded or not recorded.get('event_id'):
            return {'status': 'awaiting_reconciliation'}
        if (recorded.get('grant_id') != prior[0] or recorded.get('hospital_id') != hospital
                or recorded['event_id'] != event_id
                or recorded.get('event_digest') != digest):
            raise RuntimeError('Recorded grant does not match immutable event content')
        state = recorded['status']
        if state in ('issued', 'acknowledged', 'reconciliation_required'):
            result = _request('POST', f'/grants/{prior[0]}/reconcile', {'hospital_id': hospital})
            state = result.get('status')
            if state == 'invalid_state':
                # Another reconciler may have finalized it meanwhile.
                recorded = _request('GET', f'/grants/by-id/{prior[0]}')
                if (recorded.get('grant_id') != prior[0] or recorded.get('hospital_id') != hospital
                        or recorded.get('event_id') != event_id
                        or recorded.get('event_digest') != digest):
                    raise RuntimeError('Recorded grant changed during reconciliation')
                state = recorded['status']
        if state == 'completed':
            return {'status': 'applied', 'event_id': event_id}
        if state in ('expired', 'cancelled', 'superseded'):
            # The old Cloud write is fenced by the controller transaction.
            # A later replay may safely use a fresh grant.
            _journal(event_id, None, 'registered')
            return {'status': 'waiting_for_grant'}
        if state == 'rejected':
            return {'status': 'http_error', 'status_code': 422,
                    'error': 'Event attempt permanently rejected'}
        return {'status': 'awaiting_reconciliation'}
    if not grant or grant['status'] in ('expired', 'cancelled', 'superseded'):
        # Retired grants cannot have an accepted ACK. Clear only that fenced
        # local uncertainty; do not clear a journal merely because lookup failed.
        if grant and prior and prior[0] == grant['grant_id']:
            _journal(event_id, None, 'registered')
        candidate = advertise_candidate(event)
        if candidate.get('status') not in ('updated', 'issued'):
            return {'status': 'waiting_for_candidate', 'candidate': candidate}
        allocation = _request('POST', f'/hospitals/{quote(hospital, safe="")}/grants/next')
        grant = _request('GET', lookup)['grant']
        if not grant and admission_retry.enabled():
            proof = admission_retry.hint(allocation.get('admission_deferral'))
            if proof:
                return {'status': 'waiting_for_grant', 'admission_deferral': proof}
    if not grant:
        return {'status': 'waiting_for_grant'}
    if grant['event_digest'] != digest:
        raise RuntimeError('Stored grant does not match immutable event content')
    gid, state = grant['grant_id'], grant['status']
    if state == 'completed':
        return {'status': 'applied', 'event_id': event_id}
    if state == 'rejected':
        return {'status': 'http_error', 'status_code': 422, 'error': 'Event attempt permanently rejected'}
    if state in ('acknowledged', 'reconciliation_required') or (prior and prior[0] == gid and prior[1] in ('ack_pending', 'sent')):
        result = _request('POST', f'/grants/{gid}/reconcile', {'hospital_id': hospital})
        if result['status'] == 'completed':
            return {'status': 'applied', 'event_id': event_id}
        return {'status': 'awaiting_reconciliation'}
    if state != 'issued':
        return {'status': 'waiting_for_grant'}
    candidate = advertise_candidate(event)
    if candidate.get('status') not in ('updated', 'issued'):
        return {'status': 'waiting_for_candidate', 'candidate': candidate}
    _journal(event_id, gid, 'ack_pending')
    try:
        _request('POST', f'/grants/{gid}/acknowledge', {'hospital_id': hospital})
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 429:
            raise
        # A definite refusal did not ACK. Retain the issued grant and its place;
        # an ambiguous transport failure still uses authoritative reconciliation.
        _journal(event_id, gid, 'registered')
        if admission_retry.enabled():
            try:
                detail = error.response.json().get('detail')
                proof = admission_retry.hint({'reason': error.response.headers.get('X-CrisisGuard-Admission-Reason'),
                    'retry_after_seconds': int(error.response.headers.get('Retry-After', ''))})
            except (ValueError, TypeError, AttributeError):
                proof, detail = None, None
            if proof and detail == 'Replay admission deferred':
                return {'status': 'waiting_for_admission', 'admission_deferral': proof}
        return {'status': 'waiting_for_admission'}
    _journal(event_id, gid, 'sent')  # Crash from here never causes a second send.
    response = send(event, grant_id=gid)
    if isinstance(response, dict) and response.get('status_code') == 422:
        result = _request('POST', f'/grants/{gid}/abandon', {'hospital_id': hospital})
        if result['status'] == 'rejected':
            return response
    # Completion is checked against authoritative PostgreSQL, not our response.
    result = _request('POST', f'/grants/{gid}/complete', {'hospital_id': hospital})
    if result['status'] == 'completed':
        return {'status': 'applied', 'event_id': event_id}
    return {'status': 'awaiting_reconciliation', 'response': response}
