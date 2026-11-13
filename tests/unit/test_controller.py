
import pytest
from cloud.controller.scheduler import ControllerScheduler


def test_round_robin():
    s = ControllerScheduler(bypass_limit=0)
    s.add_hospital("A", "A001", 1)
    s.add_hospital("B", "B001", 5)

    assert s.next_hospital().hospital_id == "A"
    assert s.next_hospital().hospital_id == "B"


def test_empty_queue():
    s = ControllerScheduler()
    assert s.next_hospital() is None


def test_no_duplicate_hospitals():
    s = ControllerScheduler()
    s.add_hospital("A", "A001", 1)
    s.add_hospital("A", "A002", 5)

    assert len(s.queue) == 1
    assert s.queue[0].event_id == "A001"


def test_single_hospital():
    s = ControllerScheduler()
    s.add_hospital("A", "A001", 1)

    assert s.next_hospital().hospital_id == "A"
    assert s.next_hospital() is None


def test_ticket_information():
    s = ControllerScheduler()
    s.add_hospital("A", "A001", 3)

    ticket = s.queue[0]
    assert ticket.hospital_id == "A"
    assert ticket.event_id == "A001"
    assert ticket.priority == 3
    assert ticket.bypass_count == 0


def test_urgent_hospital_bypasses():
    s = ControllerScheduler(bypass_limit=1)
    s.add_hospital("A", "A001", 1)
    s.add_hospital("B", "B001", 5)

    assert s.next_hospital().hospital_id == "B"
    assert s.queue[0].bypass_count == 1
    assert s.next_hospital().hospital_id == "A"


def test_zero_bypass_equals_round_robin():
    s = ControllerScheduler(bypass_limit=0)
    s.add_hospital("A", "A001", 1)
    s.add_hospital("B", "B001", 5)

    assert s.next_hospital().hospital_id == "A"


def test_bypass_count_increases():
    s = ControllerScheduler(bypass_limit=2)
    s.add_hospital("A", "A001", 1)
    s.add_hospital("B", "B001", 5)

    s.next_hospital()
    assert s.queue[0].bypass_count == 1


def test_invalid_bypass_limit():
    with pytest.raises(ValueError):
        ControllerScheduler(bypass_limit=-1)


def test_highest_priority_selected():
    s = ControllerScheduler(bypass_limit=1)
    s.add_hospital("A", "A001", 1)
    s.add_hospital("B", "B001", 4)
    s.add_hospital("C", "C001", 5)

    assert s.next_hospital().hospital_id == "C"
    assert s.queue[0].bypass_count == 1
    assert s.queue[1].bypass_count == 1


def test_blocked_bypass_serves_front():
    s = ControllerScheduler(bypass_limit=1)
    s.add_hospital("A", "A001", 1)
    s.add_hospital("B", "B001", 4)
    s.add_hospital("C", "C001", 5)

    # A has already been bypassed once.
    s.queue[0].bypass_count = 1

    # C has highest priority but cannot bypass A.
    assert s.next_hospital().hospital_id == "A"


def test_priority_update_preserves_position():
    s = ControllerScheduler(bypass_limit=2)
    s.add_hospital("A", "A001", 1)
    s.add_hospital("B", "B001", 3)

    s.queue[0].bypass_count = 1
    assert s.update_priority("A", 10) is True

    assert s.queue[0].hospital_id == "A"
    assert s.queue[0].priority == 10
    assert s.queue[0].bypass_count == 1


def test_new_ticket_after_grant():
    s = ControllerScheduler(bypass_limit=1)
    s.add_hospital("A", "A001", 1)

    assert s.next_hospital().event_id == "A001"
    assert s.next_hospital() is None

    # A has another eligible event.
    s.add_hospital("A", "A002", 3)

    assert s.next_hospital().event_id == "A002"


@pytest.mark.parametrize("hospital", ["Hospital_A", "Hospital_B"])
def test_each_hospital_independently(hospital):
    s = ControllerScheduler()
    s.add_hospital(hospital, "E1", 1)
    assert s.next_hospital().hospital_id == hospital
    assert s.next_hospital() is None


@pytest.mark.parametrize("limit", [0, 1, 2, 5])
def test_continuous_urgent_arrivals_cannot_starve_head(limit):
    s = ControllerScheduler(bypass_limit=limit)
    s.add_hospital("Hospital_A", "A1", 1)
    for index in range(limit + 1):
        s.add_hospital("Hospital_B", f"B{index}", 5)
        selected = s.next_hospital()
        if index < limit:
            assert selected.hospital_id == "Hospital_B"
            assert s.queue[0].bypass_count == index + 1
        else:
            assert selected.hospital_id == "Hospital_A"


def test_ties_preserve_arrival_order():
    s = ControllerScheduler(bypass_limit=5)
    for hospital in ["A", "B", "C"]:
        s.add_hospital(hospital, hospital + "1", 5)
    assert [s.next_hospital().hospital_id for _ in range(3)] == ["A", "B", "C"]


def test_round_robin_rejoining_hospital_goes_to_tail():
    s = ControllerScheduler(bypass_limit=0)
    s.add_hospital("A", "A1", 1)
    s.add_hospital("B", "B1", 5)
    assert s.next_hospital().hospital_id == "A"
    s.add_hospital("A", "A2", 5)
    assert s.next_hospital().hospital_id == "B"
    assert s.next_hospital().event_id == "A2"
