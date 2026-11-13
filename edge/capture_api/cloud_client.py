import os
from urllib.parse import quote

import httpx

from cloud.traffic import traffic_headers

# Which cloud service owns each event type.
# Must match the event_type values each cloud API accepts.
EVENT_ROUTES = {
    "patient_registration": "patient",
    "encounter_creation": "patient",
    "emergency_record": "emergency",
    "patient_discharge": "admin",
}

SUPPORTED_EVENT_TYPES = frozenset(EVENT_ROUTES)


def get_service_urls():
    """
    Read the three cloud /events URLs from the environment.

    CLOUD_URL is still accepted as the Patient URL so older
    .env files and the smoke test keep working.
    """
    return {
        "patient": (
            os.getenv("PATIENT_URL")
            or os.getenv("CLOUD_URL")
            or "http://127.0.0.1:8001/events"
        ),
        "emergency": (
            os.getenv("EMERGENCY_URL")
            or "http://127.0.0.1:8003/events"
        ),
        "admin": (
            os.getenv("ADMIN_URL")
            or "http://127.0.0.1:8004/events"
        ),
    }


def get_event_url(event_type):
    """Return the /events URL for an event type, or None if unknown."""
    service = EVENT_ROUTES.get(event_type)
    if service is None:
        return None
    return get_service_urls()[service]


def _health_url(events_url):
    """http://host:8001/events -> http://host:8001/health"""
    base = events_url.rstrip("/")
    if base.endswith("/events"):
        base = base[: -len("/events")]
    return f"{base}/health"


def send_to_cloud(event, *, grant_id=None):
    url = get_event_url(event.get("event_type"))

    # Unknown types can never succeed, so report them as a
    # permanent 422 failure. Scheduled replay quarantines these.
    if url is None:
        return {
            "status": "http_error",
            "status_code": 422,
            "error": (
                f"Unsupported event_type for routing: "
                f"{event.get('event_type')!r}"
            ),
        }

    headers = traffic_headers("replay")
    if grant_id:
        headers.update({"X-MetaGuard-Grant": grant_id,
                        "Authorization": "Bearer " + os.getenv("HOSPITAL_CONTROLLER_TOKEN", "")})
    try:
        response = httpx.post(
            url,
            json=event,
            timeout=5.0,
            **({"headers": headers} if headers else {})
        )

        response.raise_for_status()

        return response.json()

    except httpx.RequestError as error:
        return {
            "status": "connection_failed",
            "error": str(error)
        }

    except httpx.HTTPStatusError as error:
        return {
            "status": "http_error",
            "status_code": error.response.status_code,
            "error": error.response.text
        }


def check_cloud_health(timeout=2.0):
    """
    Return {"patient": bool, "emergency": bool, "admin": bool}.
    True means that service's /health answered 200.
    """
    health = {}

    for service, events_url in get_service_urls().items():
        try:
            response = httpx.get(
                _health_url(events_url),
                timeout=timeout
            )
            health[service] = response.status_code == 200
        except httpx.RequestError:
            health[service] = False

    return health


def get_cloud_event_status(event, timeout=3.0):
    """
    Ask the owning cloud service whether an event was applied.

    Returns one of:
      {"state": "applied", "site_id": ...}
      {"state": "not_found"}
      {"state": "unknown", "error": ...}   (cloud unreachable or error)
    """
    url = get_event_url(event.get("event_type"))
    if url is None:
        return {"state": "unknown", "error": "unsupported event_type"}

    headers = traffic_headers("replay")
    event_id = event["event_id"]
    # Keep ordinary IDs compatible with older Cloud services. Query parameters
    # preserve reserved characters and dot segments without path normalization.
    query_lookup = quote(event_id, safe="") != event_id or event_id in (".", "..")
    lookup_url = url.rstrip("/") if query_lookup else f"{url.rstrip('/')}/{event_id}"
    try:
        response = httpx.get(
            lookup_url,
            timeout=timeout,
            **({"params": {"event_id": event_id}} if query_lookup else {}),
            **({"headers": headers} if headers else {})
        )
    except httpx.RequestError as error:
        return {"state": "unknown", "error": str(error)}

    if response.status_code == 404:
        return {"state": "not_found"}

    if response.status_code != 200:
        return {
            "state": "unknown",
            "error": f"HTTP {response.status_code}"
        }

    body = response.json()

    if body.get("status") == "applied":
        return {"state": "applied", "site_id": body.get("site_id")}

    return {"state": "unknown", "error": f"cloud status {body.get('status')!r}"}



from edge.database.db import (
    get_pending_events,
    mark_event_applied
)


def replay_pending_events():
    pending_events = get_pending_events()

    results = []

    for event in pending_events:
        response = send_to_cloud(event)

        if (
            response.get("status") == "applied"
            and response.get("event_id") == event["event_id"]
        ):
            updated = mark_event_applied(event["event_id"])

            results.append({
                "event_id": event["event_id"],
                "status": "applied" if updated else "not_updated"
            })
        else:
            results.append({
                "event_id": event["event_id"],
                "status": "pending",
                "response": response
            })

    return results
