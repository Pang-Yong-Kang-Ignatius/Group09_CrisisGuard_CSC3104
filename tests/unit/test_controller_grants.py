
from cloud.controller.scheduler import ControllerScheduler
from cloud.controller.grants import GrantManager
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import pytest


def make_manager(expiry_seconds=30):
    scheduler = ControllerScheduler(bypass_limit=1)
    return GrantManager(scheduler, expiry_seconds)


def test_grant_creation():
    manager = make_manager()
    manager.scheduler.add_hospital("A", "A001", 1)

    grant = manager.issue_grant()

    assert grant.hospital_id == "A"
    assert grant.event_id == "A001"
    assert grant.grant_id is not None


def test_grant_retrieval():
    manager = make_manager()
    manager.scheduler.add_hospital("A", "A001", 1)

    original = manager.issue_grant()
    retrieved = manager.get_grant("A", "A001")

    assert retrieved.grant_id == original.grant_id


def test_no_duplicate_allocation():
    manager = make_manager()
    manager.scheduler.add_hospital("A", "A001", 1)

    first = manager.issue_grant()
    second = manager.issue_grant()

    assert first is not None
    assert second is None


def test_expired_grant():
    manager = make_manager()
    manager.scheduler.add_hospital("A", "A001", 1)

    grant = manager.issue_grant()
    grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert manager.get_grant("A", "A001") is None


def test_unique_grant_ids():
    manager = make_manager()

    manager.scheduler.add_hospital("A", "A001", 1)
    manager.scheduler.add_hospital("B", "B001", 5)

    first = manager.issue_grant()
    second = manager.issue_grant()

    assert first.grant_id != second.grant_id


@pytest.mark.parametrize("expiry", [0, -1])
def test_invalid_expiry(expiry):
    with pytest.raises(ValueError):
        make_manager(expiry)


def test_requeued_event_cannot_overwrite_grant():
    manager = make_manager()
    manager.scheduler.add_hospital("A", "A001", 1)
    original = manager.issue_grant()
    manager.scheduler.add_hospital("A", "A001", 5)
    assert manager.issue_grant() is None
    assert manager.get_grant("A", "A001") == original
    assert manager.get_grant("B", "A001") is None


def test_concurrent_allocations():
    manager = make_manager()
    manager.scheduler.add_hospital("A", "A001", 1)
    manager.scheduler.add_hospital("B", "B001", 5)
    with ThreadPoolExecutor(max_workers=8) as pool:
        grants = list(pool.map(lambda _: manager.issue_grant(), range(16)))
    issued = [grant for grant in grants if grant is not None]
    assert {(g.hospital_id, g.event_id) for g in issued} == {("A", "A001"), ("B", "B001")}
    assert len({g.grant_id for g in issued}) == 2
