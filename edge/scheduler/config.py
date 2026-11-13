"""Validated process settings shared by replay and its read-only Inspector."""

import os
from dataclasses import dataclass

from edge.scheduler.validation import validate_ageing_interval, validate_enable_inheritance


@dataclass(frozen=True)
class SchedulerSettings:
    enable_inheritance: bool = True
    ageing_interval_seconds: float = 600.0
    selection_policy: str = "priority"

    def __post_init__(self):
        validate_enable_inheritance(self.enable_inheritance)
        validate_ageing_interval(self.ageing_interval_seconds)
        if self.selection_policy not in ("priority", "fifo"):
            raise ValueError("selection_policy must be priority or fifo")

    def as_dict(self):
        return {
            "inheritance_enabled": self.enable_inheritance,
            "ageing_interval_seconds": self.ageing_interval_seconds,
            "selection_policy": self.selection_policy,
        }


def load_scheduler_settings(environ=None):
    """Read settings without caching; a replay drain uses one settings snapshot.

    Environment changes require restarting deployed containers. Explicit mappings
    make configuration validation usable without mutating the process environment.
    """
    values = os.environ if environ is None else environ
    inheritance = values.get("SCHEDULER_ENABLE_INHERITANCE", "1")
    if not isinstance(inheritance, str) or inheritance.strip().lower() not in {
        "1", "0", "true", "false"
    }:
        raise ValueError("SCHEDULER_ENABLE_INHERITANCE must be 1, 0, true or false")

    interval = values.get("SCHEDULER_AGEING_INTERVAL_SECONDS", "600")
    if not isinstance(interval, str):
        raise ValueError("SCHEDULER_AGEING_INTERVAL_SECONDS must be positive and finite")
    try:
        interval = float(interval)
        validate_ageing_interval(interval)
    except (ValueError, OverflowError):
        raise ValueError(
            "SCHEDULER_AGEING_INTERVAL_SECONDS must be positive and finite"
        ) from None

    enabled = inheritance.strip().lower() in {"1", "true"}
    override = values.get("REPLAY_INHERITANCE")
    policy = values.get("REPLAY_SELECTION_POLICY", "priority")
    if ("REPLAY_INHERITANCE" in values and override not in ("0", "1")) or policy not in ("priority", "fifo"):
        raise ValueError("Invalid experimental replay policy")
    changes_inheritance = override is not None and (override == "1") != enabled
    if (override == "0" or changes_inheritance or policy != "priority") and values.get("CRISISGUARD_EXPERIMENTAL_POLICIES") != "1":
        raise ValueError("Experimental replay policies require explicit opt-in")
    if override is not None:
        enabled = override == "1"

    return SchedulerSettings(
        enable_inheritance=enabled,
        ageing_interval_seconds=interval,
        selection_policy=policy,
    )
