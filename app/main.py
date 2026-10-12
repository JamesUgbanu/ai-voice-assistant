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
from .pipeline import run_turn
from .protocol import (
    is_fatal_error,
    make_envelope,
    validate_audio_frame,
    validate_envelope,
)
from .stt import FasterWhisperBackend, SttConfig, StubSttBackend
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
_httpx_client = None


def metric(name: str, delta: int = 1) -> None:
    _metrics[name] = _metrics.get(name, 0) + delta


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
                "vad": SileroVadBackend(),
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
    event(logger, "voice_startup", models="lazy", db=settings.db_path)
    yield
    if _httpx_client is not None:
        try:
            await _httpx_client.aclose()
        except Exception:
            pass
    event(logger, "voice_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="james-voice-service", version="0.1.0", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
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
        return {"status": "ok" if db_ok else "degraded", "db": db_ok}

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
        nonlocal turn_task, turn_cancel, audio_buf
        turn_id = state.cancel()
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
            try:
                await turn_task
            except (asyncio.CancelledError, Exception):
                pass
        turn_task = None
        turn_cancel = None
        audio_buf = bytearray()
        await send(make_envelope("turn_end", {"reason": reason, "turn_id": turn_id}))

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
            result, _timings = await run_turn(
                state=state,
                turn_id=turn_id,
                audio=pcm,
                cancelled=turn_cancel,
                stt=stt,
                stt_config=stt_config,
                vad=vad,
                vad_threshold=0.5,
                rag_ask=rag_ask,
                tts=tts,
                tts_config=tts_config,
                emit_audio_frame=emit_frame,
            )
        except asyncio.CancelledError:
            return
        if result.cancelled or not state.is_current(turn_id):
            return
        if result.error:
            await send(make_envelope("turn_end", {"reason": result.error, "turn_id": turn_id}))
            return
        if not result.transcript:
            await send(make_envelope("turn_end", {"reason": "empty", "turn_id": turn_id}))
            return
        event(logger, "turn_complete", frames=result.audio_frames)
        await send(make_envelope("turn_end", {"reason": "complete", "turn_id": turn_id}))
        turn_task = None

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
                    from .protocol import validate_audio_frame

                    max_turn = settings.max_turn_audio_seconds * settings.sample_rate * 2
                    raw_pcm = validate_audio_frame(
                        payload,
                        max_frame_bytes=settings.max_audio_frame_bytes,
                        max_turn_bytes=max_turn,
                        turn_bytes_so_far=state.turn_bytes,
                    )
                    state.add_audio(len(raw_pcm), max_turn)
                    audio_buf.extend(raw_pcm)
                except (ValueError, OverflowError) as exc:
                    code = "turn_too_long" if isinstance(exc, OverflowError) else "bad_message"
                    await send(make_envelope("error", {"code": code, "message": str(exc)}))
                    await end_turn_state("error")
            elif etype == "end_utterance":
                if state.state != "collecting":
                    continue
                if turn_task is not None and not turn_task.done():
                    await end_turn_state("interrupted")
                    state.begin_turn()
                    audio_buf = bytearray()
                    continue
                pcm = bytes(audio_buf)
                audio_buf = bytearray()
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
        # Browser gone: leave the row active for stale recovery (usage still
        # counts; next session attempt settles it). Never strand quota.
        metric("disconnects")
        return


def _stt_config():
    from .stt import SttConfig

    return SttConfig(model=settings.stt_model)


def _tts_config():
    from .tts import TtsConfig

    return TtsConfig(language=settings.tts_language, voice_ref=settings.tts_voice_ref)


app = create_app()
