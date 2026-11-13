
"""
Input validation for dependency scheduler event records
and scheduler arguments.

Checks input structure before scheduling.
Does not assign priorities or resolve dependencies.
"""

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from math import isfinite


REQUIRED_FIELDS = (
    "event_id",
    "base_priority",
    "prerequisite_ids",
    "status",
)


def validate_timestamp(value, field_name):
    """
    Validate a timezone-aware timestamp.

    Accepts:
    - None
    - Timezone-aware datetime objects
    - ISO 8601 strings with timezone information
    """

    if value is None:
        return

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(
                f"{field_name} must be a valid "
                "ISO 8601 timestamp"
            ) from exc

    if not isinstance(value, datetime):
        raise ValueError(
            f"{field_name} must be a timezone-aware "
            "datetime or ISO 8601 string, or None"
        )

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{field_name} must include timezone information"
        )


def validate_prerequisite_ids(prerequisites, event_id=None):
    """Validate structure without resolving dependencies or changing IDs.

    Shared by capture and scheduling so malformed lists cannot enter the queue.
    Missing prerequisites and dependency cycles remain valid diagnostic inputs.
    """
    if not isinstance(prerequisites, list):
        raise ValueError("prerequisite_ids must be a list")

    for prerequisite in prerequisites:
        if not isinstance(prerequisite, str) or not prerequisite.strip():
            raise ValueError("Each prerequisite ID must be a nonempty string")

    if len(prerequisites) != len(set(prerequisites)):
        context = f" in event {event_id}" if event_id is not None else ""
        raise ValueError(f"Duplicate prerequisite ID{context}")


def validate_event_record(event):
    """Validate the required structure of one event."""

    if not isinstance(event, dict):
        raise ValueError(
            "Event must be a dictionary"
        )

    for field in REQUIRED_FIELDS:
        if field not in event:
            raise ValueError(
                f"Missing required event field: {field}"
            )

    # Validate event ID.
    event_id = event["event_id"]

    if (
        not isinstance(event_id, str)
        or not event_id.strip()
    ):
        raise ValueError(
            "event_id must be a nonempty string"
        )

    # Validate the base priority type.
    # Reject booleans because bool is a subclass of int.
    base_priority = event["base_priority"]

    if (
        not isinstance(base_priority, int)
        or isinstance(base_priority, bool)
    ):
        raise ValueError(
            "base_priority must be an integer"
        )

    # Validate event status.
    # Quarantine is tracked separately in recovery state.
    valid_statuses = {"pending", "applied"}
    status = event["status"]

    if (
        not isinstance(status, str)
        or status not in valid_statuses
    ):
        raise ValueError(
            "status must be 'pending' or 'applied'"
        )

    validate_prerequisite_ids(event["prerequisite_ids"], event_id=event_id)

    # Validate optional creation timestamp.
    # Missing timestamps are allowed by the scheduler.
    validate_timestamp(
        event.get("created_at"),
        "created_at"
    )


def validate_event_collection(events):
    """Require an iterable of event records, rather than text or a mapping.

    Lists, tuples, generators, and empty iterables are valid containers.
    Each record is checked separately by validate_event_record().
    """
    if (
        not isinstance(events, Iterable)
        or isinstance(events, (str, bytes, bytearray, memoryview, Mapping))
    ):
        raise ValueError(
            "events must be an iterable of event records"
        )


def validate_event_records(events):
    """Validate a collection of scheduler events."""

    validate_event_collection(events)

    for event in events:
        validate_event_record(event)


def validate_ageing_interval(ageing_interval_seconds):
    """
    Require a positive, finite numeric ageing interval.
    """

    if (
        isinstance(ageing_interval_seconds, bool)
        or not isinstance(ageing_interval_seconds, (int, float))
        or not isfinite(ageing_interval_seconds)
        or ageing_interval_seconds <= 0
    ):
        raise ValueError(
            "Ageing interval must be positive, "
            "finite, and numeric"
        )


def validate_event_id_collection(
    value,
    field_name,
    allow_none=False
):
    """Validate a collection of event IDs."""

    if value is None and allow_none:
        return

    if (
        not isinstance(value, Collection)
        or isinstance(value, (str, bytes, Mapping))
    ):
        raise ValueError(
            f"{field_name} must be a collection of event IDs"
        )

    for event_id in value:
        if (
            not isinstance(event_id, str)
            or not event_id.strip()
        ):
            raise ValueError(
                f"{field_name} must contain nonempty string IDs"
            )


def validate_evaluation_time(now):
    """Require a timezone-aware evaluation datetime, or None for current time."""
    if now is not None:
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("now must be a timezone-aware datetime")


def validate_scheduler_arguments(
    *,
    applied_ids,
    now=None,
    in_flight_ids=None,
    retry_after=None,
    permanently_blocked_ids=None
):
    """
    Validate arguments shared by both scheduler implementations.
    """

    # Validate applied event IDs.
    validate_event_id_collection(
        applied_ids,
        "applied_ids"
    )

    # Validate temporarily unavailable event IDs.
    validate_event_id_collection(
        in_flight_ids,
        "in_flight_ids",
        allow_none=True
    )

    # Validate permanently blocked event IDs.
    validate_event_id_collection(
        permanently_blocked_ids,
        "permanently_blocked_ids",
        allow_none=True
    )

    # Validate current evaluation timestamp.
    validate_evaluation_time(now)

    # Validate retry deadlines.
    # Expected format:
    # {event_id: timezone_aware_datetime_or_None}
    if retry_after is not None:
        if not isinstance(retry_after, Mapping):
            raise ValueError(
                "retry_after must be a mapping"
            )

        for event_id, deadline in retry_after.items():

            # Validate retry event ID.
            if (
                not isinstance(event_id, str)
                or not event_id.strip()
            ):
                raise ValueError(
                    "retry_after keys must be nonempty string IDs"
                )

            # None means no retry restriction.
            if deadline is None:
                continue

            # Retry deadlines must be timezone-aware
            # Python datetime objects.
            if (
                not isinstance(deadline, datetime)
                or deadline.tzinfo is None
                or deadline.utcoffset() is None
            ):
                raise ValueError(
                    "retry_after deadlines must be "
                    "timezone-aware datetime objects or None"
                )


def validate_enable_inheritance(enable_inheritance):
    """Require an explicit boolean inheritance policy."""

    if not isinstance(enable_inheritance, bool):
        raise ValueError(
            "enable_inheritance must be a boolean"
        )
