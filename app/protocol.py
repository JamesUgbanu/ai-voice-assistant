"""Browser ↔ voice-service protocol (v1).

Transport: WebSocket for interactive audio, HTTPS+JSON for session
management. Every message is a JSON envelope:

  {"v": 1, "type": "<event>", "seq": <int>, "turn_id": <str|null>, "payload": {...}}

Rules:
- `type` is required and must be known; unknown types are rejected.
- `seq` is a client-send monotonic counter per connection; the server
  echoes the highest applied seq for ordering diagnostics only.
- `turn_id` correlates one user turn end-to-end (STT → RAG → TTS →
  playback). Late results for a superseded turn are discarded.
- Audio frames carry base64 PCM16 mono 16000 Hz (`audio_b64`), plus
  `final: bool` on the last frame of an utterance. Anything else is
  rejected — never passed to a decoder blindly.
- Total inbound message size is capped (see Settings.max_message_bytes);
  audio frames additionally capped (max_audio_frame_bytes) and total
  audio per turn capped (max_turn_audio_seconds).

Client → server: hello, audio_frame, end_utterance, interrupt,
                 ping, end_session
Server → client: session_created, listening, transcript_partial,
                 transcript_final, rag_started, audio_frame, turn_end,
                 quota, error, pong, session_ended

This module validates inbound envelopes without any framework or audio
dependency so the contract is unit-testable on stdlib alone.
"""
from __future__ import annotations

import base64
import binascii

PROTOCOL_VERSION = 1

CLIENT_EVENTS = frozenset({
    "hello",
    "audio_frame",
    "end_utterance",
    "interrupt",
    "ping",
    "end_session",
    # Client confirms its playback queue drained (used to gate auto-end so the
    # closing goodbye is heard, not cut off).
    "playback_complete",
})

SERVER_EVENTS = frozenset({
    "session_created",
    "listening",
    "transcript_partial",
    "transcript_final",
    "rag_started",
    "audio_frame",
    "turn_end",
    "quota",
    "error",
    "pong",
    "session_ended",
})

#: Optional server features advertised in the `session_created` payload
#: (`capabilities`). Clients MUST gate feature-specific client→server events on
#: these, so an older server and a newer client never exchange an unknown event
#: (which the strict envelope validator would reject as `bad_message`).
SERVER_CAPABILITIES = frozenset({
    "playback_complete",  # client can confirm playback drained (auto-end gate)
})


# Recoverable (client may continue) vs fatal (server closes after sending).
RECOVERABLE_ERRORS = frozenset({
    "bad_message",
    "audio_too_large",
    "turn_too_long",
    "stt_failed",
    "rag_failed",
    "tts_failed",
    "stale_turn",
})

FATAL_ERRORS = frozenset({
    "unauthorized",
    "session_expired",
    "session_not_found",
    "quota_exhausted",
    "call_limit_reached",
    "server_error",
})


class ProtocolError(ValueError):
    """Raised for any inbound message that violates the contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _require(condition: bool, code: str, message: string) -> None:
    if not condition:
        raise ProtocolError(code, message)


def validate_envelope(msg: object, *, max_bytes: int, from_client: bool) -> dict:
    """Validate a decoded JSON envelope. Returns the envelope on success."""
    _require(isinstance(msg, dict), "bad_message", "message must be a JSON object")
    assert isinstance(msg, dict)
    _require(msg.get("v") == PROTOCOL_VERSION, "bad_message", "unsupported protocol version")
    event_type = msg.get("type")
    allowed = CLIENT_EVENTS if from_client else SERVER_EVENTS
    _require(isinstance(event_type, str) and event_type in allowed, "bad_message", f"unknown event type: {event_type!r:.40}")
    seq = msg.get("seq", 0)
    _require(isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0, "bad_message", "seq must be a non-negative int")
    turn_id = msg.get("turn_id")
    _require(
        turn_id is None or (isinstance(turn_id, str) and 1 <= len(turn_id) <= 64),
        "bad_message",
        "turn_id must be null or a short string",
    )
    payload = msg.get("payload", {})
    _require(isinstance(payload, dict), "bad_message", "payload must be an object")
    _require(max_bytes > 0, "bad_message", "server misconfigured: non-positive size cap")
    return {"v": 1, "type": event_type, "seq": seq, "turn_id": turn_id, "payload": payload}


def validate_audio_frame(payload: dict, *, max_frame_bytes: int, max_turn_bytes: int, turn_bytes_so_far: int) -> bytes:
    """Validate one audio_frame payload; returns raw PCM16 bytes."""
    b64 = payload.get("audio_b64")
    _require(isinstance(b64, str) and b64, "bad_message", "audio_frame requires audio_b64")
    assert isinstance(b64, str)
    _require(len(b64) <= (max_frame_bytes * 4) // 3 + 8, "audio_too_large", "audio frame exceeds size cap")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ProtocolError("bad_message", "audio_b64 is not valid base64")
    _require(len(raw) > 0, "bad_message", "audio frame is empty")
    _require(len(raw) <= max_frame_bytes, "audio_too_large", "audio frame exceeds size cap")
    _require(len(raw) % 2 == 0, "bad_message", "PCM16 frames must be an even byte count")
    if turn_bytes_so_far + len(raw) > max_turn_bytes:
        raise ProtocolError("turn_too_long", "turn audio budget exceeded")
    return raw


def make_envelope(event_type: str, payload: dict | None = None, *, seq: int = 0, turn_id: str | None = None) -> dict:
    """Build an outbound envelope (server side)."""
    if event_type not in SERVER_EVENTS:
        raise ProtocolError("bad_message", f"unknown server event type: {event_type!r:.40}")
    return {"v": PROTOCOL_VERSION, "type": event_type, "seq": seq, "turn_id": turn_id, "payload": payload or {}}


def is_fatal_error(code: string) -> bool:
    return code in FATAL_ERRORS


#: Minimum seconds between answered pings. Session liveness is rechecked
#: on heartbeats regardless — this only stops a ping flood from becoming
#: a database flood, since each answered ping touches the session row.
PING_MIN_INTERVAL_SECONDS = 1.0


def should_answer_ping(last_answered_at: float, now: float) -> bool:
    """Throttle ping responses without affecting liveness checks."""
    return now - last_answered_at >= PING_MIN_INTERVAL_SECONDS
