"""Centralized configuration. Everything comes from the environment;
safe defaults keep every fail-closed path testable without secrets.

Required in production:
  VOICE_HMAC_SECRET      Signs browser session tickets (>=32 random bytes).
  VOICE_RAG_API_URL      Portfolio RAG endpoint (default portfolio responder).
  VOICE_RAG_SERVICE_TOKEN  Bearer token the portfolio accepts.
  or, for another backend:
  VOICE_RESPONDER=http   plus VOICE_RESPONDER_URL.

Model selection:
  VOICE_STT_MODEL        faster-whisper checkpoint, default "base.en"
  VOICE_TTS_VOICE_REF    Path to the authorized reference wav for cloning
                         (required only when VOICE_TTS_MODE=clone)
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


def _getfloat(env: dict, name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float((env.get(name, "") or "").strip() or default)
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

        # Conversational backend: "portfolio" (the reference adapter) or
        # "http" (a small generic adapter for another application).
        self.responder: str = (get("VOICE_RESPONDER", "portfolio") or "portfolio").lower()
        self.responder_url: str = get("VOICE_RESPONDER_URL")
        self.responder_token: str = get("VOICE_RESPONDER_TOKEN")
        self.responder_timeout_seconds: int = _getint(
            src, "VOICE_RESPONDER_TIMEOUT_SECONDS", 25, 5, 120
        )
        # Optional first-party widget tokens. "anonymous" preserves the
        # portfolio's existing per-IP/visitor flow; "required" accepts only a
        # valid, unexpired, project-scoped token signed with this secret.
        self.widget_auth_mode: str = (get("VOICE_WIDGET_AUTH_MODE", "anonymous") or "anonymous").lower()
        self.widget_token_secret: str = get("VOICE_WIDGET_TOKEN_SECRET")
        self.widget_allowed_projects: list = [
            item.strip()
            for item in get("VOICE_WIDGET_ALLOWED_PROJECTS", "portfolio").split(",")
            if item.strip()
        ]

        self.stt_model: str = get("VOICE_STT_MODEL", "base.en")
        # Decoding cost vs accuracy: base.en + greedy beam is ~4x faster than
        # small.en/beam5 with near-identical transcripts on the fixed suite,
        # and the initial_prompt restores proper-noun/technical accuracy.
        self.stt_beam_size: int = _getint(src, "VOICE_STT_BEAM_SIZE", 1, 1, 8)
        # CTranslate2 worker threads for STT. 0 = library default.
        self.stt_cpu_threads: int = _getint(src, "VOICE_STT_CPU_THREADS", 0, 0, 64)
        # Optional Whisper domain biasing: markedly improves proper nouns and
        # product/place vocabulary. The default is the reference deployment's
        # list; override it with your own names and terms.
        self.stt_initial_prompt: str = get(
            "VOICE_STT_INITIAL_PROMPT",
            "James Ugbanu, Chidera, Alimosho, Lagos, Nigeria, PostgreSQL, pgvector, "
            "TypeScript, Terraform, AWS, ECS, Fargate, TaxBloc, MortgageBloc, MyTreep, "
            "Agent SDR, RAG.",
        )
        self.stt_hotwords: str = get("VOICE_STT_HOTWORDS")
        self.stt_temperature: float = _getfloat(src, "VOICE_STT_TEMPERATURE", 0.0, 0.0, 1.0)
        self.stt_condition_previous: bool = get("VOICE_STT_CONDITION_PREVIOUS", "").strip().lower() in ("1", "true", "yes")
        # Incremental (rolling-window) transcription while the user is
        # speaking (see app/partial_stt.py).
        # faster-whisper is a batch decoder, so this re-decodes the accumulated
        # utterance window on a debounce and emits replaceable
        # `transcript_partial` events; the final transcript is unchanged.
        # Set VOICE_STT_STREAMING=0 to restore the previous behavior exactly.
        self.stt_streaming: bool = get("VOICE_STT_STREAMING", "1").strip().lower() in ("1", "true", "yes")
        # Minimum wall-clock gap between partial decodes (debounce).
        self.stt_partial_interval_ms: int = _getint(src, "VOICE_STT_PARTIAL_INTERVAL_MS", 900, 200, 10000)
        # Minimum audio before the first partial (and minimum growth between
        # partials) — avoids decoding sub-word fragments.
        self.stt_partial_min_audio_ms: int = _getint(src, "VOICE_STT_PARTIAL_MIN_AUDIO_MS", 700, 200, 10000)
        # Stop starting new partials once the utterance window exceeds this;
        # the final decode still uses the whole buffer. Bounds O(n^2) cost.
        self.stt_partial_max_audio_seconds: int = _getint(src, "VOICE_STT_PARTIAL_MAX_AUDIO_SECONDS", 15, 2, 55)
        self.vad_source: str = get("VOICE_VAD_SOURCE", "snakers4/silero-vad")
        self.tts_voice_ref: str = get("VOICE_TTS_VOICE_REF")
        self.tts_language: str = get("VOICE_TTS_LANGUAGE", "english")
        # "preset" uses a built-in voice; "clone" uses the authorized
        # reference recording and requires the gated cloning weights.
        mode = get("VOICE_TTS_MODE", "preset").lower()
        self.tts_mode: str = mode if mode in ("preset", "clone") else "preset"
        self.tts_preset_voice: str = get("VOICE_TTS_PRESET_VOICE", "alba")
        # TTS segmentation: how long to wait for a natural boundary before
        # speaking partial text. Lower = faster first audio, more chunk
        # overhead; higher = fewer, longer chunks.
        self.tts_max_wait_ms: int = _getint(src, "VOICE_TTS_MAX_WAIT_MS", 1200, 100, 10000)
        # Shorter wait before the FIRST spoken segment so the assistant starts
        # talking sooner after the RAG stream's first token (the measured
        # first-audio delay); subsequent segments keep `VOICE_TTS_MAX_WAIT_MS`
        # so they stay natural and sentence-sized.
        self.tts_first_max_wait_ms: int = _getint(src, "VOICE_TTS_FIRST_MAX_WAIT_MS", 600, 100, 10000)
        self.tts_clause_trigger_chars: int = _getint(src, "VOICE_TTS_CLAUSE_TRIGGER_CHARS", 80, 8, 2000)
        self.tts_max_chars: int = _getint(src, "VOICE_TTS_MAX_CHARS", 400, 40, 3000)
        self.tts_min_chars: int = _getint(src, "VOICE_TTS_MIN_CHARS", 12, 1, 400)
        # Composer dictation (browser mic → self-hosted Whisper). Reuses the
        # STT model; the byte cap bounds per-request memory on a 2 GB VPS.
        self.transcribe_max_seconds: int = _getint(src, "VOICE_TRANSCRIBE_MAX_SECONDS", 30, 5, 120)
        self.transcribe_per_minute: int = _getint(src, "VOICE_TRANSCRIBE_PER_MINUTE", 20, 1, 600)
        self.transcribe_ticket_per_minute: int = _getint(src, "VOICE_TRANSCRIBE_TICKET_PER_MINUTE", 30, 1, 600)
        self.transcribe_ticket_ttl_seconds: int = _getint(src, "VOICE_TRANSCRIBE_TICKET_TTL_SECONDS", 300, 30, 3600)
        # "Listen to response": synthesize an existing chat answer without
        # consuming a voice session or running RAG again. Tickets are minted
        # per browser origin and every request is bounded by text length.
        self.tts_listen_max_chars: int = _getint(src, "VOICE_TTS_LISTEN_MAX_CHARS", 2000, 100, 3000)
        self.tts_per_minute: int = _getint(src, "VOICE_TTS_PER_MINUTE", 12, 1, 600)
        self.tts_ticket_per_minute: int = _getint(src, "VOICE_TTS_TICKET_PER_MINUTE", 20, 1, 600)
        self.tts_ticket_ttl_seconds: int = _getint(src, "VOICE_TTS_TICKET_TTL_SECONDS", 300, 30, 3600)

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
        # Voice booking uses the portfolio's shared deterministic booking API.
        # Derive the default from the verified RAG endpoint; never guess a host.
        _rag = self.rag_api_url.rstrip("/")
        self.booking_api_url: str = get("VOICE_BOOKING_API_URL") or (
            _rag[: -len("/api/rag")] + "/api/booking" if _rag.endswith("/api/rag") else ""
        )
        self.booking_timezone: str = get("VOICE_BOOKING_TIMEZONE", "Africa/Lagos")
        self.booking_timeout_seconds: int = _getint(src, "VOICE_BOOKING_TIMEOUT_SECONDS", 20, 5, 60)        # Automatic call termination when the conversation has clearly ended.
        # Only explicit, high-confidence closings ("goodbye", "that's all", …)
        # qualify; ambiguous/empty/ordinary turns never terminate.
        self.end_call_confidence: float = _getfloat(src, "VOICE_END_CALL_CONFIDENCE", 0.8, 0.5, 1.0)
        self.end_call_closing: str = get("VOICE_END_CALL_CLOSING", "Thanks for your time. Goodbye!")
        # Bounded wait for the client to confirm the closing audio finished
        # playing before the server terminates; on timeout it terminates anyway
        # (never leaves the call open indefinitely).
        self.playback_timeout_ms: int = _getint(src, "VOICE_PLAYBACK_TIMEOUT_MS", 15000, 2000, 60000)
        self.db_path: str = get("VOICE_DB_PATH", "data/voice.sqlite3")
        self.public_base_url: str = get("VOICE_PUBLIC_BASE_URL").rstrip("/")
        # Explicit browser origins (comma-separated). No wildcard: cookies
        # and audio never go to arbitrary sites. The default is the reference
        # deployment's origin; override it for your own frontend.
        self.allowed_origins: list = [
            o.strip() for o in get("VOICE_ALLOWED_ORIGINS", "https://www.jamesugbanu.com").split(",") if o.strip()
        ]

    @property
    def rag_configured(self) -> bool:
        return bool(self.rag_api_url and self.rag_service_token)

    def validation_errors(self) -> list[str]:
        """Actionable startup errors for missing/invalid required settings.

        The service refuses to start rather than silently disabling a required
        integration. Secrets are never included in the messages.
        """
        errors: list[str] = []
        if not self.hmac_secret:
            errors.append(
                "VOICE_HMAC_SECRET is required (>=32 random bytes; signs session tickets)"
            )
        if self.responder not in ("portfolio", "http"):
            errors.append("VOICE_RESPONDER must be 'portfolio' or 'http'")
        elif self.responder == "portfolio" and not self.rag_configured:
            errors.append(
                "VOICE_RESPONDER=portfolio requires VOICE_RAG_API_URL and "
                "VOICE_RAG_SERVICE_TOKEN (or set VOICE_RESPONDER=http and "
                "VOICE_RESPONDER_URL)"
            )
        elif self.responder == "http" and not self.responder_url:
            errors.append("VOICE_RESPONDER=http requires VOICE_RESPONDER_URL")
        if self.tts_mode == "clone" and not self.tts_voice_ref:
            errors.append("VOICE_TTS_MODE=clone requires VOICE_TTS_VOICE_REF")
        if self.widget_auth_mode not in ("anonymous", "required"):
            errors.append("VOICE_WIDGET_AUTH_MODE must be 'anonymous' or 'required'")
        if self.widget_auth_mode == "required" and not self.widget_token_secret:
            errors.append(
                "VOICE_WIDGET_AUTH_MODE=required requires VOICE_WIDGET_TOKEN_SECRET"
            )
        if self.widget_token_secret and len(self.widget_token_secret) < 32:
            errors.append("VOICE_WIDGET_TOKEN_SECRET must be at least 32 characters")
        return errors
