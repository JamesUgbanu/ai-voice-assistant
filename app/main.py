"""Voice gateway: FastAPI app with REST session management and a single
authenticated WebSocket endpoint. Runs on its own server, connects to the
portfolio RAG over HTTPS, and never touches the portfolio database.

This module requires third-party packages (fastapi, uvicorn, httpx) and
only runs on the voice server / CI with deps installed. All pure logic
lives in protocol.py, turns.py, pipeline.py, store.py, and auth.py, which
are stdlib-only and unit-tested without this module.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from . import auth, booking as booking_flow, rag_client, store
from .config import Settings
from .logging_util import configure, event
from .pipeline import run_turn, turn_end_reason
from .partial_stt import PartialTranscriber
from .conversation_end import CallLifecycle, classify_completion, ending_wait_seconds
from .greeting import greeting_reply
from .history import append_turn, clean_history
from .responder import HttpResponder
from .widget_token import verify_widget_token
from .protocol import (
    SERVER_CAPABILITIES,
    is_fatal_error,
    make_envelope,
    should_answer_ping,
    validate_audio_frame,
    validate_envelope,
)
from .stt import FasterWhisperBackend, SttConfig, StubSttBackend
from .transcribe import (
    DICTATION_SUBJECT_PREFIX,
    TranscribeError,
    WindowLimiter,
    validate_transcribe_audio,
)
from .tts import PocketTtsBackend, StubTtsBackend, TtsConfig
from .turns import TurnState
from .vad import SileroVadBackend, StubVadBackend

logger = configure()
settings = Settings()

_db_lock = threading.Lock()
_db = None
_models_lock = asyncio.Lock()
_models: dict = {}
_metrics: dict[str, int] = {}
#: Set once the startup warm-up attempt finishes (success or failure). Session
#: creation waits on it so the first turn never races a cold model load.
_warm_event = threading.Event()
_warm_error: str | None = None
#: Resolved TTS voice mode, populated at warm-up from the loaded backend's
#: ``describe()``. Reported on /healthz so a preset/fallback voice can never
#: silently stand in for the intended cloned voice (no paths, no audio).
_tts_status: dict = {}
_httpx_client = None
#: Composer-dictation rate limits (in-process, single worker).
_transcribe_throttle = WindowLimiter()
_ticket_throttle = WindowLimiter()
#: "Listen to response" TTS-only endpoints (no voice session or allowance).
_tts_throttle = WindowLimiter()
_tts_ticket_throttle = WindowLimiter()
TTS_SUBJECT_PREFIX = "tts:"
#: Cross-session single-flight gate for incremental (partial) transcription.
#: The STT model and one CPU core are shared; a partial decode from another
#: connection must never build a backlog ahead of a turn's final decode.
_partial_gate_sem = None
_partial_gate_lock = threading.Lock()


def _partial_gate():
    """Lazily create the shared partial-decode semaphore inside the running
    loop (never at import time)."""
    global _partial_gate_sem
    if _partial_gate_sem is None:
        with _partial_gate_lock:
            if _partial_gate_sem is None:
                _partial_gate_sem = asyncio.Semaphore(1)
    return _partial_gate_sem


def metric(name: str, delta: int = 1) -> None:
    _metrics[name] = _metrics.get(name, 0) + delta


def _trim_memory() -> None:
    """Return freed glibc heap pages to the OS. Inference allocates and frees
    variable-sized buffers; without trimming, RSS holds that memory after the
    turn ends. No-op on platforms without glibc (e.g. macOS dev)."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def db():
    global _db
    if _db is None:
        import os

        os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
        _db = store.connect(settings.db_path)
    return _db


async def db_call(fn, *args, **kwargs):
    """SQLite is synchronous: run under a lock in a worker thread."""
    loop = asyncio.get_running_loop()

    def _run():
        with _db_lock:
            return fn(db(), *args, **kwargs)

    return await loop.run_in_executor(None, _run)


async def get_models(use_stubs: bool = False):
    """Lazily built once per process (model loads are the slow part)."""
    global _models
    if _models:
        return _models
    async with _models_lock:
        if _models:
            return _models
        if use_stubs or _env_flag("VOICE_USE_STUBS"):
            _models = {
                "stt": StubSttBackend(),
                "tts": StubTtsBackend(),
                "vad": StubVadBackend(),
            }
        else:
            _models = {
                "stt": FasterWhisperBackend(),
                "tts": PocketTtsBackend(),
                "vad": SileroVadBackend(source=settings.vad_source),
            }
        return _models


def _env_flag(name: str) -> bool:
    import os

    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


