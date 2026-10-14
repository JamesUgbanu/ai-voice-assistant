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
from fastapi.responses import JSONResponse

from . import auth, rag_client, store
from .config import Settings
from .logging_util import configure, event
from .pipeline import run_turn, turn_end_reason
from .greeting import greeting_reply
from .protocol import (
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
_httpx_client = None
#: Composer-dictation rate limits (in-process, single worker).
_transcribe_throttle = WindowLimiter()
_ticket_throttle = WindowLimiter()


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
    db()  # ensure schema exists before serving
    event(logger, "voice_startup", models="lazy", db=settings.db_path,
          tts_mode=settings.tts_mode, tts_ref_configured=bool(settings.tts_voice_ref))
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
    app = FastAPI(title="james-voice-service", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Voice-Ticket"],
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
        return {"status": "ok" if db_ok else "degraded", "db": db_ok, "warm": _warm_event.is_set()}

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
        try:
            await db_call(
                store.record_attempt,
                ip_hash,
                now,
                settings.attempt_window_seconds,
            )
            attempts = await db_call(
                store.count_attempts, ip_hash, now, settings.attempt_window_seconds
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
                visitor_id=visitor_id,
                ip_hash=ip_hash,
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
    if len(raw.encode()) > 4096:
        await websocket.close(code=4409)
        return
    try:
        hello = validate_envelope(json.loads(raw), max_bytes=4096, from_client=True)
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
    last_ping_at = 0.0
    started_at = float(row["started_at"])
    max_seconds = int(row["max_seconds"])

    async def send(env: dict) -> None:
        await websocket.send_json(env)

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
        await send(make_envelope("turn_end", {"reason": reason, "turn_id": turn_id}))
        # Interrupted/cancelled turns leave inference scratch behind; hand it
        # back to the OS now rather than holding it as idle RSS.
        await asyncio.to_thread(_trim_memory)

    async def run_utterance(turn_id: str, pcm: bytes) -> None:
        nonlocal turn_cancel, turn_task
        if not await session_alive():
            await send(make_envelope("error", {"code": "session_expired", "message": "Call time is up."}))
            await send(make_envelope("session_ended", {"reason": "max_duration"}))
            await db_call(store.end_session, session_id, time.time(), "max_duration")
            await websocket.close(code=1000)
            return
        turn_cancel = asyncio.Event()
        await send(make_envelope("rag_started", {"turn_id": turn_id}))

        async def rag_ask(text: str) -> str:
            client = await http_client()
            from . import rag_client as rc

            out = await rc.fetch_answer(
                client,
                settings.rag_api_url,
                settings.rag_service_token,
                text,
                timeout_seconds=settings.rag_timeout_seconds,
            )
            return out.text

        async def rag_stream(text: str):
            # Stream the portfolio answer token-by-token so TTS can start on
            # the first natural segment instead of the full response.
            client = await http_client()
            from . import rag_client as rc

            stop_event = turn_cancel
            async for chunk in rc.iter_answer(
                client,
                settings.rag_api_url,
                settings.rag_service_token,
                text,
                timeout_seconds=settings.rag_timeout_seconds,
                should_stop=(lambda: stop_event.is_set()) if stop_event else None,
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
                fast_reply=lambda text, obj: greeting_reply(text, getattr(obj, "avg_logprob", None)),
                on_transcript=lambda text: send(
                    make_envelope("transcript_final", {"text": text, "turn_id": turn_id})
                ),
            )
        except asyncio.CancelledError:
            return
        # Stage-by-stage timing for one turn (monotonic durations, no PII).
        event(
            logger,
            "turn_timing",
            turn=turn_id,
            stt_ms=timings.stt_ms,
            rag_ms=timings.rag_ms,
            tts_first_ms=timings.tts_first_ms,
            first_audio_ms=timings.first_audio_ms,
            tts_total_ms=timings.tts_total_ms,
            total_ms=timings.total_ms,
            frames=result.audio_frames,
            fast_path=result.fast_path,
            cancelled=result.cancelled,
            error=result.error,
        )
        # The turn-end decision comes from the result, never from
        # state.is_current: a completed turn is already idle, so gating the
        # completion event on liveness silently swallowed turn_end(complete)
        # (client stuck 'speaking') and turn_end(error).
        reason = turn_end_reason(result)
        if reason is None:
            return
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

    await send(make_envelope("session_created", {"session_id": session_id, "max_seconds": max_seconds}))
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

            if etype == "ping":
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
                if state.state == "idle":
                    state.begin_turn()
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
                    # Stray/duplicate end_utterance carrying no new audio: never
                    # cancel an in-flight turn for nothing. Proven live: a duplicate
                    # end_utterance murdered a valid "Hello" turn (turn_end
                    # "interrupted", then "empty", transcript never delivered).
                    event(logger, "empty_end_utterance",
                          turn_active=turn_task is not None and not turn_task.done())
                    continue
                event(logger, "utterance_audio", frames=nframes,
                      bytes=len(pcm), secs=round(len(pcm) / 32000, 2))
                if turn_task is not None and not turn_task.done():
                    await end_turn_state("interrupted")
                    state.begin_turn()
                tid = state.turn_id
                if tid is None:
                    continue

                async def _guarded(tid_: str = tid, pcm_: bytes = pcm) -> None:
                    try:
                        await run_utterance(tid_, pcm_)
                    except Exception as exc:
                        event(logger, "turn_failed", error=type(exc).__name__)
                        try:
                            await send(make_envelope("turn_end", {"reason": "server_error", "turn_id": tid_}))
                        except Exception:
                            pass

                turn_task = asyncio.create_task(_guarded())
            elif etype == "interrupt":
                await end_turn_state("interrupted")
                await send(make_envelope("listening", {}))
            elif etype == "end_session":
                await db_call(store.end_session, session_id, time.time(), "user_ended")
                await send(make_envelope("session_ended", {"reason": "user_ended"}))
                await websocket.close(code=1000)
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
            stamps: dict[str, int | str] = {}
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
                stamps["tts_ms"] = int((time.time() - t) * 1000)
            except Exception as exc:  # noqa: BLE001
                stamps["tts_error"] = type(exc).__name__
            stamps["total_ms"] = int((time.time() - t_all) * 1000)
            return stamps

        stamps = await asyncio.to_thread(_work)
        event(logger, "models_warm", **stamps)
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
