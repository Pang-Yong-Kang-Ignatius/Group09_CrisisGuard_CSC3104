import asyncio
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from edge.capture_api.auto_replay import recovery_state, run_auto_replay
from edge.capture_api.cloud_client import SUPPORTED_EVENT_TYPES
from edge.capture_api.scheduled_replay import replay_scheduled_events
from edge.database.db import init_db, save_event, get_events
from edge.scheduler.live_preview import inspect_hospital_queue
from edge.scheduler.validation import validate_prerequisite_ids
from edge.scheduler.config import load_scheduler_settings
from edge.capture_api import admission_retry


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse invalid replay policy before accepting captures or starting recovery.
    load_scheduler_settings()
    admission_retry.enabled()
    init_db()

    stop_event = asyncio.Event()
    task = None

    if os.getenv("AUTO_REPLAY") == "1":
        interval = float(os.getenv("AUTO_REPLAY_INTERVAL_SECONDS", "5"))
        task = asyncio.create_task(run_auto_replay(stop_event, interval))

    yield

    if task is not None:
        stop_event.set()
        await task


app = FastAPI(
    title="CrisisGuard Hospital API",
    lifespan=lifespan
)


EventType = Literal[
    "patient_registration",
    "encounter_creation",
    "emergency_record",
    "patient_discharge",
]

# Keep capture validation and cloud routing in sync.
assert set(EventType.__args__) == set(SUPPORTED_EVENT_TYPES)


class Event(BaseModel):
    event_id: str = Field(min_length=1)
    # Optional: filled in from SITE_ID when omitted.
    site_id: str | None = None
    event_type: EventType
    base_priority: int = Field(ge=1, le=5)
    prerequisite_ids: list[str] = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)

    @field_validator("prerequisite_ids")
    @classmethod
    def check_prerequisite_ids(cls, value: list[str]) -> list[str]:
        validate_prerequisite_ids(value)
        return value


@app.get("/")
def home():
    return {
        "message": "CrisisGuard Hospital API running",
        "site_id": os.getenv("SITE_ID"),
    }


@app.post("/capture", status_code=201)
def capture_event(event: Event):
    data = event.model_dump()

    if not data["event_id"].strip():
        raise HTTPException(status_code=422, detail="event_id cannot be blank")

    configured_site = os.getenv("SITE_ID")

    if configured_site:
        if data["site_id"] is None:
            data["site_id"] = configured_site
        elif data["site_id"] != configured_site:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This hospital is {configured_site}; "
                    f"cannot capture events for {data['site_id']}"
                )
            )
    elif data["site_id"] is None:
        raise HTTPException(
            status_code=422,
            detail="site_id is required when SITE_ID is not configured"
        )

    try:
        save_event(data)
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="Event ID already exists"
        )

    return {
        "status": "pending",
        "event_id": data["event_id"],
        "site_id": data["site_id"],
    }


@app.get("/events")
def list_events():
    return get_events()

@app.get("/scheduler/inspect")
def get_scheduler_inspection(inheritance: bool | None = None):
    return inspect_hospital_queue(
        enable_inheritance=inheritance
    )

@app.get("/recovery/status")
def get_recovery_status():
    return recovery_state


@app.post("/replay")
def replay_events(request: Request):
    if os.getenv("ENABLE_MANUAL_REPLAY") != "1":
        raise HTTPException(
            status_code=404,
            detail="Manual replay is disabled"
        )

    # Authorization works across Docker forwarding without trusting client IPs.
    token = os.getenv("MANUAL_REPLAY_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="Manual replay token is not configured")
    supplied = request.headers.get("Authorization", "")
    if not secrets.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        raise HTTPException(status_code=403, detail="Manual replay authorization required")

    results = replay_scheduled_events()

    return {
        "total_attempted": len(results),
        "applied": sum(
            1 for result in results
            if result["status"] == "applied"
        ),
        "results": results,
    }
