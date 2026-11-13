
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from uuid import uuid4


@dataclass
class Grant:
    grant_id: str
    hospital_id: str
    event_id: str
    expires_at: datetime


class GrantManager:
    def __init__(self, scheduler, expiry_seconds=30):
        if expiry_seconds <= 0:
            raise ValueError("Expiry must be positive")
        self.scheduler = scheduler
        self.expiry_seconds = expiry_seconds
        self.grants = {}
        self.lock = Lock()

    def issue_grant(self):
        # Protect selection and grant creation
        with self.lock:
            ticket = self.scheduler.next_hospital()

            # Match persistent storage: an issued event cannot receive a
            # second grant, even after expiry. Retry lifecycle is separate.
            while ticket is not None and (ticket.hospital_id, ticket.event_id) in self.grants:
                ticket = self.scheduler.next_hospital()

            if ticket is None:
                return None

            grant = Grant(
                grant_id=str(uuid4()),
                hospital_id=ticket.hospital_id,
                event_id=ticket.event_id,
                expires_at=(
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self.expiry_seconds)
                )
            )

            # Store grant by hospital and event ID
            key = (ticket.hospital_id, ticket.event_id)
            self.grants[key] = grant

            return grant

    def get_grant(self, hospital_id, event_id):
        with self.lock:
            key = (hospital_id, event_id)
            grant = self.grants.get(key)

            if grant is None:
                return None

            # Do not return expired grants
            if datetime.now(timezone.utc) >= grant.expires_at:
                return None

            return grant
