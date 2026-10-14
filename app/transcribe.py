"""Composer dictation: audio validation and lightweight rate limiting.

Pure, stdlib-only helpers so the HTTP layer in main.py stays thin and these
rules are unit-testable without FastAPI. The audio contract mirrors the voice
WebSocket: raw PCM16 mono 16 kHz (exactly what the browser worklet produces and
what faster-whisper expects), so no decode/resample step is needed.
"""
from __future__ import annotations

#: Ticket subjects carry this prefix so a dictation ticket can never be
#: presented as a voice-session ticket (the WS handler looks the id up in the
#: session store and rejects a "dict:…" subject anyway).
DICTATION_SUBJECT_PREFIX = "dict:"


class TranscribeError(Exception):
    """Validation failure with a stable code for the HTTP status mapping."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_transcribe_audio(
    body: bytes,
    *,
    max_bytes: int,
    min_bytes: int = 1600,  # ~50 ms at 16 kHz mono PCM16
) -> bytes:
    """Validate raw PCM16 mono 16 kHz audio. Returns it unchanged or raises
    TranscribeError('too_large'|'empty'|'bad_audio')."""
    if len(body) > max_bytes:
        raise TranscribeError("too_large", "recording is longer than allowed")
    if len(body) < min_bytes:
        raise TranscribeError("empty", "no audio captured")
    if len(body) % 2 != 0:
        raise TranscribeError("bad_audio", "PCM16 frames must have an even byte count")
    # A pure-silence buffer is treated as no speech, never transcribed.
    if not body.strip(b"\x00"):
        raise TranscribeError("empty", "no speech detected")
    return body


class WindowLimiter:
    """In-process sliding-window limiter (single-worker service).

    Mirrors the portfolio's limiter semantics: at most `limit` hits per
    `window_ms` for a key. Never raises; a full window reports `retry_after_ms`.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[int]] = {}

    def allow(self, key: str, limit: int, window_ms: int, now_ms: int) -> tuple[bool, int]:
        hits = [t for t in self._hits.get(key, []) if now_ms - t < window_ms]
        if len(hits) >= limit:
            retry = max(0, window_ms - (now_ms - hits[0]))
            self._hits[key] = hits
            return False, retry
        hits.append(now_ms)
        self._hits[key] = hits
        return True, 0
