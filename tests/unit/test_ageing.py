from datetime import datetime, timedelta, timezone
from edge.scheduler.priority import calculate_aged_priority
import pytest

def test_new_event():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    event = {
        "base_priority": 2,
        "created_at": now.isoformat()
    }

    assert calculate_aged_priority(event, now) == 2

def test_older_event():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    event = {
        "base_priority": 2,
        "created_at": (
            now - timedelta(minutes=30)
        ).isoformat()
    }

    assert calculate_aged_priority(event, now) == 5

def test_missing_timestamp():
    event = {
        "base_priority": 3,
        "created_at": None
    }

    assert calculate_aged_priority(event) == 3

def test_future_timestamp():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    event = {
        "base_priority": 2,
        "created_at": (
            now + timedelta(minutes=10)
        ).isoformat()
    }

    assert calculate_aged_priority(event, now) == 2

def test_ageing_interval_boundaries():
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)

    waiting_times = [
        (599, 2),
        (600, 3),
        (1199, 3),
        (1200, 4)
    ]

    for waiting_seconds, expected_priority in waiting_times:
        event = {
            "base_priority": 2,
            "created_at": (
                now - timedelta(seconds=waiting_seconds)
            ).isoformat()
        }

        result = calculate_aged_priority(
            event,
            now=now,
            ageing_interval_seconds=600
        )

        assert result == expected_priority

def test_invalid_ageing_interval_rejected():
    event = {
        "base_priority": 2,
        "created_at": "2026-10-15T10:00:00+00:00"
    }

    for interval in [0, -1, -600]:
        with pytest.raises(ValueError):
            calculate_aged_priority(
                event,
                ageing_interval_seconds=interval
            )

def test_timezone_naive_timestamp_rejected():
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)

    event = {
        "base_priority": 2,
        "created_at": "2026-10-15T10:00:00"
    }

    with pytest.raises(ValueError, match="timezone"):
        calculate_aged_priority(
            event,
            now=now
        )
