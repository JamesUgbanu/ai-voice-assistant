"""Voice-service authentication.

No shared database, no portfolio imports, no login system. Two mechanisms:

1. Browser tickets: HMAC-SHA256(session_id, expiry) under VOICE_HMAC_SECRET.
   Short-lived (120s), single purpose (open exactly one WebSocket), verified
   without any lookup. The ticket proves this service authorized the session
   moments ago — nothing more.

2. Server-to-server: the portfolio accepts a static bearer token
   (RAG_SERVICE_TOKEN there, VOICE_RAG_SERVICE_TOKEN here). Constant-time
   comparison is the portfolio's job; here we only ensure the value is
   configured and never logged.

Visitor identity is an opaque random id stored in a signed HttpOnly cookie;
usage state lives server-side in SQLite. A stolen cookie can at most burn
the victim's own quota — sessions still cap at one active and five minutes.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

TICKET_TTL_SECONDS = 120

#: HttpOnly session cookie binding a browser to its anonymous visitor id.
COOKIE_NAME = "voice_vid"


def new_visitor_id() -> str:
    return secrets.token_urlsafe(24)


def is_valid_id(value: object, *, min_len: int = 16, max_len: int = 128) -> bool:
    return isinstance(value, str) and min_len <= len(value) <= max_len and value.isascii() and value.isprintable()


def issue_ticket(secret: str, session_id: str, expires_at: float) -> str:
    """ticket = base64url(session_id) . base64url(expiry) . base64url(hmac)."""
    if not secret:
        raise ValueError("HMAC secret is required")
    body = f"{session_id}.{int(expires_at)}".encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    enc = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return f"{enc(body)}.{enc(sig)}"


def verify_ticket(secret: str, ticket: object, *, now: float, max_age_seconds: int = TICKET_TTL_SECONDS) -> str | None:
    """Returns the session_id on success, else None. Never raises, never logs."""
    if not secret or not isinstance(ticket, str):
        return None
    try:
        body_b64, sig_b64 = ticket.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        body = base64.urlsafe_b64decode(pad(body_b64))
        sig = base64.urlsafe_b64decode(pad(sig_b64))
    except (ValueError, binascii.Error):
        return None
    expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        session_id, exp = body.decode().rsplit(".", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    try:
        expiry = int(exp)
    except ValueError:
        return None
    if not (now <= expiry <= now + max_age_seconds):
        return None
    if not is_valid_id(session_id, min_len=1, max_len=128):
        return None
    return session_id


def hash_ip(ip: str, salt: str) -> str:
    """Salted SHA-256 for attempt buckets. Raw IPs never touch the database."""
    return hashlib.sha256(f"{salt}|{ip}".encode()).hexdigest()[:32]
