"""Verify short-lived, project-scoped widget tokens (HS256 JWT shape).

The portfolio (or any integrating backend) signs the token with a shared
secret; the browser only ever sees the signed value. Verification is
stdlib-only and must match ``lib/widget/token.ts`` byte for byte.

Claims: { project, origin, scope, aud: "widget", iat, exp, jti }
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

WIDGET_TOKEN_AUDIENCE = "widget"
WIDGET_SCOPE_VOICE = "widget:voice"


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _sign(secret: str, data: str) -> str:
    digest = hmac.new(secret.encode(), data.encode(), hashlib.sha256).digest()
    return _b64url_encode(digest)


def verify_widget_token(
    secret: str | None,
    token: object,
    *,
    now: float | None = None,
    audience: str = WIDGET_TOKEN_AUDIENCE,
    required_scope: str = WIDGET_SCOPE_VOICE,
) -> dict[str, Any] | None:
    """Return validated claims, or None for any malformed/expired/wrong token."""
    import time

    if not secret or len(secret) < 32 or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_part, payload_part, signature_part = parts
    try:
        header = json.loads(_b64url_decode(header_part))
        payload = json.loads(_b64url_decode(payload_part))
    except (ValueError, TypeError):
        return None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        return None
    expected = _sign(secret, f"{header_part}.{payload_part}")
    if not isinstance(signature_part, str):
        return None
    try:
        # Compare bytes: compare_digest(str, str) raises on non-ASCII input,
        # and a malformed header must fail closed rather than 500.
        if not hmac.compare_digest(expected.encode("ascii"), signature_part.encode("utf-8")):
            return None
    except (UnicodeEncodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    current = time.time() if now is None else now
    exp = payload.get("exp")
    iat = payload.get("iat")
    if not isinstance(exp, (int, float)) or exp + 30 < current:
        return None
    if not isinstance(iat, (int, float)) or iat > current + 60:
        return None
    if payload.get("aud") != audience:
        return None
    project = payload.get("project")
    origin = payload.get("origin")
    scope = payload.get("scope")
    if not isinstance(project, str) or not project:
        return None
    if not isinstance(origin, str):
        return None
    if not isinstance(scope, str):
        return None
    if required_scope and required_scope not in scope.split():
        return None
    return payload
