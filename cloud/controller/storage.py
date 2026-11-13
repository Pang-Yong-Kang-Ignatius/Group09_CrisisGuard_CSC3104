
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg import errors

from cloud.database.db import get_connection


class ControllerStorage:
    def __init__(self, bypass_limit=1, expiry_seconds=300):
        if bypass_limit < 0:
            raise ValueError("Invalid bypass limit")
        if expiry_seconds <= 0:
            raise ValueError("Expiry must be positive")

        self.bypass_limit = bypass_limit
        self.expiry_seconds = expiry_seconds

    def init_db(self):
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS controller_tickets (
                        hospital_id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        bypass_count INTEGER NOT NULL DEFAULT 0,
                        queue_position BIGINT NOT NULL
                    )
                """)

                cur.execute("""
                    CREATE SEQUENCE IF NOT EXISTS
                    controller_queue_position_seq
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS controller_grants (
                        grant_id UUID PRIMARY KEY,
                        hospital_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        expires_at TIMESTAMPTZ NOT NULL,
                        status TEXT NOT NULL DEFAULT 'issued',
                        UNIQUE (hospital_id, event_id)
                    )
                """)

                # Add lifecycle metadata without dropping existing rows.
                for column, definition in (
                    ("priority", "INTEGER"),
                    ("original_queue_position", "BIGINT"),
                    ("original_bypass_count", "INTEGER NOT NULL DEFAULT 0"),
                    ("acknowledged_at", "TIMESTAMPTZ"),
                    ("completed_at", "TIMESTAMPTZ"),
                    ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
                ):
                    cur.execute(
                        f"ALTER TABLE controller_grants ADD COLUMN IF NOT EXISTS {column} {definition}"
                    )
                cur.execute("ALTER TABLE controller_tickets ADD COLUMN IF NOT EXISTS event_digest TEXT")
                cur.execute("ALTER TABLE controller_grants ADD COLUMN IF NOT EXISTS event_digest TEXT")
                from cloud.controller.experimental import controller_settings
                if controller_settings()[0] == 'drr':
                    cur.execute("""CREATE TABLE IF NOT EXISTS controller_drr_state (
                        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
                        state JSONB NOT NULL)""")
                from cloud.controller.admission import create_schema
                create_schema(cur)
                # The old unique constraint prevented retries forever.
                # History is retained; only unresolved active attempts are unique.
                cur.execute("""
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'controller_grants'::regclass
                      AND contype = 'u'
                """)
                for (constraint,) in cur.fetchall():
                    # Only drop the original pair constraint (not unrelated constraints).
                    
                    cur.execute("""
                        SELECT array_agg(att.attname ORDER BY k.ordinality)
                        FROM pg_constraint AS c
                        CROSS JOIN LATERAL
                            unnest(c.conkey)
                            WITH ORDINALITY AS k(attnum, ordinality)
                        JOIN pg_attribute AS att
                            ON att.attrelid = c.conrelid
                            AND att.attnum = k.attnum
                        WHERE c.conrelid = 'controller_grants'::regclass
                          AND c.conname = %s
                    """, (constraint,))

                    if cur.fetchone()[0] == ['hospital_id', 'event_id']:
                        from psycopg import sql
                        cur.execute(sql.SQL("ALTER TABLE controller_grants DROP CONSTRAINT {}").format(sql.Identifier(constraint)))
                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS controller_active_event_uniq
                    ON controller_grants(hospital_id, event_id)
                    WHERE status IN ('issued', 'acknowledged', 'reconciliation_required')
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS controller_grants_expiry_idx
                    ON controller_grants(expires_at)
                    WHERE status IN ('issued', 'acknowledged')
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS controller_grant_audit (
                        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        grant_id UUID NOT NULL,
                        previous_status TEXT,
                        new_status TEXT NOT NULL,
                        action TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)

    def add_ticket(self, hospital_id, event_id, priority, event_digest=None):
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Serialize controller changes across connections.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(3104, 9)"
                )

                # Do not requeue an event already granted.
                cur.execute("""
                    SELECT 1 FROM controller_grants
                    WHERE hospital_id = %s AND event_id = %s
                      AND status NOT IN ('expired', 'cancelled', 'superseded')
                """, (hospital_id, event_id))

                if cur.fetchone():
                    return False

                # Do not replace a hospital's waiting ticket.
                cur.execute("""
                    INSERT INTO controller_tickets
                    (hospital_id, event_id, priority, event_digest, queue_position)
                    VALUES (
                        %s, %s, %s, %s,
                        nextval('controller_queue_position_seq')
                    )
                    ON CONFLICT (hospital_id) DO NOTHING
                    RETURNING hospital_id
                """, (hospital_id, event_id, priority, event_digest))

                return cur.fetchone() is not None

    def set_candidate(self, hospital_id, event_id=None, priority=None, event_digest=None):
        """Replace/withdraw unsent work atomically with grant ACK and allocation.

        Waiting tickets retain queue position and bypass count. Retired issued
        grants are terminal, not ordinary cancellations that expiry would requeue.
        """
        with get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self._lock(cur)
            cur.execute("""SELECT * FROM controller_grants WHERE hospital_id=%s
                AND status IN ('issued','acknowledged','reconciliation_required')
                ORDER BY expires_at, grant_id FOR UPDATE""", (hospital_id,))
            grants = cur.fetchall()
            for grant in grants:
                if grant['status'] != 'issued':
                    return {'status': 'pinned', 'event_id': grant['event_id']}
            if event_id is not None:
                cur.execute("""SELECT 1 FROM controller_grants WHERE hospital_id=%s
                    AND event_id=%s AND status IN ('completed','rejected')""",
                    (hospital_id, event_id))
                if cur.fetchone():
                    return {'status': 'finalized', 'event_id': event_id}
            same_grant = next((g for g in grants if g['event_id'] == event_id), None)
            bypass = 0
            for grant in grants:
                if grant is same_grant:
                    continue
                bypass = max(bypass, grant['original_bypass_count'])
                cur.execute("""UPDATE controller_grants SET status='superseded',
                    updated_at=NOW() WHERE grant_id=%s""", (grant['grant_id'],))
                self._audit(cur, grant['grant_id'], 'issued', 'superseded', 'candidate_change')
            # A cancelled/expired grant can remain eligible for the later
            # reconciliation sweep. Explicit replacement or withdrawal retires
            # that older intent, while ordinary cancellation still permits retry.
            cur.execute("""SELECT grant_id,status FROM controller_grants
                WHERE hospital_id=%s AND status IN ('cancelled','expired')
                  AND event_id IS DISTINCT FROM %s FOR UPDATE""", (hospital_id, event_id))
            for retired in cur.fetchall():
                cur.execute("""UPDATE controller_grants SET status='superseded',
                    updated_at=NOW() WHERE grant_id=%s""", (retired['grant_id'],))
                self._audit(cur, retired['grant_id'], retired['status'], 'superseded', 'candidate_change')
            if event_id is None or same_grant:
                if event_id is None:
                    from cloud.controller.experimental import controller_settings, withdraw_drr
                    if controller_settings()[0] == 'drr':
                        from psycopg.types.json import Jsonb
                        cur.execute('SELECT state FROM controller_drr_state WHERE singleton=TRUE FOR UPDATE')
                        row = cur.fetchone()
                        if row:
                            cur.execute('UPDATE controller_drr_state SET state=%s WHERE singleton=TRUE',
                                        (Jsonb(withdraw_drr(row['state'], hospital_id)),))
                cur.execute('DELETE FROM controller_tickets WHERE hospital_id=%s', (hospital_id,))
                return {'status': 'withdrawn' if event_id is None else 'issued', 'event_id': event_id}
            cur.execute("""INSERT INTO controller_tickets
                (hospital_id,event_id,priority,event_digest,bypass_count,queue_position)
                VALUES (%s,%s,%s,%s,%s,nextval('controller_queue_position_seq'))
                ON CONFLICT (hospital_id) DO UPDATE SET event_id=EXCLUDED.event_id,
                    priority=EXCLUDED.priority, event_digest=EXCLUDED.event_digest
                """, (hospital_id,event_id,priority,event_digest,bypass))
            return {'status': 'updated', 'event_id': event_id}

    def list_tickets(self):
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT hospital_id, event_id,
                           priority, bypass_count
                    FROM controller_tickets
                    ORDER BY queue_position
                """)
                return cur.fetchall()

    def update_priority(self, hospital_id, event_id, priority):
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(3104, 9)")
                cur.execute("""
                    UPDATE controller_tickets
                    SET priority = %s
                    WHERE hospital_id = %s AND event_id = %s
                """, (priority, hospital_id, event_id))
                return cur.rowcount == 1

    def issue_grant(self, admission_feedback=None):
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Only one controller allocation transaction
                # may run at a time.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(3104, 9)"
                )

                cur.execute("""
                    SELECT hospital_id, event_id, priority,
                           bypass_count, queue_position, event_digest
                    FROM controller_tickets
                    ORDER BY queue_position
                    FOR UPDATE
                """)

                tickets = cur.fetchall()
                if not tickets:
                    return None
                from cloud.controller import admission
                if admission.enabled() and not admission.reserve(cur, "issue", feedback=admission_feedback):
                    return None

                from cloud.controller.experimental import controller_settings, choose
                policy, _, costs, quantum = controller_settings()
                state = None
                if policy == 'drr':
                    cur.execute('SELECT state FROM controller_drr_state WHERE singleton=TRUE FOR UPDATE')
                    row = cur.fetchone()
                    state = row['state'] if row else None
                selected_index, state = choose(tickets, policy, self.bypass_limit, state, costs, quantum)
                if policy == 'drr':
                    from psycopg.types.json import Jsonb
                    cur.execute("""INSERT INTO controller_drr_state(singleton,state) VALUES(TRUE,%s)
                        ON CONFLICT(singleton) DO UPDATE SET state=EXCLUDED.state""", (Jsonb(state),))
                selected = tickets[selected_index]

                # Record bypasses of waiting tickets.
                for ticket in tickets[:selected_index]:
                    cur.execute("""
                        UPDATE controller_tickets
                        SET bypass_count = bypass_count + 1
                        WHERE hospital_id = %s
                    """, (ticket["hospital_id"],))

                # Remove the ticket receiving this grant.
                cur.execute("""
                    DELETE FROM controller_tickets
                    WHERE hospital_id = %s
                """, (selected["hospital_id"],))

                grant = {
                    "grant_id": str(uuid4()),
                    "hospital_id": selected["hospital_id"],
                    "event_id": selected["event_id"],
                    "expires_at": datetime.now(timezone.utc)
                    + timedelta(seconds=self.expiry_seconds)
                }

                cur.execute("""
                    INSERT INTO controller_grants
                    (grant_id, hospital_id, event_id, expires_at,
                     priority, original_queue_position, original_bypass_count, event_digest)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    grant["grant_id"],
                    grant["hospital_id"],
                    grant["event_id"],
                    grant["expires_at"], selected["priority"],
                    selected["queue_position"], selected["bypass_count"], selected["event_digest"]
                ))
                self._audit(cur, grant["grant_id"], None, "issued", "allocate")

                # Both the queue changes and grant are
                # committed when the connection context exits.
                return grant

    def get_grant(self, hospital_id, event_id):
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT grant_id, hospital_id,
                           event_id, expires_at
                    FROM controller_grants
                    WHERE hospital_id = %s
                      AND event_id = %s
                      AND status IN ('issued', 'acknowledged')
                      AND expires_at > NOW()
                """, (hospital_id, event_id))

                grant = cur.fetchone()
                if grant is None:
                    return None

                grant["grant_id"] = str(grant["grant_id"])
                grant["expires_at"] = grant["expires_at"].astimezone(timezone.utc)
                return grant


    @staticmethod
    def _audit(cur, grant_id, previous, current, action):
        cur.execute("""
            INSERT INTO controller_grant_audit
            (grant_id, previous_status, new_status, action, created_at)
            VALUES (%s, %s, %s, %s, clock_timestamp())
        """, (grant_id, previous, current, action))

    def _lock(self, cur):
        cur.execute("SELECT pg_advisory_xact_lock(3104, 9)")

    def get_grant_status(self, grant_id):
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT grant_id, hospital_id, event_id, expires_at,
                           status, acknowledged_at, completed_at, event_digest
                    FROM controller_grants WHERE grant_id = %s
                """, (grant_id,))
                result = cur.fetchone()
                if result:
                    result['grant_id'] = str(result['grant_id'])
                return result

    def acknowledge_grant(self, grant_id, hospital_id, admission_feedback=None):
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                self._lock(cur)
                cur.execute("SELECT * FROM controller_grants WHERE grant_id=%s FOR UPDATE", (grant_id,))
                grant = cur.fetchone()
                if grant is None or grant['hospital_id'] != hospital_id:
                    return 'not_found'
                if grant['status'] == 'acknowledged':
                    return 'acknowledged'
                if grant['status'] != 'issued':
                    return 'invalid_state'
                if grant['expires_at'] <= datetime.now(timezone.utc):
                    return 'expired'
                from cloud.controller import admission
                if admission.enabled() and not admission.reserve(cur, "ack", feedback=admission_feedback):
                    return "admission_deferred"
                cur.execute("""
                    UPDATE controller_grants SET status='acknowledged',
                    acknowledged_at=NOW(), updated_at=NOW() WHERE grant_id=%s
                """, (grant_id,))
                self._audit(cur, grant_id, 'issued', 'acknowledged', 'acknowledge')
                return 'acknowledged'

    def complete_grant(self, grant_id, hospital_id):
        # The caller MUST be authenticated as trusted Cloud application logic.
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                self._lock(cur)
                cur.execute("SELECT * FROM controller_grants WHERE grant_id=%s FOR UPDATE", (grant_id,))
                grant = cur.fetchone()
                if grant is None or grant['hospital_id'] != hospital_id:
                    return 'not_found'
                if grant['status'] == 'completed':
                    return 'completed'
                if grant['status'] not in ('acknowledged', 'reconciliation_required'):
                    return 'invalid_state'
                cur.execute("""
                    UPDATE controller_grants SET status='completed',
                    completed_at=NOW(), updated_at=NOW() WHERE grant_id=%s
                """, (grant_id,))
                self._audit(cur, grant_id, grant['status'], 'completed', 'cloud_applied')
                return 'completed'

    def cancel_grant(self, grant_id, hospital_id):
        # Only an unacknowledged grant can be cancelled without cloud reconciliation.
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                self._lock(cur)
                cur.execute("SELECT * FROM controller_grants WHERE grant_id=%s FOR UPDATE", (grant_id,))
                grant = cur.fetchone()
                if grant is None or grant['hospital_id'] != hospital_id:
                    return 'not_found'
                if grant['status'] == 'cancelled':
                    return 'cancelled'
                if grant['status'] != 'issued':
                    return 'invalid_state'
                cur.execute("UPDATE controller_grants SET status='cancelled', updated_at=NOW() WHERE grant_id=%s", (grant_id,))
                self._audit(cur, grant_id, 'issued', 'cancelled', 'cancel')
                self._requeue(cur, grant)
                return 'cancelled'

    def _requeue(self, cur, grant):
        # Preserve the hospital's current ticket if it has advertised newer work.
        # Reuse the prior bypass budget, but rejoin at the tail of the live queue.
        cur.execute("""
            INSERT INTO controller_tickets
              (hospital_id, event_id, priority, bypass_count, event_digest, queue_position)
            VALUES (%s, %s, %s, %s, %s, nextval('controller_queue_position_seq'))
            ON CONFLICT (hospital_id) DO NOTHING
            RETURNING hospital_id
        """, (grant['hospital_id'], grant['event_id'],
              grant['priority'] or 1, grant['original_bypass_count'], grant['event_digest']))
        return cur.fetchone() is not None

    def reconcile_expired(self):
        # One controller transaction decides expiry and retry eligibility.
        # Acknowledged work is UNCERTAIN and NEVER blindly requeued.
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                self._lock(cur)
                cur.execute("""
                    SELECT * FROM controller_grants
                    WHERE status IN ('issued', 'acknowledged') AND expires_at <= NOW()
                    ORDER BY expires_at, grant_id FOR UPDATE
                """)
                expired = cur.fetchall()
                report = {'expired': 0, 'requeued': 0, 'needs_reconciliation': 0}
                for grant in expired:
                    if grant['status'] == 'acknowledged':
                        state = 'reconciliation_required'
                        report['needs_reconciliation'] += 1
                    else:
                        state = 'expired'
                        report['expired'] += 1
                    cur.execute("UPDATE controller_grants SET status=%s, updated_at=NOW() WHERE grant_id=%s", (state, grant['grant_id']))
                    self._audit(cur, grant['grant_id'], grant['status'], state, 'expiry')
                    if state == 'expired' and self._requeue(cur, grant):
                        report['requeued'] += 1
                # Previously expired/cancelled grants may have lacked space
                # because their hospital already had a waiting ticket.
                cur.execute("""
                    SELECT g.* FROM controller_grants g
                    WHERE g.status IN ('expired', 'cancelled')
                    AND NOT EXISTS (
                        SELECT 1 FROM controller_tickets t
                        WHERE t.hospital_id=g.hospital_id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM controller_tickets t
                        WHERE t.hospital_id=g.hospital_id AND t.event_id=g.event_id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM controller_grants newer
                        WHERE newer.hospital_id=g.hospital_id AND newer.event_id=g.event_id
                          AND newer.status IN ('issued','acknowledged','completed','reconciliation_required','rejected')
                    )
                    ORDER BY g.expires_at
                """)
                seen = set()
                for grant in cur.fetchall():
                    if grant['hospital_id'] in seen:
                        continue
                    seen.add(grant['hospital_id'])
                    if self._requeue(cur, grant):
                        report['requeued'] += 1
                return report

    def resolve_uncertain(self, grant_id, applied):
        # Only trusted reconciler with authoritative cloud result may call.
        with get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                self._lock(cur)
                cur.execute("SELECT * FROM controller_grants WHERE grant_id=%s FOR UPDATE", (grant_id,))
                grant = cur.fetchone()
                if not grant or grant['status'] != 'reconciliation_required':
                    return 'invalid_state'
                state = 'completed' if applied else 'expired'
                cur.execute("UPDATE controller_grants SET status=%s, updated_at=NOW(), completed_at=CASE WHEN %s THEN NOW() ELSE completed_at END WHERE grant_id=%s", (state, applied, grant_id))
                self._audit(cur, grant_id, 'reconciliation_required', state, 'cloud_reconciliation')
                if not applied:
                    self._requeue(cur, grant)
                return state

    def latest_grant(self, hospital_id, event_id):
        with get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT grant_id::text, hospital_id, event_id, status, event_digest
                FROM controller_grants WHERE hospital_id=%s AND event_id=%s
                ORDER BY (status IN ('issued','acknowledged','reconciliation_required')) DESC,
                         updated_at DESC, expires_at DESC LIMIT 1""", (hospital_id, event_id))
            return cur.fetchone()

    def reconcile_authoritative(self, grant_id, hospital_id, *, complete_only=False, abandon=False):
        from cloud.metaguard import event_digest
        with get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self._lock(cur)
            cur.execute('SELECT * FROM controller_grants WHERE grant_id=%s FOR UPDATE', (grant_id,))
            grant = cur.fetchone()
            if not grant or grant['hospital_id'] != hospital_id:
                return 'not_found'
            if grant['status'] == 'completed':
                return 'completed'
            if grant['status'] not in ('issued', 'acknowledged', 'reconciliation_required'):
                return 'invalid_state'
            # Missing schema is not authoritative absence.
            cur.execute("SELECT to_regclass('events') AS relation")
            if not cur.fetchone()['relation'] or not grant['event_digest']:
                return 'unresolved'
            cur.execute('SELECT * FROM events WHERE event_id=%s', (grant['event_id'],))
            event = cur.fetchone()
            if event and (event['status'] != 'applied' or event_digest(event) != grant['event_digest']):
                return 'unresolved'  # Conflicting identity/content requires operator investigation.
            if not event and complete_only:
                return 'unresolved'
            state = 'completed' if event else ('rejected' if abandon else 'expired')
            cur.execute("""UPDATE controller_grants SET status=%s, updated_at=NOW(),
                completed_at=CASE WHEN %s THEN NOW() ELSE completed_at END WHERE grant_id=%s""",
                (state, bool(event), grant_id))
            self._audit(cur, grant_id, grant['status'], state, 'authoritative_cloud_query')
            if abandon and not event:
                cur.execute('DELETE FROM controller_tickets WHERE hospital_id=%s AND event_id=%s',
                            (hospital_id, grant['event_id']))
            elif not event:
                self._requeue(cur, grant)
            return state

    def reconcile_cloud_outcomes(self):
        self.reconcile_expired()
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT grant_id, hospital_id FROM controller_grants
                WHERE status='reconciliation_required' AND event_digest IS NOT NULL""")
            pending = cur.fetchall()
        for grant_id, hospital in pending:
            self.reconcile_authoritative(str(grant_id), hospital)

    def claim_admission_poll(self, config):
        from cloud.controller.admission import load
        with get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self._lock(cur)
            row, now = load(cur, config)
            if row['next_poll_at'] and row['next_poll_at'] > now:
                return None
            cur.execute("""UPDATE controller_admission SET next_poll_at=%s,
                poll_version=poll_version+1 WHERE singleton RETURNING poll_version""",
                (now+timedelta(seconds=config.poll_seconds),))
            return cur.fetchone()['poll_version']

    def update_admission_metrics(self, samples, version, config):
        from cloud.controller.admission import load, assess, advance
        from psycopg.types.json import Jsonb
        import json
        try:
            json.dumps(samples, allow_nan=False)
        except (ValueError, TypeError):
            samples = {'error': 'invalid_telemetry'}
        with get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self._lock(cur)
            row, now = load(cur, config)
            if row['poll_version'] != version:
                return  # A slow old HTTP request cannot replace a newer observation.
            tier, reason, healthy, observed = assess(samples, config, now)
            if observed and row['observed_at'] and observed <= row['observed_at']:
                tier, reason, healthy, observed = 2, 'telemetry_not_advancing', False, None
            row['reason'] = reason
            advance(row, tier, healthy, now, config)
            cur.execute("""UPDATE controller_admission SET state=%s, healthy_count=%s,
                state_since=%s, reason=%s, observed_at=%s, metrics=%s, rate_rps=%s, rate_healthy_count=%s WHERE singleton""",
                (row['state'], row['healthy_count'], row['state_since'], row['reason'], observed, Jsonb(samples), row['rate_rps'], row['rate_healthy_count']))

    def admission_status(self):
        from dataclasses import asdict
        from cloud.controller.admission import load, settings, enabled, effective_rate, policy, health_policy, telemetry_fresh
        config = settings()
        with get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            self._lock(cur)
            row, now = load(cur, config)
            state = row['state'].lower()
            return {'enabled': enabled(), 'state': row['state'], 'reason': row['reason'],
                    'state_since': row['state_since'], 'observed_at': row['observed_at'],
                    'server_time': now, 'healthy_samples': row['healthy_count'],
                    'rate_rps': effective_rate(row, config),
                    'policy': policy(), 'health_policy': health_policy(),
                    'telemetry_fresh': telemetry_fresh(row, now, config),
                    'inflight_limit': getattr(config, state + '_inflight'),
                    'next_issue_at': row['next_issue_at'], 'next_ack_at': row['next_ack_at'],
                    'allowed': row['allowed'], 'denied': row['denied'],
                    'last_decision': row['last_decision'], 'metrics': row['metrics'],
                    'settings': asdict(config)}
