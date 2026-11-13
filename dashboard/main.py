
# dashboard/main.py
#
# CrisisGuard local testing dashboard
# Run:
# python -m uvicorn dashboard.main:app --host 127.0.0.1 --port 8080
import os
import secrets
from ipaddress import ip_address, ip_network

from fastapi import Request
from psycopg import OperationalError
from cloud.database.db import get_recent_cloud_events
from cloud.traffic import traffic_headers

from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field


app = FastAPI(
    title="CrisisGuard Dashboard",
    description="Local development and API testing console",
    version="0.1.0",
)
@app.get("/health")
def health():
    return {"status": "ok", "service": "dashboard"}

@app.middleware("http")
async def local_access_only(request: Request, call_next):
    # The dashboard is published only on 127.0.0.1 by Docker Compose.
    # Docker's proxy may make local requests appear to come from
    # a Docker bridge-network IP address.
    try:
        peer = ip_address(request.client.host)
        allowed = peer.is_loopback or any(peer in ip_network(cidr) for cidr in
            ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    except (ValueError, AttributeError):
        allowed = False
    if not allowed:
        return JSONResponse(status_code=403, content={'detail': 'Invalid dashboard peer'})
    if request.url.hostname not in ("127.0.0.1", "localhost", "::1"):
        return JSONResponse(
            status_code=403,
            content={"detail": "Invalid dashboard host"},
        )

    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        return JSONResponse(
            status_code=403,
            content={"detail": "Cross-origin access denied"},
        )

    if request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse(
            status_code=403,
            content={"detail": "Cross-site access denied"},
        )

    return await call_next(request)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

SERVICES = {
    "patient": os.getenv("DASHBOARD_PATIENT_URL", "http://127.0.0.1:8001"),
    "emergency": os.getenv("DASHBOARD_EMERGENCY_URL", "http://127.0.0.1:8003"),
    "admin": os.getenv("DASHBOARD_ADMIN_URL", "http://127.0.0.1:8004"),
}

HOSPITALS = {
    "hospital-a": os.getenv("DASHBOARD_HOSPITAL_A_URL", "http://127.0.0.1:8000"),
    "hospital-b": os.getenv("DASHBOARD_HOSPITAL_B_URL", "http://127.0.0.1:8002"),
}

EVENT_TYPES = {
    "patient": {"patient_registration", "encounter_creation"},
    "emergency": {"emergency_record"},
    "admin": {"patient_discharge"},
}

# Disabled unless explicitly configured for a local-development deployment.
from dashboard.test_traffic import install as install_test_traffic
install_test_traffic(app, SERVICES, EVENT_TYPES, HOSPITALS)


async def hospital_request(method: str, url: str, **kwargs):
    # uvloop shares a small libuv DNS pool with Cloud probes. Stopped Docker
    # services can exhaust it, timing out otherwise reachable hospital names.
    # Resolve hospital requests in a worker using the system resolver instead.
    return await run_in_threadpool(httpx.request, method, url, **kwargs)

app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static",
)


class TestEvent(BaseModel):
    event_id: str = Field(min_length=1)
    site_id: str
    event_type: str
    base_priority: int = Field(ge=1, le=5)
    prerequisite_ids: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


def get_service_url(service: str) -> str:
    if service not in SERVICES:
        raise HTTPException(
            status_code=404,
            detail="Unknown CrisisGuard service",
        )
    return SERVICES[service]


async def forward_request(
    service: str,
    method: str,
    endpoint: str,
    body: dict | None = None,
):
    url = get_service_url(service) + endpoint

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.request(
                method,
                url,
                json=body,
                **({"headers": traffic_headers("live")}
                   if method.upper() == "POST" and endpoint == "/events" else {}),
            )

        try:
            content = response.json()
        except ValueError:
            content = {"message": response.text[:500]}

        return {
            "service": service,
            "http_status": response.status_code,
            "response": content,
        }

    except httpx.RequestError as exc:
        return {
            "service": service,
            "http_status": None,
            "response": {
                "code": "SERVICE_UNREACHABLE",
                "message": str(exc),
            },
        }


