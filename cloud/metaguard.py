"""Shared hospital identity, immutable event binding and Cloud grant fencing."""
import hashlib
import hmac
import json
import os
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException


def event_digest(event):
    def canonical(value):
        if value is None:
            return ['null']
        if isinstance(value, bool):
            return ['boolean', value]
        if isinstance(value, (int, float)):
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError('Non-finite event number')
            sign, digits, exponent = number.as_tuple()
            digits = list(digits)
            if not any(digits):
                return ['number', 0, [0], 0]
            while digits[-1] == 0:
                digits.pop()
                exponent += 1
            return ['number', sign, digits, exponent]
        if isinstance(value, str):
            return ['string', value]
        if isinstance(value, list):
            return ['array', [canonical(item) for item in value]]
        if isinstance(value, dict):
            return ['object', [[key, canonical(value[key])] for key in sorted(value)]]
        raise ValueError('Unsupported event content')

    fields = ('event_id', 'site_id', 'event_type', 'base_priority', 'prerequisite_ids', 'payload')
    # JSONB expands exponent notation and normalizes numeric scale. Use exact
    # decimal digits/exponent, without Decimal's rounding context or float casts.
    encoded = json.dumps(canonical({key: event[key] for key in fields}), sort_keys=True,
                         separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def hospital_identity(authorization):
    try:
        tokens = json.loads(os.getenv('CONTROLLER_HOSPITAL_TOKENS', '{}'))
        if (not isinstance(tokens, dict) or not tokens
                or any(not isinstance(k, str) or not isinstance(v, str) or len(v) < 32 for k, v in tokens.items())
                or len(set(tokens.values())) != len(tokens)
                or os.getenv('CONTROLLER_CLOUD_TOKEN') in tokens.values()):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(503, 'Hospital authentication is not configured') from None
    scheme, separator, supplied = (authorization or '').partition(' ')
    if not separator or scheme.lower() != 'bearer':
        raise HTTPException(401, 'Hospital credentials required')
    for hospital, token in tokens.items():
        if hmac.compare_digest(supplied.encode(), token.encode()):
            return hospital
    raise HTTPException(401, 'Hospital credentials required')


def cloud_persist(event, request, save):
    if os.getenv('METAGUARD_REQUIRED') != '1':
        return save(event)
    hospital = hospital_identity(request.headers.get('authorization'))
    if hospital != event['site_id']:
        raise HTTPException(403, 'Hospital identity mismatch')
    grant_id = request.headers.get('x-metaguard-grant')
    try:
        grant_id = str(UUID(grant_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(403, 'Valid MetaGuard grant required') from None
    try:
        event_digest(event)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise HTTPException(422, 'Invalid event content') from None
    return save(event, grant_id=grant_id)


def fence_cloud_write(cursor, event, grant_id):
    # Same lock/order as allocation and authoritative reconciliation. Held until
    # event application COMMIT: an absence decision cannot race a late write.
    cursor.execute('SELECT pg_advisory_xact_lock(3104, 9)')
    cursor.execute("""SELECT hospital_id, event_id, status, event_digest,
                      expires_at > clock_timestamp() FROM controller_grants
                      WHERE grant_id=%s FOR UPDATE""", (grant_id,))
    row = cursor.fetchone()
    if (row is None or row[0] != event['site_id'] or row[1] != event['event_id']
            or row[2] != 'acknowledged' or row[3] != event_digest(event) or not row[4]):
        raise HTTPException(409, 'Grant cannot authorize this event attempt')
