"""Authenticated telemetry provenance, independent of event admission/auth.

Only trusted producers receive role-specific 32+ character secrets. Missing,
invalid or ambiguous credentials leave traffic unclassified; they never reject
an event or imply that an unmarked request is live. Use TLS outside trusted local
networks and strip these headers at public ingress. Do not reuse grant tokens.
"""
import hmac
import os

KINDS = ("live", "replay")
KIND_HEADER = "X-CrisisGuard-Traffic"
TOKEN_HEADER = "X-CrisisGuard-Telemetry-Token"


def traffic_headers(kind):
    if kind not in KINDS:
        raise ValueError("Unknown telemetry traffic kind")
    token = os.getenv(f"CLOUD_{kind.upper()}_TELEMETRY_TOKEN", "")
    if len(token) < 32:
        return {}
    return {KIND_HEADER: kind, TOKEN_HEADER: token}


def classify_traffic(headers):
    kind = headers.get(KIND_HEADER, "")
    if kind not in KINDS:
        return "unclassified"
    live = os.getenv("CLOUD_LIVE_TELEMETRY_TOKEN", "")
    replay = os.getenv("CLOUD_REPLAY_TELEMETRY_TOKEN", "")
    expected = live if kind == "live" else replay
    supplied = headers.get(TOKEN_HEADER, "")
    if (len(expected) < 32 or live == replay
            or not hmac.compare_digest(supplied.encode(), expected.encode())):
        return "unclassified"
    # A purported live request carrying a replay grant is contradictory.
    if kind == "live" and headers.get("X-MetaGuard-Grant"):
        return "unclassified"
    return kind