@app.get("/")
def dashboard_home():
    index = STATIC_DIR / "index.html"

    if index.exists():
        if os.getenv("METAGUARD_REQUIRED") == "1":
            html = index.read_text(encoding="utf-8").replace(
                '<details class="advanced-tools">',
                '<p>Direct Cloud submissions are disabled. Use Hospital A/B capture '
                'for grant-controlled delivery.</p><details class="advanced-tools" hidden>',
            )
            return HTMLResponse(html)
        return FileResponse(index)

    return {
        "message": "Dashboard backend is running",
        "next_step": "Create dashboard/static/index.html",
    }


@app.get("/api/services")
async def service_health():
    results = {}

    for service in SERVICES:
        results[service] = await forward_request(
            service,
            "GET",
            "/health",
        )

    return results


@app.get("/api/metrics")
async def cloud_metrics():
    results = {}

    for service in SERVICES:
        results[service] = await forward_request(
            service,
            "GET",
            "/metrics",
        )

    return results


@app.get("/api/status-codes")
def status_codes():
    return {
        "200": {
            "title": "Already Applied",
            "description": "The identical event was previously applied.",
            "success": True,
        },
        "201": {
            "title": "Event Applied",
            "description": "The new event was successfully stored.",
            "success": True,
        },
        "409": {
            "title": "Conflict",
            "description": (
                "A prerequisite is missing or the event ID "
                "already exists with different data."
            ),
            "success": False,
        },
        "422": {
            "title": "Invalid Input",
            "description": (
                "The request does not match the API schema."
            ),
            "success": False,
        },
        "503": {
            "title": "Service Unavailable",
            "description": (
                "The cloud API cannot access PostgreSQL."
            ),
            "success": False,
        },
    }


@app.post("/api/events/{service}")
async def submit_event(service: str, event: TestEvent):
    get_service_url(service)

    if os.getenv("METAGUARD_REQUIRED") == "1":
        raise HTTPException(
            status_code=403,
            detail="Direct Cloud submissions are disabled. Use Hospital A/B capture for grant-controlled delivery.",
        )

    if event.event_type not in EVENT_TYPES[service]:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Event type '{event.event_type}' "
                f"is not supported by {service}."
            ),
        )

    return await forward_request(
        service,
        "POST",
        "/events",
        body=event.model_dump(),
    )

@app.get("/api/history")
def dashboard_event_history(request: Request, limit: int = 50):
    try:
        events = get_recent_cloud_events(limit)
        return {
            "count": len(events),
            "events": events,
        }
    except OperationalError as error:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL is unavailable",
        ) from error


@app.get("/api/hospitals")
async def hospital_status():
    results = {}

    for hospital, url in HOSPITALS.items():
        try:
            response = await hospital_request("GET", f"{url}/events", timeout=5.0)
            response.raise_for_status()

            events = response.json()
            if not isinstance(events, list) or any(
                not isinstance(event, dict) or event.get("status") not in ("pending", "applied")
                for event in events
            ):
                raise ValueError("Hospital returned an invalid event queue")

            results[hospital] = {
                "status": "healthy",
                "total": len(events),
                "pending": sum(
                    1 for e in events
                    if e.get("status") == "pending"
                ),
                "applied": sum(
                    1 for e in events
                    if e.get("status") == "applied"
                ),
                "events": events,
            }

        except (httpx.HTTPError, ValueError, TypeError) as error:
            results[hospital] = {
                "status": "unavailable",
                "total": 0,
                "pending": 0,
                "applied": 0,
                "events": [],
                "error": f"{type(error).__name__}: {error}",
            }

    return results

@app.get("/api/hospitals/{hospital}/scheduler/inspect")
async def dashboard_scheduler_inspect(
    hospital: str,
    inheritance: bool | None = None,
):
    if hospital not in HOSPITALS:
        raise HTTPException(
            status_code=404,
            detail="Unknown hospital",
        )

    try:
        response = await hospital_request(
            "GET",
            f"{HOSPITALS[hospital]}/scheduler/inspect",
            timeout=10.0,
            params={} if inheritance is None else {
                "inheritance": str(inheritance).lower()
            },
        )

        response.raise_for_status()
        return response.json()

    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=503,
            detail="Hospital scheduler preview unavailable",
        ) from error

