"""Structured, privacy-safe logging. stdlib only.

Rules enforced by convention + tests, not just docs:
- never log audio bytes, transcripts, secrets, tokens, or cookie values;
- correlation uses session/turn/request ids only;
- free-form text fields are length-capped.
"""
from __future__ import annotations

import json
import logging
import sys
import time

SENSITIVE_KEYS = frozenset({
    "token", "secret", "password", "authorization", "cookie",
    "set-cookie", "audio", "transcript", "api_key", "apikey",
})

_configured = False


def _redact(obj):
    if isinstance(obj, dict):
        return {k: ("[redacted]" if k.lower() in SENSITIVE_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 500:
        return obj[:500] + "…[truncated]"
    return obj


def configure(level: str = "INFO") -> logging.Logger:
    global _configured
    logger = logging.getLogger("voice")
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        logger.propagate = False
        _configured = True
    return logger


def event(logger: logging.Logger, name: str, **fields) -> None:
    payload = {"ts": round(time.time(), 3), "event": name, **_redact(fields)}
    logger.info(json.dumps(payload, default=str))
