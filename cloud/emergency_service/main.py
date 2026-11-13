
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Response, Body, Request
from cloud.metaguard import cloud_persist
from pydantic import BaseModel, Field
from psycopg import OperationalError
from cloud.telemetry import setup_telemetry
from cloud.database.db import (
    init_db,
    test_connection,
    save_cloud_event,
    MissingPrerequisitesError,
    ConflictingEventError,
    get_cloud_event,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="CrisisGuard Emergency Cloud Service",
    description=(
        "Processes emergency hospital events with "
        "dependency validation and duplicate-safe acknowledgements."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


class Event(BaseModel):
    event_id: str = Field(
        min_length=1,
        pattern=r"\S",
        description=(
            "Unique event ID. Cannot be empty or contain only spaces. "
            "Invalid values return HTTP 422."
        )
    )

    base_priority: int = Field(
        ge=1,
        le=5,
        description=(
            "Event priority must be between 1 and 5. "
            "Invalid values return HTTP 422."
        )
    )

    site_id: Literal[
        "Hospital_A",
        "Hospital_B"
    ] = Field(
        description="Hospital that created the event"
    )

    event_type: Literal["emergency_record"] = Field(
        description=(
            "Only emergency_record is accepted. "
            "Other event types return HTTP 422."
        )
    )


    prerequisite_ids: list[str] = Field(
        default_factory=list,
        description="Events that must already be applied"
    )

    payload: dict = Field(
        default_factory=dict,
        description="Emergency event information"
    )


class EventResponse(BaseModel):
    status: Literal["applied"]
    event_id: str
    site_id: str
    result: Literal["applied", "already_applied"]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "applied",
                    "event_id": "SWAGGER_EMERGENCY_001",
                    "site_id": "Hospital_A",
                    "result": "applied"
                }
            ]
        }
    }


EMERGENCY_TEST_EXAMPLES = {
    "new_event_201": {
        "summary": "201 - New Emergency Event",
        "description": "Run first to create a new emergency record.",
        "value": {
            "event_id": "SWAGGER_EMERGENCY_001",
            "site_id": "Hospital_A",
            "event_type": "emergency_record",
            "base_priority": 5,
            "prerequisite_ids": [],
            "payload": {
                "patient_id": "P_EMERGENCY_001",
                "emergency_type": "critical"
            }
        }
    },

    "identical_retry_200": {
        "summary": "200 - Identical Retry",
        "description": "Run after 201 to test duplicate-safe ACK.",
        "value": {
            "event_id": "SWAGGER_EMERGENCY_001",
            "site_id": "Hospital_A",
            "event_type": "emergency_record",
            "base_priority": 5,
            "prerequisite_ids": [],
            "payload": {
                "patient_id": "P_EMERGENCY_001",
                "emergency_type": "critical"
            }
        }
    },

    "missing_prerequisite_409": {
        "summary": "409 - Missing Prerequisite",
        "description": "Rejects an event with an unapplied prerequisite.",
        "value": {
            "event_id": "SWAGGER_EMERGENCY_MISSING_001",
            "site_id": "Hospital_A",
            "event_type": "emergency_record",
            "base_priority": 5,
            "prerequisite_ids": [
                "NONEXISTENT_EMERGENCY_DEP_999"
            ],
            "payload": {
                "patient_id": "P_EMERGENCY_002",
                "emergency_type": "critical"
            }
        }
    },

    "conflicting_id_409": {
        "summary": "409 - Conflicting Event ID",
        "description": "Run after 201. Reuses its ID with different data.",
        "value": {
            "event_id": "SWAGGER_EMERGENCY_001",
            "site_id": "Hospital_A",
            "event_type": "emergency_record",
            "base_priority": 5,
            "prerequisite_ids": [],
            "payload": {
                "patient_id": "DIFFERENT_PATIENT",
                "emergency_type": "critical"
            }
        }
    },

    "invalid_type_422": {
        "summary": "422 - Invalid Event Type",
        "description": "Patient registration is not an emergency event.",
        "value": {
            "event_id": "SWAGGER_EMERGENCY_INVALID_001",
            "site_id": "Hospital_A",
            "event_type": "patient_registration",
            "base_priority": 1,
            "prerequisite_ids": [],
            "payload": {
                "patient_id": "P_EMERGENCY_003"
            }
        }
    }
}


@app.get(
    "/",
    summary="Emergency Service Health Check"
)
def home():
    return {
        "message": "CrisisGuard Emergency Cloud Service running"
    }

@app.get(
    "/health",
    summary="Check Database Health",
    description="Checks whether the Emergency Service can connect to PostgreSQL."
)
def health_check():
    try:
        database = test_connection()

        return {
            "status": "healthy",
            "service": "emergency",
            "database": database
        }

    except Exception:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATABASE_UNAVAILABLE",
                "message": "PostgreSQL is not reachable"
            }
        )


@app.post(
    "/events",
    status_code=201,
    response_model=EventResponse,
    summary="Submit Emergency Event",
    description=(
        "Accepts emergency_record only. "
        "Prerequisites must already be applied. "
        "Identical retries are acknowledged without duplicate insertion. "
        "Use the selectable examples to test HTTP 200, 201, 409 and 422."
    ),
    responses={
        200: {
            "description": "Identical event already applied",
            "model": EventResponse
        },
        201: {
            "description": "New emergency event applied"
        },
        409: {
            "description": (
                "MISSING_PREREQUISITES or EVENT_CONFLICT"
            )
        },
        422: {
            "description": (
                "Invalid request or unsupported event type"
            )
        },
        503: {
            "description": (
                "DATABASE_UNAVAILABLE: PostgreSQL is not reachable. "
                "Retry the event after the database recovers."
            )
        }
    }
)
def receive_event(
    event: Annotated[
        Event,
        Body(openapi_examples=EMERGENCY_TEST_EXAMPLES)
    ],
    response: Response,
    request: Request
):
    try:
        result = cloud_persist(event.model_dump(), request, save_cloud_event)

    except MissingPrerequisitesError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MISSING_PREREQUISITES",
                "missing_ids": error.missing_ids
            }
        ) from error

    except ConflictingEventError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EVENT_CONFLICT",
                "message": str(error)
            }
        ) from error

    
    except OperationalError as error:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATABASE_UNAVAILABLE",
                "message": "PostgreSQL is not reachable"
            }
        ) from error


    if result == "already_applied":
        response.status_code = 200

    return {
        "status": "applied",
        "event_id": event.event_id,
        "site_id": event.site_id,
        "result": result
    }

# Look up whether an event was applied (used by hospital reconciliation)
@app.get(
    "/events",
    summary="Get Event Status by Query",
    description="Read-only lookup preserving reserved characters in the event_id query parameter."
)
@app.get(
    "/events/{event_id}",
    summary="Get Event Status",
    description=(
        "Returns the stored status of one event. "
        "404 means the cloud has never applied this event ID."
    )
)
def get_event_status(event_id: str):
    try:
        event = get_cloud_event(event_id)
    except OperationalError as error:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATABASE_UNAVAILABLE",
                "message": "PostgreSQL is not reachable"
            }
        ) from error

    if event is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "EVENT_NOT_FOUND", "event_id": event_id}
        )

    return event

setup_telemetry(
    app,
    service_name="emergency",
    database_check=test_connection,
)