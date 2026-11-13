
from contextlib import asynccontextmanager
import os
import asyncio
import logging
from uuid import UUID
import hmac
from fastapi import Header

from fastapi import FastAPI, HTTPException, Request
from cloud.metaguard import hospital_identity
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from psycopg import OperationalError

from cloud.controller.storage import ControllerStorage
from cloud.controller import admission
from cloud.controller.experimental import controller_settings


storage = ControllerStorage(
    bypass_limit=controller_settings()[1],
    expiry_seconds=300
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create controller tables when the API starts.
    controller_settings()
    storage.init_db()
    admission.settings()  # Reject unsafe configuration before accepting requests.
    task = asyncio.create_task(_reconcile_loop()) if os.getenv("CONTROLLER_HOSPITAL_TOKENS") else None
    metrics_task = asyncio.create_task(_admission_loop()) if admission.enabled() else None
    try:
        yield
    finally:
        for running in (task, metrics_task):
            if not running:
                continue
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="CrisisGuard Controller",
    lifespan=lifespan
)


async def _admission_loop():
    while True:
        try:
            await asyncio.to_thread(admission.poll, storage)
        except Exception:
            logging.getLogger(__name__).warning('Admission telemetry unavailable; conservative limits remain')
        await asyncio.sleep(min(1, admission.settings().poll_seconds))


async def _reconcile_loop():
    while True:
        try:
            await asyncio.to_thread(storage.reconcile_cloud_outcomes)
        except Exception:
            logging.getLogger(__name__).warning('Cloud reconciliation unavailable; will retry')
        await asyncio.sleep(5)


@app.middleware('http')
async def authenticate(request: Request, call_next):
    if request.url.path == '/health':
        return await call_next(request)
    expected = os.getenv('CONTROLLER_CLOUD_TOKEN')
    supplied = request.headers.get('x-cloud-token')
    request.state.operator = bool(expected and supplied and hmac.compare_digest(expected.encode(), supplied.encode()))
    try:
        request.state.hospital = None if request.state.operator else hospital_identity(request.headers.get('authorization'))
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})
    return await call_next(request)


def _owner(request, hospital):
    if not request.state.operator and request.state.hospital != hospital:
        raise HTTPException(403, 'Hospital identity mismatch')


def _operator(request):
    if not request.state.operator:
        raise HTTPException(403, 'Trusted Cloud credentials required')


def _grant_owner(request, grant_id):
    grant_id = _validate_grant_id(grant_id)
    if not request.state.operator:
        grant = storage.get_grant_status(grant_id)
        if grant is None:
            raise HTTPException(404, 'Grant not found')
        _owner(request, grant['hospital_id'])
    return grant_id


def _validate_grant_id(grant_id):
    try:
        return str(UUID(grant_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, 'Invalid grant ID') from None


@app.exception_handler(OperationalError)
async def database_unavailable(request, exc):
    return JSONResponse(status_code=503, content={"detail": "Controller database unavailable"})


class TicketRequest(BaseModel):
    hospital_id: str = Field(min_length=1, pattern=r"\S")
    event_id: str = Field(min_length=1, pattern=r"\S")
    # Effective priorities can exceed the base event priority range (ageing).
    priority: int = Field(strict=True, ge=1, le=2147483647)
    event_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get('/admission/status')
def admission_status(request: Request):
    _operator(request)
    return storage.admission_status()


@app.post("/tickets")
def register_ticket(ticket: TicketRequest, request: Request):
    _owner(request, ticket.hospital_id)
    if not request.state.operator and ticket.event_digest is None:
        raise HTTPException(422, "Event digest required")
    added = storage.add_ticket(
        ticket.hospital_id,
        ticket.event_id,
        ticket.priority,
        **({"event_digest": ticket.event_digest} if ticket.event_digest is not None else {})
    )

    return {
        "message": (
            "Ticket submitted"
            if added else "Ticket not added"
        ),
        "hospital_id": ticket.hospital_id,
        "added": added
    }


@app.get("/tickets")
def view_tickets(request: Request):
    _operator(request)
    return storage.list_tickets()


@app.post("/grants/next")
def next_grant(request: Request):
    _operator(request)
    # Repair unacknowledged expired grants before new allocation.
    storage.reconcile_expired()
    grant = storage.issue_grant()

    if grant is None:
        return {"message": "No eligible hospitals"}

    return grant


class GrantAction(BaseModel):
    hospital_id: str = Field(min_length=1, pattern=r"\S")


@app.get('/hospitals/{hospital_id}/grant')
@app.get('/hospitals/{hospital_id}/events/{event_id:path}/grant')
def latest_grant(hospital_id: str, event_id: str, request: Request):
    _owner(request, hospital_id)
    return {'grant': storage.latest_grant(hospital_id, event_id)}


@app.post('/hospitals/{hospital_id}/grants/next')
def hospital_next_grant(hospital_id: str, request: Request):
    _owner(request, hospital_id)
    storage.reconcile_expired()
    feedback = {} if _retry_feedback(request) else None
    grant = storage.issue_grant(admission_feedback=feedback) if feedback is not None else storage.issue_grant()
    # Allocation follows the shared fairness policy, never caller preference.
    if grant and grant['hospital_id'] == hospital_id:
        return grant
    return {'waiting': True, **({'admission_deferral': feedback} if feedback else {})}


@app.post('/grants/{grant_id}/reconcile')
def reconcile_cloud_grant(grant_id: str, body: GrantAction, request: Request):
    _owner(request, body.hospital_id)
    grant_id = _grant_owner(request, grant_id)
    return {'status': storage.reconcile_authoritative(grant_id, body.hospital_id)}


@app.post('/grants/{grant_id}/abandon')
def abandon_cloud_grant(grant_id: str, body: GrantAction, request: Request):
    _owner(request, body.hospital_id)
    grant_id = _grant_owner(request, grant_id)
    return {'status': storage.reconcile_authoritative(grant_id, body.hospital_id, abandon=True)}


class CandidateRequest(BaseModel):
    event_id: str | None = Field(default=None, min_length=1, pattern=r"\S")
    priority: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    event_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode='after')
    def complete_candidate(self):
        if self.event_id is None:
            if self.priority is not None or self.event_digest is not None:
                raise ValueError('Withdrawal must not include priority or digest')
        elif self.priority is None or self.event_digest is None:
            raise ValueError('Candidate requires priority and event digest')
        return self


