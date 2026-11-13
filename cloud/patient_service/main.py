
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



# Initialize PostgreSQL on startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="CrisisGuard Patient Cloud Service",
    description=(
        "Patient event processing service for CrisisGuard. "
        "Supports patient registration, encounter creation, "
        "dependency validation and duplicate-safe acknowledgements."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# Request model
class Event(BaseModel):

    event_id: str = Field(
        min_length=1,
        pattern=r"\S",
        description=(
            "Required unique event identifier. "
            "Cannot be empty or contain only spaces. "
            "Invalid values return HTTP 422."
        )
    )


    site_id: Literal[
        "Hospital_A",
        "Hospital_B"
    ] = Field(
        description="Hospital that created the event"
    )

    event_type: Literal[
        "patient_registration",
        "encounter_creation"
    ] = Field(
        description=(
            "Allowed event types: patient_registration "
            "and encounter_creation. "
            "Invalid types return HTTP 422."
        )
    )


    base_priority: int = Field(
        ge=1,
        le=5,
        description=(
            "Event priority must be between 1 and 5 (inclusive). "
            "Values outside this range return HTTP 422."
        )
    )


    prerequisite_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Event IDs that must already be applied "
            "before this event can be processed"
        )
    )

    payload: dict = Field(
        default_factory=dict,
        description="Patient or encounter information"
    )


# Success response model
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
                    "event_id": "SWAGGER_PATIENT_TEST_001",
                    "site_id": "Hospital_A",
                    "result": "applied"
                }
            ]
        }
    }


# Selectable Swagger testing examples
PATIENT_TEST_EXAMPLES = {
    "new_event_201": {
        "summary": "201 - New Patient Event",
        "description": (
            "Creates a new patient event. "
            "Run this before testing retries or conflicts."
        ),
        "value": {
            "event_id": "SWAGGER_PATIENT_TEST_001",
            "site_id": "Hospital_A",
            "event_type": "patient_registration",
            "base_priority": 1,
            "prerequisite_ids": [],
            "payload": {
                "patient_id": "P_SWAGGER_001"
            }
        }
    },

    "identical_retry_200": {
        "summary": "200 - Identical Retry",
        "description": (
            "Submit after the 201 test. "
            "The same event should return already_applied."
        ),
        "value": {
            "event_id": "SWAGGER_PATIENT_TEST_001",
            "site_id": "Hospital_A",
            "event_type": "patient_registration",
            "base_priority": 1,
            "prerequisite_ids": [],
            "payload": {
                "patient_id": "P_SWAGGER_001"
            }
        }
    },

    "missing_prerequisite_409": {
        "summary": "409 - Missing Prerequisite",
        "description": (
            "Rejects an event because its required "
            "prerequisite has not been applied."
        ),
        "value": {
            "event_id": "SWAGGER_MISSING_TEST_001",
            "site_id": "Hospital_A",
            "event_type": "encounter_creation",
            "base_priority": 2,
            "prerequisite_ids": [
                "NONEXISTENT_EVENT_999"
            ],
            "payload": {
                "patient_id": "P_SWAGGER_002",
                "encounter_id": "ENC002"
            }
        }
    },

    "conflicting_id_409": {
        "summary": "409 - Conflicting Event ID",
        "description": (
            "Submit after the 201 test. "
            "Reuses the same event ID with different data."
        ),
        "value": {
            "event_id": "SWAGGER_PATIENT_TEST_001",
            "site_id": "Hospital_A",
            "event_type": "patient_registration",
            "base_priority": 1,
            "prerequisite_ids": [],
            "payload": {
                "patient_id": "DIFFERENT_PATIENT"
            }
        }
    },

    "invalid_type_422": {
        "summary": "422 - Invalid Event Type",
        "description": (
            "Rejects emergency_record because "
            "it belongs to the Emergency Service."
        ),
        "value": {
            "event_id": "SWAGGER_INVALID_TYPE_001",
            "site_id": "Hospital_A",
            "event_type": "emergency_record",
            "base_priority": 5,
            "prerequisite_ids": [],
            "payload": {
                "patient_id": "P_SWAGGER_003"
            }
        }
    }
}


# Health check
@app.get(
    "/",
    summary="Patient Service Health Check"
)
def home():
    return {
        "message": "CrisisGuard Cloud Patient Service running"
    }


@app.get(
    "/health",
    summary="Check Database Health",
    description="Checks whether the Patient Service can connect to PostgreSQL."
)
def health_check():
    try:
        database = test_connection()

        return {
            "status": "healthy",
            "service": "patient",
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


# Receive hospital patient events
@app.post(
    "/events",
    status_code=201,
    response_model=EventResponse,
    summary="Submit Patient Event",
    description=(
        "Accepts patient_registration and encounter_creation only. "
        "Prerequisites must already be applied in PostgreSQL. "
        "Identical retries receive a successful acknowledgement. "
        "Conflicting event IDs and missing prerequisites are rejected. "
        "Use the request examples to test HTTP 200, 201, 409 and 422."
    ),
    responses={
        200: {
            "description": "Identical event already applied",
            "model": EventResponse
        },
        201: {
            "description": "New event successfully applied"
        },
        409: {
            "description": (
                "Missing prerequisites or conflicting event ID"
            )
        },
        422: {
            "description": (
                "Invalid request body or unsupported event type"
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
        Body(openapi_examples=PATIENT_TEST_EXAMPLES)
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


    # Identical retry
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
    service_name="patient",
    database_check=test_connection,
)