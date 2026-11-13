"""The AWS evidence checker must reject missing, altered and unhealthy state."""

import json

import pytest

from infrastructure.scripts.verify_aws_recovery import (
    check_containers, check_events, parse_containers,
)

EXPECTED = {"event_id": "TEAM_A_REG_001", "site_id": "Hospital_A", "event_type": "patient_registration",
            "base_priority": 1, "prerequisite_ids": [], "payload": {"patient_id": "SYNTHETIC_TEAM_A"}}


def original_record():
    return {**EXPECTED, "status": "applied"}


def test_compose_json_formats():
    rows = [{"Service": "patient"}, {"Service": "postgres"}]
    assert parse_containers(json.dumps(rows)) == rows
    assert parse_containers("\n".join(map(json.dumps, rows))) == rows
    assert parse_containers("") == []


@pytest.mark.parametrize("rows", [[], [{"Service": "patient", "State": "exited", "Health": "healthy"}], [{"Service": "patient", "State": "running", "Health": "unhealthy"}]])
def test_reject_missing_or_unhealthy_container(rows):
    with pytest.raises(RuntimeError):
        check_containers(rows, ["patient"])


def test_healthy_container():
    rows = [{"Service": "patient", "State": "running", "Health": "healthy"}]
    assert check_containers(rows, ["patient"]) == rows


def test_original_record_and_missing_duplicate_rejection():
    record = original_record()
    expected = [EXPECTED]
    assert check_events([record], expected)[0]["status"] == "applied"
    for rows in ([], [record, record]):
        with pytest.raises(RuntimeError):
            check_events(rows, expected)


@pytest.mark.parametrize("field,value", [("status", "pending"), ("site_id", "Hospital_B"), ("payload", {"patient_id": "changed"}), ("prerequisite_ids", ["unexpected"])])
def test_reject_altered_original_record(field, value):
    record = original_record()
    record[field] = value
    with pytest.raises(RuntimeError):
        check_events([record], [EXPECTED])
