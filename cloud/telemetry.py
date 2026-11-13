
# cloud/telemetry.py
import math
import os
from datetime import datetime, timezone
from collections import deque
from threading import Lock
from time import monotonic, perf_counter
from urllib import request

from fastapi import FastAPI, HTTPException, Request
from anyio.to_thread import current_default_thread_limiter
from starlette.concurrency import run_in_threadpool

from cloud.traffic import classify_traffic


WINDOW_SECONDS = 60


class Telemetry:
    def __init__(self):
        self.started_at = monotonic()
        self.lock = Lock()
        self.requests = deque()
        self.classified_requests = deque()
        self.total_requests = 0
        self.failed_requests = 0
        self.in_flight_requests = 0
        configured_slo = os.getenv("CLOUD_LIVE_SLO_MS", "").strip()

        if configured_slo:
            threshold = float(configured_slo)
            if not math.isfinite(threshold) or threshold <= 0:
                raise ValueError("CLOUD_LIVE_SLO_MS must be finite and positive")
            self.live_slo_threshold_ms = threshold
        else:
            self.live_slo_threshold_ms = None

    def start_request(self):
        """Count application work after middleware entry, not a server queue."""
        with self.lock:
            self.in_flight_requests += 1

    def record(self, status_code: int, latency_ms: float, *, complete_request=False, traffic_kind="unclassified"):
        if traffic_kind not in ("live", "replay", "unclassified"):
            raise ValueError("Unknown telemetry traffic kind")
        with self.lock:
            # Timestamp inside the same lock as append: concurrent completions
            # must remain time-ordered for prefix expiry to be correct.
            now = monotonic()
            if complete_request:
                self.in_flight_requests -= 1
            self.total_requests += 1

            if status_code >= 500:
                self.failed_requests += 1

            self.requests.append(
                (now, status_code, latency_ms)
            )
            self.classified_requests.append((now, traffic_kind, latency_ms))
            self._cleanup(now)

    def _cleanup(self, now):
        cutoff = now - WINDOW_SECONDS

        while self.requests and self.requests[0][0] < cutoff:
            self.requests.popleft()
        while self.classified_requests and self.classified_requests[0][0] < cutoff:
            self.classified_requests.popleft()

    def snapshot(self):
        with self.lock:
            now = monotonic()
            self._cleanup(now)

            recent = list(self.requests)
            total = len(recent)

            errors = sum(
                1 for _, status, _ in recent
                if status >= 500
            )

            latency = sum(
                duration for _, _, duration in recent
            )

            durations = sorted(duration for _, _, duration in recent)
            # Exact nearest-rank p95: ceil(95*n/100), one-based. No samples
            # means no percentile observation, rather than a measured zero.
            p95 = durations[(95 * total + 99) // 100 - 1] if total else None

            uptime = max(now - self.started_at, 0.001)
            period = min(uptime, WINDOW_SECONDS)

            live_durations = sorted(duration for _, kind, duration in self.classified_requests
                                    if kind == "live")
            live_count = len(live_durations)
            live_p95 = live_durations[(95 * live_count + 99) // 100 - 1] if live_count else None
            slo_threshold = self.live_slo_threshold_ms

            live_slo_violations = (
                sum(duration > slo_threshold for duration in live_durations)
                if slo_threshold is not None
                else None
            )

            live_slo_rate = (
                live_slo_violations / live_count
                if slo_threshold is not None and live_count
                else None
            )
            replay_count = sum(kind == "replay" for _, kind, _ in self.classified_requests)
            return {
                "live_slo_threshold_ms": slo_threshold,
                "live_slo_violation_count": live_slo_violations,
                "live_slo_violation_rate": (
                    round(live_slo_rate, 4)
                    if live_slo_rate is not None
                    else None
                ),
                "live_p95_latency_ms": round(live_p95, 2) if live_p95 is not None else None,
                "live_latency_sample_count": live_count,
                "replay_latency_sample_count": replay_count,
                "unclassified_latency_sample_count": len(self.classified_requests) - live_count - replay_count,
                "p95_latency_ms": round(p95, 2) if p95 is not None else None,
                "latency_sample_count": total,
                "observation_seconds": round(period, 6),
                "in_flight_requests": self.in_flight_requests,
                "average_latency_ms": round(
                    latency / total, 2
                ) if total else 0.0,

                "error_rate": round(
                    errors / total, 4
                ) if total else 0.0,

                "throughput_rps": round(
                    total / period, 4
                ),

                "window_requests": total,
                "total_requests": self.total_requests,
                "failed_requests": self.failed_requests,
                "window_seconds": WINDOW_SECONDS,
                "uptime_seconds": round(uptime, 2),
            }


def setup_telemetry(
    app: FastAPI,
    service_name: str,
    database_check,
):
    telemetry = Telemetry()

    # Make telemetry available to other components.
    app.state.telemetry = telemetry

    @app.middleware("http")
    async def telemetry_middleware(
        request: Request,
        call_next,
    ):
        # Prevent monitoring requests from distorting metrics.
        if request.url.path in ("/metrics", "/health"):
            return await call_next(request)

        traffic_kind = classify_traffic(request.headers)
        start = perf_counter()
        telemetry.start_request()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response

        finally:
            latency_ms = (
                perf_counter() - start
            ) * 1000

            telemetry.record(
                status_code=status_code,
                latency_ms=latency_ms,
                complete_request=True,
                traffic_kind=traffic_kind,
            )

    @app.get(
        "/metrics",
        summary="Cloud Health Telemetry",
        description=(
            "Provides request metrics for MetaGuard. "
            "Latency, error rate and throughput use "
            "a rolling 60-second window. p95_latency_ms is nearest-rank "
            "over the same completed requests (null when empty). "
            "live_p95_latency_ms uses only authenticated live provenance; unmarked traffic "
            "is unclassified. executor_queue_depth is the process AnyIO worker-limiter "
            "wait queue, sampled before the monitoring database probe. "
            "in_flight_requests counts active application requests, not queue depth."
        ),
    )
    async def get_metrics():
        sampled_at = datetime.now(timezone.utc).isoformat()
        data = telemetry.snapshot()
        # Read the actual limiter, not active HTTP requests or an estimated backlog.
        # This async handler observes even when every sync worker is occupied.
        workers = current_default_thread_limiter().statistics()
        data.update({
            "executor_queue_depth": workers.tasks_waiting,
            "executor_active_workers": workers.borrowed_tokens,
            "executor_worker_limit": workers.total_tokens,
            "executor_queue_pressure": workers.tasks_waiting / workers.total_tokens,
            "executor_queue_sampled_at": datetime.now(timezone.utc).isoformat(),
        })

        try:
            await run_in_threadpool(database_check)
            database_healthy = True
        except Exception:
            database_healthy = False

        data.update({
            "sampled_at": sampled_at,
            "service": service_name,
            "database_healthy": database_healthy,
        })

        return data
