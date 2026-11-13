"""Safety and measurement-contract checks; no network or Docker required."""

import asyncio

import httpx
import pytest

from evaluation.experiments.calibrate_live_reads import (
    Criteria, local_url, nearest_rank_p95, run_step_async, select_capacity,
)


@pytest.mark.parametrize("url", [
    "https://localhost:18101/events/A", "http://example.com:18101/events/A",
    "http://127.0.0.1:18101/health", "http://localhost:18101/events/",
    "http://localhost:18101/events/A?secret=x", "http://user:pass@localhost:18101/events/A",
])
def test_rejects_nonlocal_or_nonrecord_endpoints(url):
    with pytest.raises(ValueError):
        local_url(url)


def test_accepts_known_local_event_url():
    assert local_url("http://127.0.0.1:18101/events/KNOWN_A") == "http://127.0.0.1:18101/events/KNOWN_A"


def test_nearest_rank_p95_no_samples_and_exact_rank():
    assert nearest_rank_p95([]) is None
    assert nearest_rank_p95(list(range(1, 21))) == 19


def result(rate, offered=100, good=100, failed=0, p95=50):
    return {"offered_rps": rate, "offered": offered, "completed_2xx": good,
            "failed": failed, "p95_client_ms": p95}


def test_capacity_selection_requires_validated_rate():
    criteria = Criteria(slo_ms=100)
    steps = [result(10), result(20, good=94, failed=6), result(40, p95=150)]
    assert [criteria.accepts(step) for step in steps] == [True, False, False]
    targets = select_capacity(steps, criteria)
    assert targets["capacity_rps"] == 10
    assert targets["load_30_rps"] == 3
    assert targets["load_70_rps"] == 7


def test_no_passing_capacity_never_invents_percentages():
    targets = select_capacity([result(10, good=0, failed=100, p95=None)], Criteria())
    assert targets["capacity_rps"] is None
    assert targets["load_30_rps"] is None
    assert targets["load_70_rps"] is None


def test_generator_skips_count_as_failed_delivery():
    assert not Criteria().accepts(result(50, good=80, failed=20))


def test_generator_cap_skips_disqualify_capacity_even_if_delivery_ratio_is_high():
    sample = result(100, offered=100, good=99, failed=1)
    sample["generator_cap_skips"] = 1
    assert not Criteria().accepts(sample)


def test_async_generator_completes_paced_reads_without_network():
    async def respond(request):
        assert request.headers["X-CrisisGuard-Traffic"] == "live"
        return httpx.Response(200, json={"status": "applied"})

    sample = asyncio.run(run_step_async(
        "http://localhost:18101/events/KNOWN_A", {"X-CrisisGuard-Traffic": "live"},
        20, .2, 4, 1, transport=httpx.MockTransport(respond)))
    assert sample["offered"] == 4
    assert sample["completed_2xx"] == 4
    assert sample["generator_cap_skips"] == 0
    assert sample["p95_start_lag_ms"] is not None


def test_async_generator_distinguishes_client_saturation_from_server_errors():
    async def slow_read(request):
        await asyncio.sleep(.08)
        return httpx.Response(200)

    sample = asyncio.run(run_step_async(
        "http://localhost:18101/events/KNOWN_A", {},
        100, .1, 1, 1, transport=httpx.MockTransport(slow_read)))
    assert sample["generator_cap_skips"] > 0
    assert sample["status_counts"].get("500", 0) == 0
    assert not Criteria().accepts(sample)
