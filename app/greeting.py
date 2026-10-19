"""Deterministic greeting fast path for the voice service.

A tiny, strict matcher: the entire finalized transcript must normalize to an
exact known greeting. This lets "hello" get a spoken reply immediately without
a remote backend round trip, while any message with extra content — including
"Hello, can you explain the architecture?" — falls through to the responder.

The reply strings below are examples from the reference assistant; edit them
for your own persona (or remove phrases you want sent to the backend).
No classifier, no LLM: exact-string membership after normalization. A
low-confidence/corrupted transcript is never treated as a greeting.
"""
from __future__ import annotations

import re

#: Whole-message greetings → canonical reply (spoken in the cloned voice).
_GREETINGS: dict[str, str] = {}

def _add(reply: str, *phrases: str) -> None:
    for p in phrases:
        _GREETINGS[p] = reply


_HELLO = "Hey, good to hear from you. What would you like to know about my work?"
_HOW_ARE_YOU = "I'm doing well, thanks for asking. What can I tell you about my work?"
_NICE = "Nice to meet you too. What can I tell you about my work?"
_MORNING = "Good morning! What would you like to know about my work?"
_AFTERNOON = "Good afternoon! What would you like to know about my work?"
_EVENING = "Good evening! What would you like to know about my work?"

_add(_HELLO, "hello", "hi", "hey", "yo", "sup", "howdy", "hiya", "greetings",
     "hello there", "hi there", "hey there",
     "hello james", "hi james", "hey james",
     "hello there james", "hi there james", "hey there james")
_add(_HOW_ARE_YOU, "hello how are you", "hi how are you", "hey how are you",
     "how are you", "how are you doing", "how is it going", "hows it going",
     "how are things", "whats up", "what is up")
_add(_NICE, "nice to meet you", "nice to meet you james", "pleased to meet you")
_add(_MORNING, "good morning", "morning")
_add(_AFTERNOON, "good afternoon", "afternoon")
_add(_EVENING, "good evening", "evening")

_NORMALIZE_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")

#: A corrupted/uncertain transcript must never be treated as a greeting. This
#: is a Whisper segment log-probability floor (higher = more confident).
MIN_AVG_LOGPROB = -0.9


def normalize(text: str) -> str:
    """Lowercase, drop apostrophes, strip other punctuation, collapse spaces."""
    lowered = (text or "").lower().replace("'", "").replace("\u2019", "")
    return _WS_RE.sub(" ", _NORMALIZE_RE.sub(" ", lowered)).strip()


def greeting_reply(text: str, avg_logprob: float | None = None) -> str | None:
    """Return a spoken greeting reply, or None to use the normal RAG path.

    Strict: the whole message must equal a known greeting; any extra words
    disqualify it. `avg_logprob` (if provided) gates out low-confidence text."""
    key = normalize(text)
    if not key or len(key) > 40:
        return None
    if avg_logprob is not None and avg_logprob < MIN_AVG_LOGPROB:
        return None
    return _GREETINGS.get(key)


def is_greeting(text: str, avg_logprob: float | None = None) -> bool:
    return greeting_reply(text, avg_logprob) is not None
