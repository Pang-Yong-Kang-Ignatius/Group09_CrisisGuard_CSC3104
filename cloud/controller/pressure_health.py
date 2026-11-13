"""Opt-in health hypothesis; not a calibrated production capacity profile."""
from datetime import datetime
import math


def signals(sample, config, now):
    """Validate measured tails/queue; empty populations stay null, never zeroed."""
    def number(key):
        value = sample[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid pressure metric')
        return value

    stamp = datetime.fromisoformat(sample['executor_queue_sampled_at'])
    if stamp.tzinfo is None or not -2 <= (now-stamp).total_seconds() <= config.max_age_seconds:
        raise ValueError('stale queue')
    depth, limit = number('executor_queue_depth'), number('executor_worker_limit')
    pressure = number('executor_queue_pressure')
    if type(depth) is not int or limit <= 0 or not math.isclose(pressure, depth/limit):
        raise ValueError('inconsistent queue')
    result = [('average_latency_ms', sample['average_latency_ms'], config.healthy_latency_ms,
               config.caution_latency_ms, config.recovery_latency_ms),
              ('error_rate', sample['error_rate'], config.healthy_error_rate,
               config.caution_error_rate, config.recovery_error_rate)]
    for prefix in ('', 'live_'):
        count_key = 'live_latency_sample_count' if prefix else 'latency_sample_count'
        count = sample[count_key]
        if type(count) is not int or not 0 <= count <= sample['window_requests']:
            raise ValueError('invalid tail population')
        if not prefix and count != sample['window_requests']:
            raise ValueError('inconsistent tail population')
        key = prefix + 'p95_latency_ms'
        if count:
            result.append((key, number(key), config.healthy_latency_ms,
                           config.caution_latency_ms, config.recovery_latency_ms))
        elif sample[key] is not None:
            raise ValueError('empty tail must be null')
    number('live_slo_threshold_ms')
    if sample['live_slo_threshold_ms'] <= 0:
        raise ValueError('SLO must be configured')
    count, violations = sample['live_latency_sample_count'], sample['live_slo_violation_count']
    if type(violations) is not int or not 0 <= violations <= count:
        raise ValueError('invalid SLO population')
    if count:
        rate = number('live_slo_violation_rate')
        # Telemetry rounds this ratio to four decimals.
        if rate > 1 or not math.isclose(rate, violations/count, abs_tol=.000051):
            raise ValueError('inconsistent SLO ratio')
        result.append(('live_slo_violation_rate', rate, config.healthy_error_rate,
                       config.caution_error_rate, config.recovery_error_rate))
    elif sample['live_slo_violation_rate'] is not None:
        raise ValueError('empty SLO must be null')
    # Any waiting work prevents recovery votes; a full worker-pool of waiting
    # work triggers Recovery. Pressure can legitimately exceed one.
    result.append(('executor_queue_pressure', pressure, 0, 1/limit, 1))
    return result, stamp
