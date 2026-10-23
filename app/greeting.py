"""Deterministic social-reply fast path for the voice service.

This handles greetings and short pleasantries ("hello", "good morning",
"how are you?", "thank you") without a remote backend round trip. It also
recognises a narrow repair form ("I said, how are you doing?") so a repeated
question gets an acknowledgement and an answer instead of the booking prompt.

Strictness is the point: the entire normalized transcript must match one of
the patterns below. Any message with extra content — including
"Hello, can you explain the architecture?" — falls through to the responder.
There is no classifier and no LLM, and a low-confidence/corrupted transcript
is never treated as social conversation.

These replies are deliberately booking-free. They are safe in booking-only
mode because the booking guardrail still owns any turn that asks about
scheduling; social replies must not smuggle in an outbound contact channel.
"""
from __future__ import annotations

import re

_GREETING = r"(?:hello|hi|hey|hiya|howdy|yo|sup|greetings)(?: there)?(?: james)?"
_HOW_ARE_YOU = (
    r"(?:(?:how are you|howre you|how are ya)(?: doin(?:g)?)?"
    r"(?: today| this morning| this afternoon| this evening| these days| lately)?|"
    r"how (?:you|ya) doin(?:g)?(?: today| this morning| these days| lately)?|"
    r"how (?:have )?you been(?: doin(?:g)?| lately)?|"
    r"how is it going|hows it going|how are things|whats up|what is up|how do you do)"
)
_THANKS = (
    r"(?:(?:thanks|thank you)(?: so much| a lot| very much)?"
    r"(?: for (?:the|your) help)?|many thanks|much appreciated|appreciate it|cheers)"
)
_NICE_TO_MEET = (
    r"(?:(?:nice|pleased) to meet you(?: too)?(?: james)?|"
    r"good to see you(?: james)?|good to hear from you)"
)
_FEELING = (
    r"(?:(?:im|i am)(?: doing)? (?:good|well|fine|great|okay|ok|alright)"
    r"(?: thanks| thank you)?|"
    r"(?:good|well|fine|great|okay|ok|alright)(?: thanks| thank you)|"
    r"not bad(?: thanks)?|cant complain|all good(?: thanks)?|"
    r"pretty good(?: thanks)?|doing (?:well|good|fine|great)(?: thanks| thank you)?|"
    r"so far so good)"
)

_HELLO_REPLY = "Hi! How can I help you today?"
_HOW_REPLY = "I'm doing well, thanks. How are you?"
_HELLO_HOW_REPLY = "Hi! I'm doing well, thanks. How are you?"
_MORNING_REPLY = "Good morning! How can I help you today?"
_AFTERNOON_REPLY = "Good afternoon! How can I help you today?"
_EVENING_REPLY = "Good evening! How can I help you today?"
_MORNING_HOW_REPLY = "Good morning! I'm doing well, thanks. How are you?"
_AFTERNOON_HOW_REPLY = "Good afternoon! I'm doing well, thanks. How are you?"
_EVENING_HOW_REPLY = "Good evening! I'm doing well, thanks. How are you?"
_FEELING_REPLY = "Glad to hear it. How can I help?"
_THANKS_REPLY = "You're welcome!"
_NICE_REPLY = "Nice to meet you too. How can I help?"

# Order matters: compound greetings before the bare greeting, and time
# greetings before the generic "how are you" pattern.
_SOCIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"{_THANKS}"), _THANKS_REPLY),
    (re.compile(rf"{_NICE_TO_MEET}"), _NICE_REPLY),
    (re.compile(rf"{_GREETING} {_HOW_ARE_YOU}"), _HELLO_HOW_REPLY),
    (re.compile(rf"(?:good )?morning(?: james)? {_HOW_ARE_YOU}"), _MORNING_HOW_REPLY),
    (re.compile(rf"(?:good )?afternoon(?: james)? {_HOW_ARE_YOU}"), _AFTERNOON_HOW_REPLY),
    (re.compile(rf"(?:good )?evening(?: james)? {_HOW_ARE_YOU}"), _EVENING_HOW_REPLY),
    (re.compile(_FEELING), _FEELING_REPLY),
    (re.compile(_HOW_ARE_YOU), _HOW_REPLY),
    (re.compile(r"(?:good )?morning(?: james)?"), _MORNING_REPLY),
    (re.compile(r"(?:good )?afternoon(?: james)?"), _AFTERNOON_REPLY),
    (re.compile(r"(?:good )?evening(?: james)?"), _EVENING_REPLY),
    (re.compile(_GREETING), _HELLO_REPLY),
)

#: Leading phrases that only restate an earlier question. We strip one, then
#: require the remainder to be an exact social phrase; this cannot turn an
#: unrelated factual question into a social reply.
_REPAIR_PREFIXES = (
    "i said",
    "i asked",
    "i was asking",
    "i just asked",
    "i literally asked",
    "thats not what i asked",
    "that is not what i asked",
    "you didnt answer",
    "you did not answer",
)

_REPAIR_REPLY_PREFIX = "You're right, I missed that. "
_NORMALIZE_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")

#: A corrupted/uncertain transcript must never be treated as a greeting. This
#: is a Whisper segment log-probability floor (higher = more confident).
MIN_AVG_LOGPROB = -0.9


def normalize(text: str) -> str:
    """Lowercase, drop apostrophes, strip other punctuation, collapse spaces."""
    lowered = (text or "").lower().replace("'", "").replace("\u2019", "")
    return _WS_RE.sub(" ", _NORMALIZE_RE.sub(" ", lowered)).strip()


def _social_reply(key: str) -> str | None:
    for pattern, reply in _SOCIAL_PATTERNS:
        if pattern.fullmatch(key):
            return reply
    return None


def _strip_repair_prefix(key: str) -> tuple[bool, str]:
    for prefix in _REPAIR_PREFIXES:
        if key.startswith(prefix + " "):
            return True, key[len(prefix) + 1 :].strip()
    return False, key


def greeting_reply(text: str, avg_logprob: float | None = None) -> str | None:
    """Return a spoken social reply, or None to use the normal RAG path.

    Strict: the whole message must match a known social phrase, optionally
    preceded by a recognised repair phrase. Any extra words disqualify it.
    `avg_logprob` (if provided) gates out low-confidence text."""
    key = normalize(text)
    if not key or len(key) > 80:
        return None
    if avg_logprob is not None and avg_logprob < MIN_AVG_LOGPROB:
        return None
    repaired, base = _strip_repair_prefix(key)
    reply = _social_reply(base)
    if reply is None:
        return None
    return f"{_REPAIR_REPLY_PREFIX}{reply}" if repaired else reply


def is_greeting(text: str, avg_logprob: float | None = None) -> bool:
    return greeting_reply(text, avg_logprob) is not None
