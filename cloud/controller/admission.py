"""Global, durable admission pacing driven by the existing Cloud metrics."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
import math
import os

import httpx
from psycopg.types.json import Jsonb


SERVICES = ('patient', 'emergency', 'admin')


@dataclass(frozen=True)
class Settings:
    normal_rps: float = 2.0
    caution_rps: float = .5
    recovery_rps: float = .2
    normal_inflight: int = 4
    caution_inflight: int = 2
    recovery_inflight: int = 1
    caution_latency_ms: float = 250
    recovery_latency_ms: float = 1000
    healthy_latency_ms: float = 150
    caution_error_rate: float = .05
    recovery_error_rate: float = .2
    healthy_error_rate: float = .01
    caution_throughput_rps: float = 20
    recovery_throughput_rps: float = 50
    healthy_throughput_rps: float = 10
    poll_seconds: float = 5
    max_age_seconds: float = 15
    timeout_seconds: float = 2
    dwell_seconds: float = 15
    healthy_samples: int = 3
    additive_rps: float = .2

    def __post_init__(self):
        if any(not math.isfinite(v) or v <= 0 for v in asdict(self).values()):
            raise ValueError('Admission settings must be finite and positive')
        for name in ('rps', 'inflight'):
            if not getattr(self, 'recovery_' + name) <= getattr(self, 'caution_' + name) <= getattr(self, 'normal_' + name):
                raise ValueError('Admission rates/caps must increase toward Normal')
        for name in ('latency_ms', 'error_rate', 'throughput_rps'):
            if not getattr(self, 'healthy_' + name) < getattr(self, 'caution_' + name) < getattr(self, 'recovery_' + name):
                raise ValueError('Healthy thresholds must be below degradation thresholds')
        if self.recovery_error_rate > 1 or self.max_age_seconds <= self.poll_seconds + self.timeout_seconds:
            raise ValueError('Invalid error threshold or telemetry freshness interval')


def policy():
    value = os.getenv('CONTROLLER_ADMISSION_POLICY', 'fixed')
    if value not in ('fixed', 'aimd'):
        raise ValueError('Admission policy must be fixed or aimd')
    return value


def health_policy():
    value = os.getenv('CONTROLLER_ADMISSION_HEALTH_POLICY', 'throughput')
    if value not in ('throughput', 'pressure-experimental'):
        raise ValueError('Unknown admission health policy')
    if value != 'throughput' and os.getenv('CRISISGUARD_EXPERIMENTAL_POLICIES') != '1':
        raise ValueError('Experimental health policy requires explicit experimental opt-in')
    return value


def effective_rate(row, config):
    if policy() == 'fixed':
        return getattr(config, row['state'].lower() + '_rps')
    rate = row.get('rate_rps')
    if rate is None or not math.isfinite(rate):
        rate = config.recovery_rps
    return max(config.recovery_rps, min(config.normal_rps, rate))


def telemetry_fresh(row, now, config):
    stamp = row.get('observed_at')
    return stamp is not None and -2 <= (now-stamp).total_seconds() <= config.max_age_seconds


def settings():
    policy()  # Reject unknown policies at startup, before admitting work.
    health_policy()
    defaults = Settings()
    return Settings(**{field.name: field.type(os.getenv('CONTROLLER_ADMISSION_' + field.name.upper(),
                        str(getattr(defaults, field.name)))) for field in fields(defaults)})


def enabled():
    value = os.getenv('CONTROLLER_ADMISSION_ENABLED', '0')
    if value not in ('0', '1'):
        raise ValueError('CONTROLLER_ADMISSION_ENABLED must be 0 or 1')
    return value == '1'


def assess(samples, config, now):
    """Validate all services; one unavailable/stale source means Recovery."""
    experimental = health_policy() == 'pressure-experimental'
    earliest = now
    tier, reason, healthy = 0, 'healthy', True
    try:
        for name in SERVICES:
            sample = samples[name]
            stamp = datetime.fromisoformat(sample['sampled_at'])
            if stamp.tzinfo is None or not -2 <= (now-stamp).total_seconds() <= config.max_age_seconds:
                raise ValueError('stale')
            earliest = min(earliest, stamp)
            if sample['service'] != name or type(sample['database_healthy']) is not bool:
                raise ValueError('identity/health')
            for field in ('average_latency_ms', 'throughput_rps', 'error_rate', 'uptime_seconds', 'window_seconds'):
                value = sample[field]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError('invalid metric')
            if sample['window_seconds'] <= 0 or sample['error_rate'] > 1:
                raise ValueError('invalid window/error rate')
            for field in ('total_requests', 'failed_requests', 'window_requests'):
                if type(sample[field]) is not int or sample[field] < 0:
                    raise ValueError('invalid counters')
            if max(sample['failed_requests'], sample['window_requests']) > sample['total_requests']:
                raise ValueError('invalid counters')
            if not sample['database_healthy']:
                tier, reason, healthy = 2, name + ':database_unhealthy', False
            if experimental:
                from cloud.controller.pressure_health import signals
                measured, queue_stamp = signals(sample, config, now)
                earliest = min(earliest, queue_stamp)
            else:
                measured = [(field, sample[field], getattr(config, 'healthy_' + suffix),
                             getattr(config, 'caution_' + suffix), getattr(config, 'recovery_' + suffix))
                            for field, suffix in (('average_latency_ms', 'latency_ms'),
                                ('error_rate', 'error_rate'), ('throughput_rps', 'throughput_rps'))]
            for field, value, healthy_limit, caution_limit, recovery_limit in measured:
                severity = 2 if value >= recovery_limit else (1 if value >= caution_limit else 0)
                if severity > tier:
                    tier, reason = severity, name + ':' + field
                healthy &= value <= healthy_limit
        return tier, reason, healthy, earliest
    except (KeyError, ValueError, TypeError, OverflowError):
        return 2, 'telemetry_missing_invalid_or_stale', False, None


def advance(row, tier, healthy, now, config):
    rate = effective_rate(row, config)
    votes = row.get('rate_healthy_count', 0)
    if policy() == 'aimd':
        if tier > 0:
            rate, votes = max(config.recovery_rps, rate / 2), 0
        elif healthy:
            votes += 1
            if votes >= config.healthy_samples:
                rate, votes = min(config.normal_rps, rate + config.additive_rps), 0
        else:
            votes = 0
    else:
        votes = 0
    state, count = row['state'], row['healthy_count']
    reason = row['reason']
    if tier == 2:
        state, count = 'Recovery', 0
    elif tier == 1 and state == 'Normal':
        state, count = 'Caution', 0
    elif healthy and state != 'Normal':
        count += 1
        if count >= config.healthy_samples and (now-row['state_since']).total_seconds() >= config.dwell_seconds:
            state, count = ('Caution' if state == 'Recovery' else 'Normal'), 0
    else:
        count = 0
    if state != row['state']:
        row['state_since'] = now
    row.update(state=state, healthy_count=count, reason=reason,
               rate_rps=rate if policy() == 'aimd' else getattr(config, state.lower()+'_rps'),
               rate_healthy_count=votes)
    return row


def create_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS controller_admission (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        state TEXT NOT NULL DEFAULT 'Recovery', healthy_count INTEGER NOT NULL DEFAULT 0,
        state_since TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        reason TEXT NOT NULL DEFAULT 'cold_start', observed_at TIMESTAMPTZ,
        metrics JSONB NOT NULL DEFAULT '{}', next_poll_at TIMESTAMPTZ,
        poll_version BIGINT NOT NULL DEFAULT 0,
        last_issue_at TIMESTAMPTZ, next_issue_at TIMESTAMPTZ,
        last_ack_at TIMESTAMPTZ, next_ack_at TIMESTAMPTZ,
        allowed BIGINT NOT NULL DEFAULT 0, denied BIGINT NOT NULL DEFAULT 0,
        last_decision JSONB NOT NULL DEFAULT '{}')""")
    cur.execute('ALTER TABLE controller_admission ADD COLUMN IF NOT EXISTS rate_rps DOUBLE PRECISION')
    cur.execute('ALTER TABLE controller_admission ADD COLUMN IF NOT EXISTS rate_healthy_count INTEGER NOT NULL DEFAULT 0')
    cur.execute('INSERT INTO controller_admission (singleton) VALUES (TRUE) ON CONFLICT DO NOTHING')


