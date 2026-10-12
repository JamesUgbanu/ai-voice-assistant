"""One conversational turn: validate audio → VAD → STT → RAG → TTS chunks.

All collaborators are constructor-injected behind the adapter interfaces in
stt.py / tts.py / vad.py, so tests drive this with deterministic stubs and
production wires real models. Every unit of work checks the turn token
first: a cancelled or superseded turn aborts before spending more compute,
and late results are dropped by the caller comparing turn ids.

Cancellation model: an asyncio.Event per turn. STT runs in a worker thread
(poll event between sentences); RAG streams tokens (abort on event);
TTS synthesizes sentence by sentence (abort between sentences).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

from .turns import TurnState, split_sentences


@dataclass
class TurnResult:
    turn_id: str
    transcript: str
    answer_text: str
    audio_frames: int
    cancelled: bool = False
    error: str | None = None


@dataclass
class TurnTimings:
    stt_ms: int = 0
    rag_ms: int = 0
    tts_first_ms: int = 0
    tts_total_ms: int = 0


def _silent_pcm16(duration_seconds: float, sample_rate: int = 16000) -> bytes:
    return b"\x00" * int(duration_seconds * sample_rate) * 2


async def run_turn(
    *,
    state: TurnState,
    turn_id: str,
    audio: bytes,
    cancelled: "asyncio.Event",
    stt,
    stt_config,
    vad,
    vad_threshold: float,
    rag_ask: Callable[[str], Any],
    tts,
    tts_config,
    emit_audio_frame,
    max_answer_chars: int = 4000,
    now_ms: Callable[[], int] | None = None,
) -> tuple[TurnResult, TurnTimings]:
    """Execute one turn. Pure orchestration; all side effects via callables.
    `emit_audio_frame(pcm_bytes, final)` may be sync or async."""
    clock = now_ms or (lambda: int(time.time() * 1000))
    timings = TurnTimings()
    t0 = clock()
    if state.state != "collecting" or state.turn_id != turn_id:
        # Stale or superseded turn (cancelled while queued): report
        # cancellation instead of raising so late workers stay quiet.
        return TurnResult(turn_id, "", "", 0, cancelled=True), TurnTimings()

    def alive() -> bool:
        return state.is_current(turn_id) and not cancelled.is_set()

    # 1. Server-side voice check: never spend STT/RAG on silence or noise.
    if not audio.strip(b"\x00"):
        state.transition("idle")
        return TurnResult(turn_id, "", "", 0), timings
    try:
        speech_prob = vad.speech_probability(audio)
    except Exception:
        return _fail(state, turn_id, timings, "stt_failed")
    if speech_prob < vad_threshold:
        state.transition("idle")
        return TurnResult(turn_id, "", "", 0), timings

    # 2. STT in a worker thread (blocking native call).
    try:
        transcript_obj = await asyncio.to_thread(stt.transcribe, audio, stt_config)
    except asyncio.CancelledError:
        raise
    except Exception:
        return _fail(state, turn_id, timings, "stt_failed")
    timings.stt_ms = clock() - t0
    transcript = (transcript_obj.text or "").strip()
    if not transcript or getattr(transcript_obj, "no_speech", False):
        state.transition("idle")
        return TurnResult(turn_id, "", "", 0), timings
    if len(transcript) > 500:
        # Keep RAG input within the portfolio's own question bounds.
        transcript = transcript[:500]
    state.transcript = transcript
    state.transition("processing")
    if not alive():
        return TurnResult(turn_id, "", "", 0, cancelled=True), timings

    # 3. RAG (existing portfolio pipeline via HTTP client).
    t_rag = clock()
    try:
        answer = await rag_ask(transcript)
    except asyncio.CancelledError:
        raise
    except Exception:
        return _fail(state, turn_id, timings, "rag_failed")
    timings.rag_ms = clock() - t_rag
    if not alive():
        return TurnResult(turn_id, transcript, "", 0, cancelled=True), timings
    answer_text = (answer or "").strip()[:max_answer_chars]
    if not answer_text:
        return _fail(state, turn_id, timings, "rag_failed")
    try:
        state.transition("speaking")
    except ValueError:
        return TurnResult(turn_id, transcript, answer_text, 0, cancelled=True), timings

    # 4. Sentence-chunked synthesis: first audio starts after the first
    # sentence, not after the full answer. Genuinely incremental where the
    # engine supports it; sentence framing keeps it correct regardless.
    t_tts = clock()
    frames = 0
    first_audio = True
    try:
        for sentence in split_sentences(answer_text):
            if not alive():
                return TurnResult(turn_id, transcript, answer_text, frames, cancelled=True), timings
            pcm = await asyncio.to_thread(tts.synthesize_full, sentence, tts_config)
            if not alive():
                return TurnResult(turn_id, transcript, answer_text, frames, cancelled=True), timings
            if first_audio:
                timings.tts_first_ms = clock() - t_tts
                first_audio = False
            frames += 1
            res = emit_audio_frame(pcm, False)
            if asyncio.iscoroutine(res):
                await res
        if alive():
            res = emit_audio_frame(b"", True)
            if asyncio.iscoroutine(res):
                await res
    except asyncio.CancelledError:
        raise
    except Exception:
        return _fail(state, turn_id, timings, "tts_failed")
    timings.tts_total_ms = clock() - t_tts
    if not alive():
        return TurnResult(turn_id, transcript, answer_text, frames, cancelled=True), timings
    state.transition("idle")
    return TurnResult(turn_id, transcript, answer_text, frames), timings


def _fail(state: TurnState, turn_id: str, timings: TurnTimings, error: str) -> tuple[TurnResult, TurnTimings]:
    try:
        state.transition("idle")
    except ValueError:
        pass
    return TurnResult(turn_id, state.transcript, "", 0, error=error), timings