@app.post("/api/hospitals/{hospital}/capture")
async def capture_hospital_event(
    hospital: str,
    event: TestEvent,
):
    if hospital not in HOSPITALS:
        raise HTTPException(
            status_code=404,
            detail="Unknown hospital",
        )

    expected_site = {
        "hospital-a": "Hospital_A",
        "hospital-b": "Hospital_B",
    }[hospital]

    if event.site_id != expected_site:
        raise HTTPException(
            status_code=422,
            detail="Event site_id does not match hospital",
        )

    try:
        response = await hospital_request(
            "POST",
            f"{HOSPITALS[hospital]}/capture",
            timeout=8.0,
            json=event.model_dump(),
        )

        return {
            "http_status": response.status_code,
            "response": response.json(),
        }

    except httpx.RequestError as error:
        raise HTTPException(
            status_code=503,
            detail=f"Hospital API unavailable: {error}",
        ) from error





@app.post("/api/hospitals/{hospital}/replay")
async def replay_hospital_events(hospital: str, request: Request):
    if hospital not in HOSPITALS:
        raise HTTPException(
            status_code=404,
            detail="Unknown hospital",
        )

    if os.getenv("ENABLE_MANUAL_REPLAY") != "1":
        raise HTTPException(status_code=404, detail="Manual replay is disabled")
    token = os.getenv("MANUAL_REPLAY_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="Manual replay token is not configured")
    operator_token = os.getenv("DASHBOARD_REPLAY_OPERATOR_TOKEN", "")
    if len(operator_token) < 32 or operator_token == token:
        raise HTTPException(status_code=503, detail="Dashboard replay authorization is not configured")
    supplied = request.headers.get("Authorization", "")
    if not secrets.compare_digest(supplied.encode(), f"Bearer {operator_token}".encode()):
        raise HTTPException(status_code=403, detail="Manual replay authorization required")

    try:
        response = await hospital_request(
            "POST",
            f"{HOSPITALS[hospital]}/replay",
            timeout=60.0,
            headers={"Authorization": f"Bearer {token}"},
        )

        return {
            "http_status": response.status_code,
            "response": response.json(),
        }

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Replay timed out. Refresh both event lists to check the actual result.",
        )

    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="Hospital API is unavailable",
        )


@app.get("/api/recovery")
async def recovery_monitoring():
    """Read-only, allowlisted views. Never return upstream configuration or errors."""
    hospitals = {}
    fields = ("enabled", "cloud_reachable", "last_check_at", "last_reconnected_at",
              "drain_completed_at", "last_replay_at", "last_replay_attempted",
              "last_replay_applied", "replayable_backlog")
    for hospital, url in HOSPITALS.items():
        try:
            response = await hospital_request("GET", f"{url}/recovery/status", timeout=5.0)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Invalid recovery status")
            hospitals[hospital] = {"status": "available", **{k: body[k] for k in fields if k in body}}
        except (httpx.HTTPError, ValueError, TypeError):
            hospitals[hospital] = {"status": "unavailable"}

    controller = {"status": "not_configured"}
    url = os.getenv("DASHBOARD_CONTROLLER_URL")
    token = os.getenv("DASHBOARD_CONTROLLER_TOKEN")
    if url and token:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url.rstrip("/") + "/admission/status",
                                            headers={"x-cloud-token": token})
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("state") not in ("Normal", "Caution", "Recovery"):
                raise ValueError("Invalid admission status")
            fields = ("enabled", "state", "state_since", "observed_at", "server_time", "rate_rps",
                      "inflight_limit", "healthy_samples", "allowed", "denied", "telemetry_fresh", "policy")
            controller = {"status": "available", **{k: body[k] for k in fields if k in body}}
        except (httpx.HTTPError, ValueError, TypeError):
            controller = {"status": "unavailable"}
    return {"hospitals": hospitals, "controller": controller}
