"""Centralized configuration. Everything comes from the environment;
safe defaults keep every fail-closed path testable without secrets.

Required in production (no defaults — the service refuses to start
voice sessions without them):
  VOICE_RAG_API_URL      Portfolio RAG endpoint, e.g. https://www.jamesugbanu.com/api/rag
  VOICE_RAG_SERVICE_TOKEN  Bearer token the portfolio accepts for server-to-server calls
  VOICE_HMAC_SECRET      Signs browser session tickets (>=32 random bytes)

Model selection (Phase 2 locks these after hardware benchmarks):
  VOICE_STT_MODEL        faster-whisper checkpoint, default "small.en"
  VOICE_TTS_VOICE_REF    Path to the authorized reference wav for cloning (optional until supplied)
  VOICE_TTS_LANGUAGE     Default "english"
"""
from __future__ import annotations

import os


def _getint(env: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int((env.get(name, "") or "").strip() or default)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


class Settings:
    """Read once at startup; tests construct with explicit values instead."""

    def __init__(self, env: dict | None = None) -> None:
        src: dict = dict(env) if env is not None else dict(os.environ)
        get = lambda k, d="": (src.get(k, d) or d).strip()

        self.rag_api_url: str = get("VOICE_RAG_API_URL")
        self.rag_service_token: str = get("VOICE_RAG_SERVICE_TOKEN")
        self.hmac_secret: str = get("VOICE_HMAC_SECRET")

        self.stt_model: str = get("VOICE_STT_MODEL", "small.en")
        self.stt_fallback_model: str = get("VOICE_STT_FALLBACK_MODEL", "base.en")
        self.vad_source: str = get("VOICE_VAD_SOURCE", "snakers4/silero-vad")
        self.tts_voice_ref: str = get("VOICE_TTS_VOICE_REF")
        self.tts_language: str = get("VOICE_TTS_LANGUAGE", "english")

        self.max_call_seconds: int = _getint(src, "VOICE_MAX_CALL_SECONDS", 300, 60, 3600)
        self.daily_allowance_seconds: int = _getint(src, "VOICE_DAILY_ALLOWANCE_SECONDS", 600, 60, 86400)
        self.session_attempts: int = _getint(src, "VOICE_SESSION_ATTEMPTS", 3, 1, 100)
        self.attempt_window_seconds: int = _getint(src, "VOICE_SESSION_ATTEMPT_WINDOW_SECONDS", 600, 60, 86400)
        self.stale_session_seconds: int = _getint(src, "VOICE_STALE_SESSION_SECONDS", 600, 120, 86400)

        self.max_audio_frame_bytes: int = _getint(src, "VOICE_MAX_AUDIO_FRAME_BYTES", 192000, 1024, 1_000_000)
        self.max_turn_audio_seconds: int = _getint(src, "VOICE_MAX_TURN_AUDIO_SECONDS", 60, 5, 300)
        self.max_message_bytes: int = _getint(src, "VOICE_MAX_MESSAGE_BYTES", 262144, 4096, 2_000_000)

        self.sample_rate: int = 16000
        self.session_ticket_ttl_seconds: int = 120
        self.rag_timeout_seconds: int = _getint(src, "VOICE_RAG_TIMEOUT_SECONDS", 25, 5, 120)
        self.db_path: str = get("VOICE_DB_PATH", "data/voice.sqlite3")
        self.public_base_url: str = get("VOICE_PUBLIC_BASE_URL").rstrip("/")
        # Explicit browser origins (comma-separated). No wildcard: cookies
        # and audio never go to arbitrary sites.
        self.allowed_origins: list = [
            o.strip() for o in get("VOICE_ALLOWED_ORIGINS", "https://www.jamesugbanu.com").split(",") if o.strip()
        ]

    @property
    def rag_configured(self) -> bool:
        return bool(self.rag_api_url and self.rag_service_token)