async def http_client():
    global _httpx_client
    if _httpx_client is None:
        import httpx

        _httpx_client = httpx.AsyncClient(timeout=settings.rag_timeout_seconds + 5)
    return _httpx_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast with actionable messages instead of starting a service that
    # cannot authenticate sessions or reach a conversational backend.
    config_errors = settings.validation_errors()
    if config_errors:
        for message in config_errors:
            event(logger, "config_error", error=message)
        raise RuntimeError("Invalid voice-service configuration: " + "; ".join(config_errors))
    db()  # ensure schema exists before serving
    event(logger, "voice_startup", models="lazy", db=settings.db_path,
          tts_mode=settings.tts_mode, tts_ref_configured=bool(settings.tts_voice_ref),
          responder=settings.responder)
    # Warm the models in the background so the first turn is not cold.
    warm_task = asyncio.create_task(warm_models())
    yield
    warm_task.cancel()
    if _httpx_client is not None:
        try:
            await _httpx_client.aclose()
        except Exception:
            pass
    event(logger, "voice_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="AI Voice Assistant", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Voice-Ticket", "X-Widget-Token"],
        allow_credentials=True,
        max_age=600,
    )

    @app.get("/healthz")
    async def healthz():
        try:
            await db_call(lambda c: c.execute("SELECT 1").fetchone())
            db_ok = True
        except Exception:
            db_ok = False
        # Voice-mode self-report: which TTS voice is actually active, so a
        # preset/fallback voice standing in for the cloned one is visible
        # without reading logs. Booleans/short strings only — never a path.
        tts = {"mode": settings.tts_mode, "ref_configured": bool(settings.tts_voice_ref)}
        tts.update(_tts_status or {})
        return {
            "status": "ok" if db_ok else "degraded",
            "db": db_ok,
            "warm": _warm_event.is_set(),
            "tts": tts,
        }

    @app.get("/metrics")
    async def metrics():
        # Operational counters only. No sessions, transcripts, or PII.
        return dict(_metrics)

    @app.post("/v1/sessions")
    async def create_session(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        # Never hand out a session before the models are warm, so the first
        # turn is not a cold load. Bounded: proceeds after the timeout anyway.
        await wait_until_warm()
        client_ip = _client_ip(request)
        ip_hash = auth.hash_ip(client_ip, _hmac_secret_salt())
        now = time.time()
        visitor_id = body.get("visitor_id") if isinstance(body.get("visitor_id"), str) else ""
        if not auth.is_valid_id(visitor_id):
            visitor_id = auth.new_visitor_id()

        # Optional first-party widget auth. A supplied token must always be
        # valid; "required" mode rejects anonymous session creation. With no
        # token (the portfolio's in-page widget), behavior is unchanged.
        widget_token = request.headers.get("x-widget-token", "")
        claims = (
            verify_widget_token(
                settings.widget_token_secret,
                widget_token,
                now=now,
                audience="widget",
                required_scope="widget:voice",
            )
            if widget_token
            else None
        )
        if widget_token and claims is None:
            metric("sessions_rejected_token")
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "Widget token invalid or expired."}},
                status_code=401,
            )
        if settings.widget_auth_mode == "required" and claims is None:
            metric("sessions_rejected_token")
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "A valid widget token is required."}},
                status_code=401,
            )
        project = ""
        if claims is not None:
            project = str(claims.get("project") or "")
            if settings.widget_allowed_projects and project not in settings.widget_allowed_projects:
                return JSONResponse(
                    {"error": {"code": "unauthorized", "message": "Widget project is not allowed."}},
                    status_code=403,
                )
            request_origin = request.headers.get("origin", "")
            if claims.get("origin") and claims.get("origin") != request_origin:
                return JSONResponse(
                    {"error": {"code": "unauthorized", "message": "Widget token origin mismatch."}},
                    status_code=403,
                )
        # Project-scoped keys isolate sessions/quotas without changing the
        # existing anonymous identifiers used by the portfolio.
        scope = f"{project}:" if project else ""
        attempt_key = f"{scope}{ip_hash}"
        session_visitor_id = f"{scope}{visitor_id}"
        try:
            await db_call(
                store.record_attempt,
                attempt_key,
                now,
                settings.attempt_window_seconds,
            )
            attempts = await db_call(
                store.count_attempts, attempt_key, now, settings.attempt_window_seconds
            )
        except Exception:
            return JSONResponse({"error": {"code": "server_error", "message": "Try again shortly."}}, status_code=500)
        if attempts > settings.session_attempts:
            metric("sessions_rejected_attempts")
            return JSONResponse(
                {"error": {"code": "rate_limited", "message": "Too many session attempts. Try again later."}},
                status_code=429,
            )
        try:
            row = await db_call(
                store.create_session,
                visitor_id=session_visitor_id,
                ip_hash=attempt_key,
                now=now,
                max_seconds=settings.max_call_seconds,
                daily_allowance_seconds=settings.daily_allowance_seconds,
                stale_after_seconds=settings.stale_session_seconds,
            )
        except store.SessionError as exc:
            metric("sessions_rejected")
            status = 409 if exc.code == "active_session" else 429
            message = (
                "A voice session is already active for this visitor."
                if exc.code == "active_session"
                else "Daily voice allowance exhausted."
            )
            return JSONResponse({"error": {"code": exc.code, "message": message}}, status_code=status)
        except Exception:
            return JSONResponse({"error": {"code": "server_error", "message": "Try again shortly."}}, status_code=500)
        expiry = now + 120
        ticket = auth.issue_ticket(_ticket_secret(), row["id"], expiry)
        metric("sessions_created")
        event(logger, "session_created", session=row["id"][:8] + "…")
        base = (settings.public_base_url or "").rstrip("/")
        if base.startswith("https://"):
            ws_url = "wss://" + base[len("https://"):] + "/v1/voice"
        elif base.startswith("http://"):
            ws_url = "ws://" + base[len("http://"):] + "/v1/voice"
        else:
            ws_url = "/v1/voice"
        response = JSONResponse(
            {
                "session_id": row["id"],
                "ticket": ticket,
                "ticket_expires_at": int(expiry),
                "ws_url": ws_url,
                "max_seconds": settings.max_call_seconds,
                "warning_at_seconds": max(0, settings.max_call_seconds - 60),
                "sample_rate": settings.sample_rate,
            }
        )
        secure = request.url.scheme == "https"
        response.set_cookie(
            auth.COOKIE_NAME,
            visitor_id,
            max_age=365 * 24 * 3600,
            path="/",
            httponly=True,
            secure=secure,
            samesite="none" if secure else "lax",
        )
        return response

    @app.post("/v1/transcribe/ticket")
    async def transcribe_ticket(request: Request):
        """Mint a short-lived HMAC ticket for composer dictation.

        Browser-usable without any secret: the ticket only proves this origin
        spoke to us moments ago; abuse is bounded by per-IP windows and the
        ticket's short TTL. No voice-session/allowance budget is consumed."""
        await wait_until_warm()
        now = time.time()
        now_ms = int(now * 1000)
        ip_hash = auth.hash_ip(_client_ip(request), _hmac_secret_salt())
        allowed, retry = _ticket_throttle.allow(
            f"tt:{ip_hash}", settings.transcribe_ticket_per_minute, 60_000, now_ms
        )
        if not allowed:
            metric("transcribe_ticket_rejected")
            return JSONResponse(
                {"error": {"code": "rate_limited", "message": "Too many requests. Try again shortly."}},
                status_code=429,
                headers={"Retry-After": str(max(1, retry // 1000))},
            )
        visitor_id = request.cookies.get(auth.COOKIE_NAME, "")
        new_visitor = not auth.is_valid_id(visitor_id)
        if new_visitor:
            visitor_id = auth.new_visitor_id()
        expiry = now + settings.transcribe_ticket_ttl_seconds
        ticket = auth.issue_ticket(
            _ticket_secret(), f"{DICTATION_SUBJECT_PREFIX}{visitor_id}", expiry
        )
        response = JSONResponse(
            {"ticket": ticket, "expires_at": int(expiry), "sample_rate": settings.sample_rate}
        )
        if new_visitor:
            secure = request.url.scheme == "https"
            response.set_cookie(
                auth.COOKIE_NAME,
                visitor_id,
                max_age=365 * 24 * 3600,
                path="/",
                httponly=True,
                secure=secure,
                samesite="none" if secure else "lax",
            )
        return response

    @app.post("/v1/transcribe")
    async def transcribe(request: Request):
        """Transcribe one composer recording with the SAME loaded Whisper model
        used by voice mode. Body: raw PCM16 mono 16 kHz (what the browser worklet
        produces and faster-whisper expects — no decode/resample step)."""
        now = time.time()
        ticket = request.headers.get("x-voice-ticket", "")
        subject = (
            auth.verify_ticket(
                _ticket_secret(), ticket, now=now,
                max_age_seconds=settings.transcribe_ticket_ttl_seconds,
            )
            if ticket
            else None
        )
        if not subject or not subject.startswith(DICTATION_SUBJECT_PREFIX):
            metric("transcribe_unauthorized")
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "Dictation ticket invalid or expired."}},
                status_code=401,
            )
        ip_hash = auth.hash_ip(_client_ip(request), _hmac_secret_salt())
        allowed, retry = _transcribe_throttle.allow(
            f"tr:{ip_hash}", settings.transcribe_per_minute, 60_000, int(now * 1000)
        )
        if not allowed:
            metric("transcribe_rejected")
            return JSONResponse(
                {"error": {"code": "rate_limited", "message": "Too many dictation requests. Try again shortly."}},
                status_code=429,
                headers={"Retry-After": str(max(1, retry // 1000))},
            )
        # Bounded read: never buffer more than one allowed recording.
        max_bytes = settings.transcribe_max_seconds * settings.sample_rate * 2
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                metric("transcribe_too_large")
                return JSONResponse(
                    {"error": {"code": "too_large", "message": "Recording is longer than allowed."}},
                    status_code=413,
                )
        try:
            pcm = validate_transcribe_audio(bytes(buf), max_bytes=max_bytes)
        except TranscribeError as exc:
            status = 413 if exc.code == "too_large" else 400
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=status)
        await wait_until_warm()
        try:
            models = await get_models()
            stt, stt_cfg = models["stt"], _stt_config()
        except Exception:
            return JSONResponse(
                {"error": {"code": "server_error", "message": "Try again shortly."}}, status_code=500
            )
        t0 = time.time()
        try:
            transcript = await asyncio.to_thread(stt.transcribe, pcm, stt_cfg)
        except Exception as exc:  # noqa: BLE001
            event(logger, "transcribe_failed", error=type(exc).__name__)
            return JSONResponse(
                {"error": {"code": "transcribe_failed", "message": "Could not transcribe that. Please try again."}},
                status_code=500,
            )
        metric("transcribe_ok")
        event(logger, "dictation_transcribed", ms=int((time.time() - t0) * 1000),
              no_speech=bool(getattr(transcript, "no_speech", False)),
              chars=len(transcript.text or ""))
        await asyncio.to_thread(_trim_memory)
        return JSONResponse(
            {"text": transcript.text or "", "no_speech": bool(getattr(transcript, "no_speech", False))}
        )

    @app.post("/v1/tts/ticket")
    async def tts_ticket(request: Request):
        """Mint a short-lived ticket for reading an existing chat answer aloud.

        This is intentionally separate from voice sessions: listening must not
        request microphone permission or consume the daily call allowance."""
        await wait_until_warm()
        now = time.time()
        ip_hash = auth.hash_ip(_client_ip(request), _hmac_secret_salt())
        allowed, retry = _tts_ticket_throttle.allow(
            f"tts-ticket:{ip_hash}", settings.tts_ticket_per_minute, 60_000, int(now * 1000)
        )
        if not allowed:
            metric("tts_ticket_rejected")
            return JSONResponse(
                {"error": {"code": "rate_limited", "message": "Too many requests. Try again shortly."}},
                status_code=429,
                headers={"Retry-After": str(max(1, retry // 1000))},
            )
        visitor_id = request.cookies.get(auth.COOKIE_NAME, "")
        new_visitor = not auth.is_valid_id(visitor_id)
        if new_visitor:
            visitor_id = auth.new_visitor_id()
        expiry = now + settings.tts_ticket_ttl_seconds
        ticket = auth.issue_ticket(
            _ticket_secret(), f"{TTS_SUBJECT_PREFIX}{visitor_id}", expiry
        )
        response = JSONResponse(
            {"ticket": ticket, "expires_at": int(expiry), "sample_rate": 24000}
        )
        if new_visitor:
            secure = request.url.scheme == "https"
            response.set_cookie(
                auth.COOKIE_NAME,
                visitor_id,
                max_age=365 * 24 * 3600,
                path="/",
                httponly=True,
                secure=secure,
                samesite="none" if secure else "lax",
            )
        return response

    @app.post("/v1/tts")
    async def tts_listen(request: Request):
        """Synthesize existing assistant text through the same TTS backend.

        No RAG, no transcript storage, and no microphone. Text is bounded and
        never logged; only the resolved voice metadata is observable."""
        now = time.time()
        ticket = request.headers.get("x-voice-ticket", "")
        subject = (
            auth.verify_ticket(
                _ticket_secret(), ticket, now=now,
                max_age_seconds=settings.tts_ticket_ttl_seconds,
            )
            if ticket
            else None
        )
        if not subject or not subject.startswith(TTS_SUBJECT_PREFIX):
            metric("tts_unauthorized")
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "Audio ticket invalid or expired."}},
                status_code=401,
            )
        ip_hash = auth.hash_ip(_client_ip(request), _hmac_secret_salt())
        allowed, retry = _tts_throttle.allow(
            f"tts:{ip_hash}", settings.tts_per_minute, 60_000, int(now * 1000)
        )
        if not allowed:
            metric("tts_rejected")
            return JSONResponse(
                {"error": {"code": "rate_limited", "message": "Too many audio requests. Try again shortly."}},
                status_code=429,
                headers={"Retry-After": str(max(1, retry // 1000))},
            )
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16_384:
                return JSONResponse(
                    {"error": {"code": "too_large", "message": "That response is too long to read aloud."}},
                    status_code=413,
                )
        try:
            body = json.loads(bytes(raw))
        except Exception:
            body = None
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                {"error": {"code": "bad_message", "message": "Text is required."}},
                status_code=400,
            )
        text = text.strip()
        if len(text) > settings.tts_listen_max_chars:
            return JSONResponse(
                {"error": {"code": "too_large", "message": "That response is too long to read aloud."}},
                status_code=413,
            )
        await wait_until_warm()
        try:
            models = await get_models()
            voice = models["tts"]
            pcm = await asyncio.to_thread(voice.synthesize_full, text, _tts_config())
        except Exception:
            metric("tts_failed")
            return JSONResponse(
                {"error": {"code": "tts_failed", "message": "Could not read that response. Please try again."}},
                status_code=500,
            )
        metric("tts_ok")
        await asyncio.to_thread(_trim_memory)
        return Response(
            content=pcm,
            media_type="audio/L16",
            headers={
                "Cache-Control": "no-store",
                "X-Sample-Rate": str(voice.sample_rate),
                "Access-Control-Expose-Headers": "X-Sample-Rate",
            },
        )

    @app.websocket("/v1/voice")
    async def voice_socket(websocket: WebSocket):
        origin = websocket.headers.get("origin", "")
        if origin and origin not in settings.allowed_origins:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        try:
            await _handle_socket(websocket)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            event(logger, "socket_error", error=type(exc).__name__)
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    return app


def _hmac_secret_salt() -> str:
    # Salt for IP hashing; falls back to a process-ephemeral value so a
    # missing salt degrades toward *more* privacy (no cross-restart link).
    import os

    return os.environ.get("VOICE_IP_SALT", "") or f"ephemeral-{os.getpid()}"


def _ticket_secret() -> str:
    import os

    secret = os.environ.get("VOICE_HMAC_SECRET", "")
    if not secret:
        raise RuntimeError("VOICE_HMAC_SECRET is not configured")
    return secret


def _diag_capture_dir() -> str | None:
    """Directory for temporary P2Y Whisper-input captures, or None.

    Opt-in via VOICE_DIAG_CAPTURE_DIR (set only in the local-dev overlay).
    Unset/empty by default: zero capture behavior. Local only — the sidecar
    holds transcript text and must never be enabled in production.
    """
    import os

    directory = os.environ.get("VOICE_DIAG_CAPTURE_DIR", "").strip()
    return directory or None


def _client_ip(request: Request) -> str:
    # Trust model: Caddy overwrites X-Forwarded-For with the true peer IP,
    # so first-entry is correct in production. Direct container access could
    # spoof this, but it only feeds the attempt-rate bucket — per-visitor
    # quotas and the active-session guard do not depend on IP at all.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first[:64]
    return request.client.host if request.client else "unknown"


async def _handle_socket(websocket: WebSocket) -> None:
    now = time.time()
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
    except (asyncio.TimeoutError, Exception):
        await websocket.close(code=4408)
        return
    if len(raw.encode()) > 32768:
        await websocket.close(code=4409)
        return
    try:
        hello = validate_envelope(json.loads(raw), max_bytes=32768, from_client=True)
    except Exception:
        await websocket.close(code=4400)
        return
    if hello["type"] != "hello":
        await websocket.close(code=4400)
        return
    payload = hello["payload"]
    ticket = payload.get("ticket", "")
    session_id = auth.verify_ticket(_ticket_secret(), ticket, now=now)
    if not session_id:
        await websocket.send_json(make_envelope("error", {"code": "unauthorized", "message": "Session ticket invalid or expired."}))
        await websocket.close(code=4401)
        return
    row = await db_call(store.get_session, session_id)
    if row is None or row["status"] != "active":
        await websocket.send_json(make_envelope("error", {"code": "session_expired", "message": "Session is no longer active."}))
        await websocket.close(code=4401)
        return

    models = await get_models()
    stt, tts, vad = models["stt"], models["tts"], models["vad"]
    stt_config = _stt_config()
    tts_config = _tts_config()
    state = TurnState()
    turn_cancel: asyncio.Event | None = None
    turn_task: asyncio.Task | None = None
    audio_buf = bytearray()
    audio_frames = 0
    # Same-session conversational memory. The portfolio RAG owns grounding;
    # this list only carries recent turns over the existing authenticated API.
    history: list[dict] = clean_history(payload.get("history"))
    # Session-local voice booking state; never shared across WebSockets.
    booking = booking_flow.VoiceBookingSession()
    # The generic HTTP responder is optional; the portfolio adapter below is
    # the default and preserves the existing RAG/booking contract.
    http_responder = (
        HttpResponder(
            url=settings.responder_url,
            token=settings.responder_token,
            timeout_seconds=settings.responder_timeout_seconds,
            session_id=session_id,
            client_factory=http_client,
        )
        if settings.responder == "http"
        else None
    )
    last_ping_at = 0.0
    started_at = float(row["started_at"])
    max_seconds = int(row["max_seconds"])
    # Monotonic marks for the streaming-latency breakdown (per turn).
    turn_started_at = 0.0
    eou_at = 0.0
    partial_times: dict = {"first": None}

    async def send(env: dict) -> None:
        await websocket.send_json(env)

    async def emit_partial(text: str, seq: int, tid: str) -> None:
        if partial_times["first"] is None:
            partial_times["first"] = time.monotonic()
        await send(
            make_envelope("transcript_partial", {"text": text, "seq": seq, "turn_id": tid})
        )

    # Incremental transcription while the user speaks. Disabled by config, in
    # which case nothing below emits and the previous behavior is exact.
    partial = PartialTranscriber(
        stt=stt,
        stt_config=stt_config,
        emit=emit_partial,
        enabled=settings.stt_streaming,
        interval_ms=settings.stt_partial_interval_ms,
        min_audio_ms=settings.stt_partial_min_audio_ms,
        max_audio_seconds=settings.stt_partial_max_audio_seconds,
        gate=_partial_gate(),
        on_error=lambda _name: metric("partial_stt_failed"),
    )

    # --- Automatic call termination (ACTIVE → ENDING → TERMINATED) ----------
    # The agent may end a clearly-concluded conversation. The decision is a
    # deterministic structured value (never raw generated text); the closing
    # goodbye is spoken through the normal pipeline, and termination reuses the
    # same session-end path as a client hangup. Idempotent by construction.
    call = CallLifecycle()
    playback_done = asyncio.Event()
    terminate_task: asyncio.Task | None = None

    async def terminate_call(reason: str) -> None:
        """Single, idempotent termination funnel (agent- or user-initiated)."""
        if not call.terminate():
            return
        try:
            await db_call(store.end_session, session_id, time.time(), reason)
        except Exception:
            pass
        try:
            await send(make_envelope("session_ended", {"reason": reason}))
        except Exception:
            pass
        try:
            await websocket.close(code=1000)
        except Exception:
            pass

    async def begin_ending(frames: int) -> None:
        """Speak-complete → wait (bounded) for playback → terminate. Reversible
        until the client confirms playback or the bound elapses."""
        nonlocal terminate_task
        if not call.begin_ending():
            return
        playback_done.clear()
        metric("auto_end_started")

        async def _finish() -> None:
            timeout = min(settings.playback_timeout_ms / 1000.0, ending_wait_seconds(frames))
            try:
                await asyncio.wait_for(playback_done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                # Documented fallback: playback could not be confirmed; end the
                # call anyway rather than leaving it open indefinitely.
                event(logger, "auto_end_playback_timeout", bound_ms=int(timeout * 1000))
            await terminate_call("agent_ended")

        terminate_task = asyncio.create_task(_finish())

    def cancel_ending() -> None:
        """The caller resumed speaking during ENDING → back to ACTIVE."""
        nonlocal terminate_task
        if not call.cancel_ending():
            return
        if terminate_task is not None and not terminate_task.done():
            terminate_task.cancel()
        terminate_task = None
        playback_done.clear()
        metric("auto_end_cancelled")

    async def session_alive() -> bool:
        fresh = await db_call(store.get_session, session_id)
        if fresh is None or fresh["status"] != "active":
            return False
        if time.time() - started_at >= max_seconds:
            return False
        await db_call(store.touch_session, session_id, time.time())
        return True

    async def end_turn_state(reason: str) -> None:
        nonlocal turn_task, turn_cancel, audio_buf, audio_frames
        turn_id = state.cancel()
        # Signal cooperatively first: the RAG stream checks this event and
        # closes the upstream HTTP request, and the streaming TTS producer
        # observes it between chunks. The task cancel below is the backstop for
        # anything mid-await. Interrupting cancels the whole obsolete pipeline
        # (RAG + TTS generation), not just playback.
        if turn_cancel is not None:
            turn_cancel.set()
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
            try:
                await turn_task
            except (asyncio.CancelledError, Exception):
                pass
        turn_task = None
        turn_cancel = None
        audio_buf = bytearray()
        audio_frames = 0
        # A cancelled/superseded turn must not keep emitting partials.
        await partial.stop()
        await send(make_envelope("turn_end", {"reason": reason, "turn_id": turn_id}))
        # Interrupted/cancelled turns leave inference scratch behind; hand it
        # back to the OS now rather than holding it as idle RSS.
        await asyncio.to_thread(_trim_memory)

    async def run_utterance(
        turn_id: str, pcm: bytes, reused_transcript=None, decision: dict | None = None,
        text_input: str | None = None,
    ) -> None:
        nonlocal turn_cancel, turn_task
        # When the incremental path already decoded exactly this audio, the
        # pipeline reuses it and the final transcript is immediate.
        final_at: dict = {"t": None}

        async def on_transcript(text: str) -> None:
            final_at["t"] = time.monotonic()
            # Structured completion decision from the finalized transcript. The
            # decision only *marks* the turn; the orchestrator enforces the
            # lifecycle and speaks a fixed closing (no extra LLM request).
            completion = classify_completion(text, settings.end_call_confidence)
            if completion.should_end_call and decision is not None:
                decision["should_end"] = True
                decision["reason"] = completion.reason
            await send(make_envelope("transcript_final", {"text": text, "turn_id": turn_id}))

        if not await session_alive():
            await send(make_envelope("error", {"code": "session_expired", "message": "Call time is up."}))
            await send(make_envelope("session_ended", {"reason": "max_duration"}))
            await db_call(store.end_session, session_id, time.time(), "max_duration")
            await websocket.close(code=1000)
            return
        turn_cancel = asyncio.Event()
        await send(make_envelope("rag_started", {"turn_id": turn_id}))

        # Per-turn TTFB sink: the portfolio RAG stream writes request→first-body-
        # byte here. A fresh dict per utterance keeps concurrent turns isolated.
        ttfb_out: dict = {}

        if http_responder is not None:
            # Generic application backend. It receives the finalized utterance,
            # bounded history, and session id, and streams plain answer text.
            async def rag_ask(text: str) -> str:
                return await http_responder.ask(text, list(history))

            def rag_stream(text: str):
                stop_event = turn_cancel
                return http_responder.stream(
                    text,
                    list(history),
                    should_stop=(lambda: stop_event.is_set()) if stop_event else None,
                )
        else:
            async def rag_ask(text: str) -> str:
                client = await http_client()
                from . import rag_client as rc

                out = await rc.fetch_answer(
                    client,
                    settings.rag_api_url,
                    settings.rag_service_token,
                    text,
                    history=list(history),
                    timeout_seconds=settings.rag_timeout_seconds,
                )
                return out.text

            async def call_booking_api(payload: dict) -> dict:
                if not settings.booking_api_url:
                    return {"status": "failed", "message": "Voice booking is not configured."}
                try:
                    client = await http_client()
                    response = await client.post(
                        settings.booking_api_url,
                        json=payload,
                        timeout=settings.booking_timeout_seconds,
                    )
                    try:
                        body = response.json()
                    except Exception:
                        body = {}
                    # Single source of truth for the confirmed contract: only
                    # 200/201 with status == "confirmed" may be announced.
                    return booking_flow.parse_booking_response(response.status_code, body)
                except Exception:
                    # A timeout is uncertain: the event may exist. Never speak a
                    # confirmation. The session keeps the idempotency key so a
                    # retry reconciles instead of creating a second booking.
                    return {"status": "uncertain", "message": "The booking request timed out."}

            def capture_booking(data: dict) -> None:
                booking.capture_offer(data)

            async def rag_stream(text: str):
                # Pending voice booking turns are deterministic; unrelated turns
                # fall through to ordinary RAG.
                reply, payload = booking.handle(text, settings.booking_timezone)
                if reply is not None:
                    yield reply
                    return
                if payload is not None:
                    result = await call_booking_api(payload)
                    message = booking_flow.result_message(result, booking.slot or {}, settings.booking_timezone)
                    if result.get("status") == "uncertain":
                        # Keep the key/details reachable so an explicit retry
                        # reconciles with the portfolio instead of double-booking.
                        booking.mark_uncertain()
                    else:
                        booking.clear()
                    yield message
                    return
                client = await http_client()
                from . import rag_client as rc

                stop_event = turn_cancel
                async for chunk in rc.iter_answer(
                    client,
                    settings.rag_api_url,
                    settings.rag_service_token,
                    text,
                    history=list(history),
                    timeout_seconds=settings.rag_timeout_seconds,
                    should_stop=(lambda: stop_event.is_set()) if stop_event else None,
                    mode="voice",
                    time_zone=settings.booking_timezone,
                    on_booking=capture_booking,
                    on_ttfb=lambda ms: ttfb_out.__setitem__("ms", ms),
                ):
                    yield chunk

        async def emit_frame(pcm_bytes: bytes, final: bool) -> None:
            if not state.is_current(turn_id):
                return
            import base64 as _b64

            await send(
                {
                    "v": 1,
                    "type": "audio_frame",
                    "seq": 0,
                    "turn_id": turn_id,
                    "payload": {
                        "audio_b64": _b64.b64encode(pcm_bytes).decode(),
                        "sample_rate": tts.sample_rate,
                        "final": final,
                    },
                }
            )

        def _fast_reply(text: str, obj: object) -> str | None:
            # A concluded conversation speaks the fixed closing (no RAG round
            # trip); otherwise the existing deterministic greeting fast path.
            if decision is not None and decision.get("should_end"):
                return settings.end_call_closing
            return greeting_reply(text, getattr(obj, "avg_logprob", None))

        try:
            result, timings = await run_turn(
                state=state,
                turn_id=turn_id,
                audio=pcm,
                cancelled=turn_cancel,
                stt=stt,
                stt_config=stt_config,
                vad=vad,
                vad_threshold=0.5,
                rag_ask=rag_ask,
                rag_stream=rag_stream,
                tts=tts,
                tts_config=tts_config,
                emit_audio_frame=emit_frame,
                segmentation=_segmentation_config(),
                fast_reply=lambda text, obj: _fast_reply(text, obj),
                on_transcript=on_transcript,
                diag_dir=_diag_capture_dir(),
                preexisting_transcript=reused_transcript,
                text_input=text_input,
                ttfb_out=ttfb_out,
            )
        except asyncio.CancelledError:
            return
        # A superseded/cancelled turn may have submitted a booking whose
        # outcome we never observed. Keep the idempotency key so the next
        # explicit confirmation reconciles rather than booking twice.
        if result.cancelled:
            booking.mark_uncertain()
        if decision is not None:
            decision["frames"] = result.audio_frames
        # Stage-by-stage timing for one turn (monotonic durations, no PII).
        event(
            logger,
            "turn_timing",
            turn=turn_id,
            stt_ms=timings.stt_ms,
            stt_reused=timings.stt_reused,
            rag_ms=timings.rag_ms,
            rag_ttfb_ms=timings.rag_ttfb_ms,
            rag_total_ms=timings.rag_total_ms,
            seg_wait_ms=timings.seg_wait_ms,
            tts_first_ms=timings.tts_first_ms,
            first_audio_ms=timings.first_audio_ms,
            tts_total_ms=timings.tts_total_ms,
            total_ms=timings.total_ms,
            frames=result.audio_frames,
            fast_path=result.fast_path,
            cancelled=result.cancelled,
            error=result.error,
        )
        # Incremental-transcription breakdown (monotonic, no audio/text):
        # how many partials the user saw, when the first one arrived, and the
        # end-of-speech → final-transcript cost the streaming path changed.
        event(
            logger,
            "turn_streaming",
            turn=turn_id,
            partials=partial.emitted,
            stt_reused=timings.stt_reused,
            first_partial_ms=(
                int((partial_times["first"] - turn_started_at) * 1000)
                if partial_times["first"] is not None and turn_started_at
                else None
            ),
            eou_to_final_ms=(
                int((final_at["t"] - eou_at) * 1000)
                if final_at["t"] is not None and eou_at
                else None
            ),
            stt_ms=timings.stt_ms,
        )
        # The turn-end decision comes from the result, never from
        # state.is_current: a completed turn is already idle, so gating the
        # completion event on liveness silently swallowed turn_end(complete)
        # (client stuck 'speaking') and turn_end(error).
        # Record only a completed, uncancelled turn. The transcript is the
        # authoritative decode; the answer is what the visitor actually heard.
        # Retries cannot duplicate it because this runs once per completed turn.
        if not result.cancelled and not result.error:
            append_turn(history, result.transcript, result.answer_text)
        reason = turn_end_reason(result)
        if reason is None:
            return
        if not result.cancelled and not result.error and result.answer_text.strip():
            await send(make_envelope(
                "assistant_text",
                {"text": result.answer_text.strip()},
                turn_id=turn_id,
            ))
        if reason == "complete":
            event(logger, "turn_complete", frames=result.audio_frames)
        elif reason == "empty":
            pass
        else:
            event(logger, "turn_failed", turn=turn_id, reason=reason)
        await send(make_envelope("turn_end", {"reason": reason, "turn_id": turn_id}))
        turn_task = None
        # Return inference scratch (TTS/RAG buffers) to the OS after the turn.
        await asyncio.to_thread(_trim_memory)

    await send(make_envelope(
        "session_created",
        {"session_id": session_id, "max_seconds": max_seconds, "capabilities": sorted(SERVER_CAPABILITIES)},
    ))
    await send(make_envelope("listening", {}))

    try:
        while True:
            try:
                raw_msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Heartbeat window: re-validate session, keep alive if valid.
                if not await session_alive():
                    await send(make_envelope("session_ended", {"reason": "max_duration"}))
                    await db_call(store.end_session, session_id, time.time(), "max_duration")
                    await websocket.close(code=1000)
                    return
                await send(make_envelope("pong", {}))
                continue
            if len(raw_msg.encode()) > settings.max_message_bytes:
                await send(make_envelope("error", {"code": "bad_message", "message": "Message too large."}))
                continue
            try:
                env_msg = validate_envelope(json.loads(raw_msg), max_bytes=settings.max_message_bytes, from_client=True)
            except Exception:
                await send(make_envelope("error", {"code": "bad_message", "message": "Malformed message."}))
                continue
            etype = env_msg["type"]
            payload = env_msg["payload"]

            if etype == "text_message":
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 500:
                    await send(make_envelope("error", {"code": "bad_message", "message": "Enter a short message."}))
                    continue
                if call.reversible:
                    cancel_ending()
                # A typed message supersedes an in-flight spoken turn. This is
                # the same cancellation path used by barge-in, not a second
                # conversation engine.
                # A typed turn supersedes any collecting/processing/speaking
                # turn, including a spoken utterance that has not ended yet.
                if state.state != "idle" or (turn_task is not None and not turn_task.done()):
                    await end_turn_state("interrupted")
                state.begin_turn()
                turn_started_at = time.monotonic()
                partial_times["first"] = None
                tid_typed = state.turn_id
                if tid_typed is None:
                    continue
                await partial.stop()
                decision_typed: dict = {}

                async def _guarded_typed(tid_: str = tid_typed, text_: str = text) -> None:
                    try:
                        await run_utterance(tid_, b"", None, decision_typed, text_input=text_)
                    except Exception as exc:
                        event(logger, "turn_failed", error=type(exc).__name__)
                        try:
                            await send(make_envelope("turn_end", {"reason": "server_error", "turn_id": tid_}))
                        except Exception:
                            pass
                        return
                    if decision_typed.get("should_end"):
                        await begin_ending(int(decision_typed.get("frames", 0)))

                turn_task = asyncio.create_task(_guarded_typed())
            elif etype == "ping":
                # Throttled via protocol.should_answer_ping: session_alive()
                # hits SQLite, so a ping flood must not become a DB flood.
                # Heartbeat timeout below covers genuinely idle connections.
                if not should_answer_ping(last_ping_at, time.time()):
                    continue
                last_ping_at = time.time()
                if not await session_alive():
                    await send(make_envelope("session_ended", {"reason": "max_duration"}))
                    await db_call(store.end_session, session_id, time.time(), "max_duration")
                    await websocket.close(code=1000)
                    return
                await send(make_envelope("pong", {}))
            elif etype == "audio_frame":
                # The caller resumed speaking during ENDING: cancel the pending
                # (still-reversible) termination and continue the conversation.
                if call.reversible:
                    cancel_ending()
                if state.state == "idle":
                    state.begin_turn()
                    turn_started_at = time.monotonic()
                    partial_times["first"] = None
                    partial.begin(state.turn_id)
                try:
                    max_turn = settings.max_turn_audio_seconds * settings.sample_rate * 2
                    raw_pcm = validate_audio_frame(
                        payload,
                        max_frame_bytes=settings.max_audio_frame_bytes,
                        max_turn_bytes=max_turn,
                        turn_bytes_so_far=state.turn_bytes,
                    )
                    state.add_audio(len(raw_pcm), max_turn)
                    audio_buf.extend(raw_pcm)
                    audio_frames += 1
                    # Offer the growing utterance to the incremental path. It
                    # skips (never queues) when a decode is in flight or the
                    # debounce/increment threshold is not met.
                    if state.state == "collecting" and state.turn_id is not None:
                        partial.maybe_start(
                            audio_buf, state.turn_id, int(time.monotonic() * 1000)
                        )
                except (ValueError, OverflowError) as exc:
                    code = "turn_too_long" if isinstance(exc, OverflowError) else "bad_message"
                    await send(make_envelope("error", {"code": code, "message": str(exc)}))
                    await end_turn_state("error")
            elif etype == "end_utterance":
                if state.state != "collecting":
                    continue
                # Preserve this utterance's audio FIRST: if a previous turn
                # is still running, interrupt it, then process the buffered
                # utterance as the new turn instead of dropping it silently.
                pcm = bytes(audio_buf)
                nframes = audio_frames
                audio_buf = bytearray()
                audio_frames = 0
                if not pcm.strip(b"\x00"):
                    # Stray/duplicate end_utterance with no new audio. If a
                    # turn is in flight, protect it and stay silent (its own
                    # completion will answer). Otherwise acknowledge with an
                    # empty turn_end so the client never hangs waiting — pure
                    # silence must still terminate the utterance.
                    # (Proven live: an all-silence utterance got no reply at
                    # all and the client waited out its full timeout.)
                    interrupted = turn_task is not None and not turn_task.done()
                    event(logger, "empty_end_utterance", turn_active=interrupted)
                    await partial.stop()
                    if interrupted:
                        continue
                    try:
                        state.transition("idle")
                    except ValueError:
                        pass
                    await send(make_envelope("turn_end", {"reason": "empty", "turn_id": None}))
                    continue
                eou_at = time.monotonic()
                # Let any in-flight partial finish, then reuse it ONLY when it
                # decoded the exact final buffer: identical bytes give an
                # identical transcript, so this cannot clip a trailing word.
                # A shorter window (the usual case) settles to None and the
                # pipeline runs the authoritative full decode as before.
                reused = await partial.settle(len(pcm))
                partial_count = partial.emitted
                await partial.stop()
                event(logger, "utterance_audio", frames=nframes,
                      bytes=len(pcm), secs=round(len(pcm) / 32000, 2),
                      partials=partial_count, stt_reused=reused is not None)
                if turn_task is not None and not turn_task.done():
                    await end_turn_state("interrupted")
                    state.begin_turn()
                tid = state.turn_id
                if tid is None:
                    continue

                async def _guarded(tid_: str = tid, pcm_: bytes = pcm, reused_=reused) -> None:
                    decision: dict = {}
                    try:
                        await run_utterance(tid_, pcm_, reused_, decision)
                    except Exception as exc:
                        event(logger, "turn_failed", error=type(exc).__name__)
                        try:
                            await send(make_envelope("turn_end", {"reason": "server_error", "turn_id": tid_}))
                        except Exception:
                            pass
                        return
                    # The conversation clearly ended: speak the closing (already
                    # emitted) then end the call after playback. Idempotent.
                    if decision.get("should_end"):
                        await begin_ending(int(decision.get("frames", 0)))

                turn_task = asyncio.create_task(_guarded())
            elif etype == "playback_complete":
                # Client confirms its audio queue drained; releases the bounded
                # auto-end wait so the goodbye is never cut off.
                playback_done.set()
            elif etype == "interrupt":
                await end_turn_state("interrupted")
                await send(make_envelope("listening", {}))
            elif etype == "end_session":
                await terminate_call("user_ended")
                return
            elif etype == "hello":
                await send(make_envelope("error", {"code": "bad_message", "message": "Session already open."}))
    except WebSocketDisconnect:
        # Browser gone: cancel in-flight inference (bounded waste, no late
        # audio after reconnect), then leave the row active for stale
        # recovery (usage still counts; next session attempt settles it).
        # Never strand quota.
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
        # Drop any in-flight partial so it cannot emit onto a dead socket.
        await partial.stop()
        metric("disconnects")
        return


def _stt_config():
    from .stt import SttConfig

    return SttConfig(
        model=settings.stt_model,
        beam_size=settings.stt_beam_size,
        cpu_threads=settings.stt_cpu_threads,
        initial_prompt=settings.stt_initial_prompt,
        hotwords=settings.stt_hotwords,
        temperature=settings.stt_temperature,
        condition_on_previous_text=settings.stt_condition_previous,
    )


def _segmentation_config() -> dict:
    return {
        "max_wait_ms": settings.tts_max_wait_ms,
        "first_max_wait_ms": settings.tts_first_max_wait_ms,
        "clause_trigger_chars": settings.tts_clause_trigger_chars,
        "max_chars": settings.tts_max_chars,
        "min_chars": settings.tts_min_chars,
    }


async def warm_models() -> None:
    """Load STT/TTS/VAD weights once at startup, off the request path, so the
    first real turn does not pay the cold model load (the dominant cause of
    multi-second first turns and the historical outlier). Non-blocking: runs
    in a worker thread and never affects health or startup."""
    import time

    try:
        models = await get_models()
        stt, tts, vad = models["stt"], models["tts"], models["vad"]
        stt_cfg, tts_cfg = _stt_config(), _tts_config()

        def _work() -> dict:
            stamps: dict[str, object] = {}
            t_all = time.time()
            t = time.time()
            try:
                vad.speech_probability(bytes(3200))
                stamps["vad_ms"] = int((time.time() - t) * 1000)
            except Exception as exc:  # noqa: BLE001
                stamps["vad_error"] = type(exc).__name__
            t = time.time()
            try:
                # 1 s of silence loads the CTranslate2 model without a real
                # transcript to leak.
                stt.transcribe(bytes(16000 * 2), stt_cfg)
                stamps["stt_ms"] = int((time.time() - t) * 1000)
            except Exception as exc:  # noqa: BLE001
                stamps["stt_error"] = type(exc).__name__
            t = time.time()
            try:
                if hasattr(tts, "preflight"):
                    tts.preflight(tts_cfg)
                else:
                    tts.synthesize_full("Ready.", tts_cfg)
                # Resolved voice mode (preset/clone + cloning_active), so
                # /healthz can prove the intended voice is the active one.
                if hasattr(tts, "describe"):
                    stamps["tts_status"] = tts.describe()
                stamps["tts_ms"] = int((time.time() - t) * 1000)
            except Exception as exc:  # noqa: BLE001
                stamps["tts_error"] = type(exc).__name__
            stamps["total_ms"] = int((time.time() - t_all) * 1000)
            return stamps

        stamps = await asyncio.to_thread(_work)
        global _tts_status
        resolved = stamps.pop("tts_status", None)
        if isinstance(resolved, dict):
            _tts_status = resolved
        # Selected STT model and resolved TTS voice status are logged so the
        # running configuration is always verifiable (e.g. base.en in production
        # vs local small.en; preset vs clone voice).
        event(logger, "models_warm", stt_model=settings.stt_model,
              tts_status=resolved, **stamps)
    except Exception as exc:  # noqa: BLE001
        global _warm_error
        _warm_error = type(exc).__name__
        event(logger, "models_warm_failed", error=_warm_error)
    finally:
        # Readiness reflects "warm-up finished", so a session never waits
        # forever if warming failed — it just proceeds with lazy loading.
        _warm_event.set()
        await asyncio.to_thread(_trim_memory)


async def wait_until_warm(timeout: float = 180.0) -> bool:
    """Wait for the startup warm-up to finish (bounded). Returns True when the
    models are warm, False on timeout."""
    if _warm_event.is_set():
        return True
    try:
        await asyncio.wait_for(asyncio.to_thread(_warm_event.wait), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


def _tts_config():
    from .tts import TtsConfig

    return TtsConfig(
        language=settings.tts_language,
        mode=settings.tts_mode,
        voice_ref=settings.tts_voice_ref,
        preset_voice=settings.tts_preset_voice,
    )


app = create_app()
