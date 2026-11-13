"""Bounded, read-only Checkpoint 7B Cloud telemetry calibration.

Uses authenticated live provenance on existing-event GETs only. It performs no
event submission, configuration update, Docker operation, or database write.
The output path is exclusive: existing evidence is never overwritten.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter

import httpx


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "docs/evidence/cp7b-cloud-telemetry-calibration-20261028.json"
SERVICES = {
    "patient": {
        "base_url": "http://127.0.0.1:8001",
        "event_id": "CP7B_RESUME_20261028T123204Z_Hospital_A_stale",
    },
    "emergency": {
        "base_url": "http://127.0.0.1:8003",
        "event_id": "DASH_0c768eef92d1413b866cbbeb45bffa25",
    },
    "admin": {
        "base_url": "http://127.0.0.1:8004",
        "event_id": "DASH_e5d593df3dd444ffbc3d37e513d628df",
    },
}
STEADY_RPS = 6.0
STEADY_REQUESTS = 60
BURST_LEVELS = (20, 40, 80, 160)
METRICS_SAMPLE_DELAYS = (0.002, 0.005, 0.01, 0.02, 0.04)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def private_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


async def metrics(client: httpx.AsyncClient, service: str, phase: str) -> dict:
    response = await client.get(SERVICES[service]["base_url"] + "/metrics")
    response.raise_for_status()
    return {"captured_at": utc_now(), "phase": phase, **response.json()}


async def controller(client: httpx.AsyncClient, token: str, phase: str) -> dict:
    response = await client.get(
        "http://127.0.0.1:8005/admission/status",
        headers={"X-Cloud-Token": token},
    )
    response.raise_for_status()
    return {"captured_at": utc_now(), "phase": phase, **response.json()}


async def application_get(
    client: httpx.AsyncClient,
    service: str,
    headers: dict[str, str],
    phase: str,
    ordinal: int,
) -> dict:
    url = SERVICES[service]["base_url"] + "/events/" + SERVICES[service]["event_id"]
    started_at = utc_now()
    started = perf_counter()
    try:
        response = await client.get(url, headers=headers)
        status = response.status_code
        body_state = response.json().get("status") if response.headers.get("content-type", "").startswith("application/json") else None
        error = None
    except Exception as exc:
        status = None
        body_state = None
        error = type(exc).__name__
    ended = perf_counter()
    return {
        "service": service,
        "phase": phase,
        "ordinal": ordinal,
        "started_at": started_at,
        "finished_at": utc_now(),
        "client_latency_ms": round((ended - started) * 1000, 6),
        "status_code": status,
        "body_status": body_state,
        "error": error,
    }


async def sample_after(
    client: httpx.AsyncClient,
    service: str,
    phase: str,
    delay: float,
) -> dict:
    await asyncio.sleep(delay)
    return await metrics(client, service, f"{phase}:delay={delay}")


async def steady_service(
    client: httpx.AsyncClient,
    service: str,
    headers: dict[str, str],
) -> list[dict]:
    requests = []
    start = asyncio.get_running_loop().time()
    for ordinal in range(1, STEADY_REQUESTS + 1):
        deadline = start + (ordinal - 1) / STEADY_RPS
        await asyncio.sleep(max(0.0, deadline - asyncio.get_running_loop().time()))
        requests.append(asyncio.create_task(application_get(client, service, headers, "steady", ordinal)))
    return await asyncio.gather(*requests)


async def periodic_samples(
    client: httpx.AsyncClient,
    operator_token: str,
    duration: float,
) -> tuple[list[dict], list[dict]]:
    metric_samples: list[dict] = []
    controller_samples: list[dict] = []
    start = asyncio.get_running_loop().time()
    next_metrics = start
    next_controller = start
    while asyncio.get_running_loop().time() - start <= duration:
        now = asyncio.get_running_loop().time()
        tasks = []
        kinds = []
        if now >= next_metrics:
            for service in SERVICES:
                tasks.append(asyncio.create_task(metrics(client, service, "steady")))
                kinds.append("metrics")
            next_metrics += 0.25
        if now >= next_controller:
            tasks.append(asyncio.create_task(controller(client, operator_token, "steady")))
            kinds.append("controller")
            next_controller += 0.5
        if tasks:
            results = await asyncio.gather(*tasks)
            metric_count = kinds.count("metrics")
            metric_samples.extend(results[:metric_count])
            if "controller" in kinds:
                controller_samples.append(results[-1])
        await asyncio.sleep(0.01)
    return metric_samples, controller_samples


async def burst(
    client: httpx.AsyncClient,
    service: str,
    headers: dict[str, str],
    count: int,
) -> tuple[list[dict], list[dict]]:
    phase = f"burst_{count}"
    request_tasks = [
        asyncio.create_task(application_get(client, service, headers, phase, ordinal))
        for ordinal in range(1, count + 1)
    ]
    sample_tasks = [
        asyncio.create_task(sample_after(client, service, phase, delay))
        for delay in METRICS_SAMPLE_DELAYS
    ]
    requests, samples = await asyncio.gather(
        asyncio.gather(*request_tasks),
        asyncio.gather(*sample_tasks),
    )
    return requests, samples


def summarize_requests(requests: list[dict]) -> dict:
    latencies = sorted(item["client_latency_ms"] for item in requests)
    statuses: dict[str, int] = {}
    for item in requests:
        key = str(item["status_code"])
        statuses[key] = statuses.get(key, 0) + 1
    rank = (95 * len(latencies) + 99) // 100 - 1
    return {
        "requests": len(requests),
        "status_counts": statuses,
        "errors": sum(item["error"] is not None for item in requests),
        "client_mean_latency_ms": round(sum(latencies) / len(latencies), 6),
        "client_p95_latency_ms_nearest_rank": latencies[rank],
        "client_max_latency_ms": latencies[-1],
    }


async def run(output: Path, idle_expiry_seconds: float) -> dict:
    env = private_env()
    live_token = env.get("CLOUD_LIVE_TELEMETRY_TOKEN", "")
    replay_token = env.get("CLOUD_REPLAY_TELEMETRY_TOKEN", "")
    operator_token = env.get("CONTROLLER_CLOUD_TOKEN", "")
    if min(map(len, (live_token, replay_token, operator_token))) < 32 or live_token == replay_token:
        raise RuntimeError("Required distinct private credentials are unavailable")
    live_headers = {
        "X-CrisisGuard-Traffic": "live",
        "X-CrisisGuard-Telemetry-Token": live_token,
    }
    report = {
        "run": "cp7b-cloud-telemetry-calibration-20261028",
        "scope": "bounded read-only original desktop Cloud telemetry calibration",
        "started_at": utc_now(),
        "source_commit": "66e66bb2f566afd9f2d1e792c3267fc89ac34c74",
        "credentials": {
            "live_present_and_strong": len(live_token) >= 32,
            "replay_present_and_strong": len(replay_token) >= 32,
            "roles_distinct": live_token != replay_token,
            "values_recorded": False,
        },
        "limits": {
            "method": "GET",
            "paths": {service: "/events/" + data["event_id"] for service, data in SERVICES.items()},
            "steady_rps_per_service": STEADY_RPS,
            "steady_requests_per_service": STEADY_REQUESTS,
            "burst_concurrency_levels_per_service": list(BURST_LEVELS),
            "maximum_concurrency": max(BURST_LEVELS),
            "expected_application_requests_per_service": STEADY_REQUESTS + sum(BURST_LEVELS),
            "request_timeout_seconds": 15,
            "event_writes": 0,
        },
        "baseline_metrics": {},
        "baseline_controller": None,
        "requests": [],
        "metrics_samples": [],
        "controller_samples": [],
        "phase_summaries": [],
    }
    timeout = httpx.Timeout(15.0)
    limits = httpx.Limits(max_connections=400, max_keepalive_connections=200)
    async with httpx.AsyncClient(timeout=timeout, limits=limits, trust_env=False) as client:
        for service in SERVICES:
            report["baseline_metrics"][service] = await metrics(client, service, "baseline")
        report["baseline_controller"] = await controller(client, operator_token, "baseline")

        print("steady phase", flush=True)
        sampler = asyncio.create_task(periodic_samples(client, operator_token, 11.0))
        steady_results = await asyncio.gather(
            *(steady_service(client, service, live_headers) for service in SERVICES)
        )
        steady_metrics, steady_controller = await sampler
        report["metrics_samples"].extend(steady_metrics)
        report["controller_samples"].extend(steady_controller)
        for service, results in zip(SERVICES, steady_results):
            report["requests"].extend(results)
            report["phase_summaries"].append({"service": service, "phase": "steady", **summarize_requests(results)})

        for service in SERVICES:
            for count in BURST_LEVELS:
                print(f"burst {service} {count}", flush=True)
                before = await metrics(client, service, f"burst_{count}:before")
                report["metrics_samples"].append(before)
                results, samples = await burst(client, service, live_headers, count)
                report["requests"].extend(results)
                report["metrics_samples"].extend(samples)
                after = await metrics(client, service, f"burst_{count}:after")
                report["metrics_samples"].append(after)
                report["phase_summaries"].append({"service": service, "phase": f"burst_{count}", **summarize_requests(results)})
                report["controller_samples"].append(await controller(client, operator_token, f"after_{service}_burst_{count}"))
                await asyncio.sleep(0.35)

        await asyncio.sleep(6.0)
        report["post_workload_metrics"] = {
            service: await metrics(client, service, "post_workload") for service in SERVICES
        }
        report["post_workload_controller"] = await controller(client, operator_token, "post_workload")

        print(f"idle expiry {idle_expiry_seconds}s", flush=True)
        await asyncio.sleep(idle_expiry_seconds)
        report["post_expiry_metrics"] = {
            service: await metrics(client, service, "post_expiry") for service in SERVICES
        }
        report["post_expiry_controller"] = await controller(client, operator_token, "post_expiry")

    report["finished_at"] = utc_now()
    report["request_summary"] = summarize_requests(report["requests"])
    report["result"] = "passed" if report["request_summary"]["errors"] == 0 else "failed"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--idle-expiry-seconds", type=float, default=61.0)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    report = asyncio.run(run(args.output, args.idle_expiry_seconds))
    print(json.dumps({
        "result": report["result"],
        "requests": report["request_summary"]["requests"],
        "status_counts": report["request_summary"]["status_counts"],
        "output": str(args.output),
    }, indent=2))


if __name__ == "__main__":
    main()