def load(cur, config):
    cur.execute('SELECT * FROM controller_admission WHERE singleton FOR UPDATE')
    row = cur.fetchone()
    cur.execute('SELECT clock_timestamp() AS now')
    now = cur.fetchone()['now']
    if not telemetry_fresh(row, now, config):
        if row['state'] != 'Recovery':
            row['state_since'] = now
        row.update(state='Recovery', healthy_count=0, rate_healthy_count=0, reason='telemetry_missing_or_stale')
        cur.execute("""UPDATE controller_admission SET state='Recovery', healthy_count=0, rate_healthy_count=0,
                    state_since=%s, reason=%s WHERE singleton""", (row['state_since'], row['reason']))
    return row, now


def reserve(cur, phase, feedback=None):
    """Called inside existing allocation/ACK transaction and advisory lock."""
    config = settings()
    row, now = load(cur, config)
    state = row['state'].lower()
    rate, cap = effective_rate(row, config), getattr(config, state + '_inflight')
    statuses = ('issued', 'acknowledged', 'reconciliation_required') if phase == 'issue' else ('acknowledged', 'reconciliation_required')
    cur.execute('SELECT COUNT(*) AS count FROM controller_grants WHERE status = ANY(%s)', (list(statuses),))
    inflight = cur.fetchone()['count']
    last, deadline = row['last_' + phase + '_at'], row['next_' + phase + '_at']
    if last:
        deadline = max(deadline or last, last + timedelta(seconds=1/rate))
    fresh = telemetry_fresh(row, now, config)
    allowed = fresh and inflight < cap and (deadline is None or now >= deadline)
    reason = 'telemetry_unavailable' if not fresh else ('admitted' if allowed else ('inflight_limit' if inflight >= cap else 'rate_limit'))
    decision = {'phase': phase, 'allowed': allowed, 'reason': reason, 'state': row['state'],
                'at': now.isoformat(), 'rate_rps': rate, 'inflight': inflight, 'inflight_limit': cap}
    if feedback is not None and not allowed and fresh:
        # Advisory only: every subsequent request still passes reserve(). Bound
        # polling to at most once/second even when the pacing deadline is near.
        feedback.update(reason=reason, retry_after_seconds=max(1, min(30,
            math.ceil((deadline-now).total_seconds()) if deadline else 1)))
    if allowed:
        # Phase is an internal constant, never a request parameter.
        cur.execute(f"""UPDATE controller_admission SET last_{phase}_at=%s, next_{phase}_at=%s,
                    allowed=allowed+1, last_decision=%s WHERE singleton""",
                    (now, now+timedelta(seconds=1/rate), Jsonb(decision)))
    else:
        cur.execute('UPDATE controller_admission SET denied=denied+1, last_decision=%s WHERE singleton', (Jsonb(decision),))
    return allowed


def poll(storage):
    config = settings()
    version = storage.claim_admission_poll(config)
    if version is None:
        return
    def fetch(name):
        url = os.getenv('CONTROLLER_' + name.upper() + '_METRICS_URL', f'http://{name}:8000/metrics')
        try:
            response = httpx.get(url, timeout=config.timeout_seconds)
            response.raise_for_status()
            return name, response.json()
        except (httpx.HTTPError, ValueError):
            return name, {'error': 'telemetry_unavailable'}
    with ThreadPoolExecutor(max_workers=3) as pool:
        samples = dict(pool.map(fetch, SERVICES))
    storage.update_admission_metrics(samples, version, config)