@app.post('/hospitals/{hospital_id}/candidate')
def hospital_candidate(hospital_id: str, body: CandidateRequest, request: Request):
    _owner(request, hospital_id)
    return storage.set_candidate(hospital_id, body.event_id, body.priority, body.event_digest)


class PriorityRequest(BaseModel):
    priority: int = Field(strict=True, ge=1, le=2147483647)


@app.post('/hospitals/{hospital_id}/priority')
@app.post('/hospitals/{hospital_id}/events/{event_id:path}/priority')
def hospital_priority(hospital_id: str, event_id: str, body: PriorityRequest, request: Request):
    _owner(request, hospital_id)
    return {'updated': storage.update_priority(hospital_id, event_id, body.priority)}


def _retry_feedback(request):
    return (os.getenv('CRISISGUARD_EXPERIMENTAL_POLICIES') == '1'
            and request.headers.get('X-CrisisGuard-Retry-Policy') == 'bounded-deferral')


def _result(value, feedback=None):
    if value == 'admission_deferred':
        headers = {'Retry-After': '1'}
        if feedback:
            headers.update({'Retry-After': str(feedback['retry_after_seconds']),
                            'X-CrisisGuard-Admission-Reason': feedback['reason']})
        raise HTTPException(status_code=429, detail='Replay admission deferred', headers=headers)
    if value == 'not_found':
        raise HTTPException(status_code=404, detail='Grant not found for hospital')
    if value in ('invalid_state', 'expired'):
        raise HTTPException(status_code=409, detail=f'Invalid grant state: {value}')
    return {'status': value}


@app.post('/grants/{grant_id}/acknowledge')
def acknowledge_grant(grant_id: str, body: GrantAction, request: Request):
    _owner(request, body.hospital_id)
    grant_id = _grant_owner(request, grant_id)
    feedback = {} if _retry_feedback(request) else None
    result = (storage.acknowledge_grant(grant_id, body.hospital_id, admission_feedback=feedback)
              if feedback is not None else storage.acknowledge_grant(grant_id, body.hospital_id))
    return _result(result, feedback)


def _require_cloud_token(token):
    # No completion or reconciliation without an explicitly configured secret.
    expected = os.getenv('CONTROLLER_CLOUD_TOKEN')
    if not expected or not token or not hmac.compare_digest(expected.encode(), token.encode()):
        raise HTTPException(status_code=403, detail='Trusted Cloud token required')


@app.post('/grants/{grant_id}/complete')
def complete_grant(grant_id: str, body: GrantAction, request: Request,
                   x_cloud_token: str | None = Header(default=None)):
    _owner(request, body.hospital_id)
    grant_id = _grant_owner(request, grant_id)
    if not request.state.operator:
        return {"status": storage.reconcile_authoritative(grant_id, body.hospital_id, complete_only=True)}
    _require_cloud_token(x_cloud_token)
    return _result(storage.complete_grant(grant_id, body.hospital_id))


@app.post('/grants/{grant_id}/cancel')
def cancel_grant(grant_id: str, body: GrantAction, request: Request):
    _owner(request, body.hospital_id)
    grant_id = _grant_owner(request, grant_id)
    return _result(storage.cancel_grant(grant_id, body.hospital_id))


@app.get('/grants/by-id/{grant_id}')
def grant_status(grant_id: str, request: Request):
    grant_id = _grant_owner(request, grant_id)
    result = storage.get_grant_status(grant_id)
    if result is None:
        raise HTTPException(status_code=404, detail='Grant not found')
    return result


@app.get("/grants/{hospital_id}/{event_id}")
def retrieve_grant(hospital_id: str, event_id: str, request: Request):
    _owner(request, hospital_id)
    grant = storage.get_grant(hospital_id, event_id)

    if grant is None:
        raise HTTPException(
            status_code=404,
            detail="No active grant found"
        )

    return grant


@app.post('/grants/reconcile-expired')
def reconcile_expired(x_cloud_token: str | None = Header(default=None)):
    _require_cloud_token(x_cloud_token)
    return storage.reconcile_expired()


class Reconciliation(BaseModel):
    applied: bool


@app.post('/grants/{grant_id}/resolve')
def resolve_grant(grant_id: str, body: Reconciliation,
                  x_cloud_token: str | None = Header(default=None)):
    _require_cloud_token(x_cloud_token)
    grant_id = _validate_grant_id(grant_id)
    result = storage.resolve_uncertain(grant_id, body.applied)
    # Here expired means successful authoritative resolution, not a late ACK.
    if result == 'expired':
        return {'status': result}
    return _result(result)
