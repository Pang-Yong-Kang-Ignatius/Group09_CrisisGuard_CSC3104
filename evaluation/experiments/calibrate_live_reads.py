"""Calibrate *read-only* live load against a known Cloud event on localhost.

This does not establish live-write capacity or the proposal's full D2/D3 matrix.
It deliberately leaves Cloud, MetaGuard and scheduler algorithms unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlparse

import httpx


TRAFFIC_HEADER = "X-CrisisGuard-Traffic"
TOKEN_HEADER = "X-CrisisGuard-Telemetry-Token"


def local_url(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or not parsed.port or not parsed.path.startswith("/events/")
            or not parsed.path.removeprefix("/events/").strip("/")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Use a localhost HTTP URL for an existing /events/{id} resource")
    return value


def nearest_rank_p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(.95 * len(ordered)) - 1]


@dataclass(frozen=True)
class Criteria:
    minimum_delivery_ratio: float = .99
    maximum_error_ratio: float = .01
    slo_ms: float | None = None

    def accepts(self, result: dict) -> bool:
        return (result["offered"] > 0
                and result.get("generator_cap_skips", 0) == 0
                and result["completed_2xx"] / result["offered"] >= self.minimum_delivery_ratio
                and result["failed"] / result["offered"] <= self.maximum_error_ratio
                and (self.slo_ms is None or (
                    result["p95_client_ms"] is not None and result["p95_client_ms"] <= self.slo_ms)))


async def run_step_async(url: str, headers: dict[str, str], rate: float, duration: float,
                         workers: int, timeout: float, transport=None) -> dict:
    """Pace independent async requests without tying one thread to each arrival."""
    offered = math.floor(rate * duration)
    completed = []
    slots = asyncio.Semaphore(workers)
    tasks = []
    started = time.monotonic()

    async def read(client: httpx.AsyncClient, scheduled: float):
        before = time.monotonic()
        try:
            response = await client.get(url, headers=headers)
            outcome = {"status": response.status_code,
                       "latency_ms": (time.monotonic() - before) * 1000,
                       "start_lag_ms": max(0, (before - scheduled) * 1000)}
        except httpx.HTTPError as exc:
            outcome = {"error": type(exc).__name__,
                       "latency_ms": (time.monotonic() - before) * 1000,
                       "start_lag_ms": max(0, (before - scheduled) * 1000)}
        finally:
            slots.release()
        completed.append(outcome)

    refused = 0
    limits = httpx.Limits(max_connections=workers, max_keepalive_connections=workers)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False, limits=limits,
                                 transport=transport) as client:
        for index in range(offered):
            due = started + index / rate
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if slots.locked():
                refused += 1
                continue
            await slots.acquire()
            tasks.append(asyncio.create_task(read(client, due)))
        if tasks:
            await asyncio.gather(*tasks)

    statuses = Counter(str(item["status"]) for item in completed if "status" in item)
    successful = [item["latency_ms"] for item in completed if 200 <= item.get("status", 0) < 300]
    accepted = len(successful)
    failed = offered - accepted
    elapsed = time.monotonic() - started
    return {"offered_rps": rate, "duration_seconds": duration, "elapsed_seconds": round(elapsed, 3),
            "offered": offered, "completed": len(completed), "completed_2xx": accepted,
            "failed": failed, "generator_cap_skips": refused,
            "status_counts": dict(statuses),
            "transport_errors": dict(Counter(item["error"] for item in completed if "error" in item)),
            "achieved_rps": round(accepted / duration, 4),
            "p95_client_ms": round(nearest_rank_p95(successful), 3) if successful else None,
            "p95_start_lag_ms": round(nearest_rank_p95([item["start_lag_ms"] for item in completed]), 3)
            if completed else None}


def run_step(url: str, headers: dict[str, str], rate: float, duration: float,
             workers: int, timeout: float) -> dict:
    return asyncio.run(run_step_async(url, headers, rate, duration, workers, timeout))


def select_capacity(steps: list[dict], criteria: Criteria) -> dict:
    """Only an accepted measured step can define a conservative target capacity."""
    accepted = [s["offered_rps"] for s in steps if criteria.accepts(s)]
    if not accepted:
        return {"capacity_rps": None, "load_30_rps": None, "load_70_rps": None,
                "limitation": "No rate met the configured criteria; capacity not established"}
    capacity = max(accepted)
    return {"capacity_rps": capacity, "load_30_rps": round(.3 * capacity, 4),
            "load_70_rps": round(.7 * capacity, 4),
            "limitation": "Highest passing tested read rate, not an estimated saturation point or live-write capacity"}


def positive_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("Must be finite and positive")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Local URL of an already-applied event, e.g. http://127.0.0.1:18101/events/ID")
    parser.add_argument("--rates", required=True, help="Comma-separated offered read RPS, e.g. 5,10,20")
    parser.add_argument("--duration", type=positive_float, default=30)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=positive_float, default=3)
    parser.add_argument("--slo-ms", type=positive_float)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token-env", default="CLOUD_LIVE_TELEMETRY_TOKEN")
    args = parser.parse_args(argv)
    try:
        url = local_url(args.url)
        rates = [positive_float(s.strip()) for s in args.rates.split(",")]
        if not 1 <= args.workers <= 128 or len(rates) > 20 or len(set(rates)) != len(rates):
            raise ValueError("Specify 1..128 workers and 1..20 distinct rates")
        if any(rate * args.duration > 100_000 for rate in rates):
            raise ValueError("Each step is limited to 100,000 offered requests")
        token = os.environ.get(args.token_env, "")
        if len(token) < 32:
            raise ValueError("Live telemetry token environment variable is missing or too short")
        if args.output.exists():
            raise ValueError("Output already exists; choose a new evidence file")
        headers = {TRAFFIC_HEADER: "live", TOKEN_HEADER: token}
        # A known record must exist *before* calibration; 404 is not live-read success.
        with httpx.Client(timeout=args.timeout, trust_env=False) as client:
            check = client.get(url, headers=headers)
        if check.status_code != 200:
            raise ValueError(f"Existing-record preflight must return 200, got {check.status_code}")
        criteria = Criteria(slo_ms=args.slo_ms)
        steps = []
        for rate in sorted(rates):
            result = run_step(url, headers, rate, args.duration, args.workers, args.timeout)
            result["accepted"] = criteria.accepts(result)
            steps.append(result)
        report = {"kind": "calibrated_existing_record_live_read", "criteria": asdict(criteria),
                  "workers": args.workers, "timeout_seconds": args.timeout,
                  "endpoint_path": urlparse(url).path, "steps": steps,
                  "targets": select_capacity(steps, criteria),
                  "limitations": ["Read-only existing-record workload; does not measure live writes.",
                                  "Capacity is workload, host and configuration specific.",
                                  "Measured client p95 is not identical to service middleware p95.",
                                  "One run per rate is exploratory; replicate before proposal acceptance."]}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Calibration saved to {args.output}; selected read capacity: {report['targets']['capacity_rps']}")
        return 0
    except (ValueError, httpx.HTTPError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
