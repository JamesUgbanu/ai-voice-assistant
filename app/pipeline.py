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
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .turns import TurnState, split_sentences
from .segmentation import SegmentationBuffer
from .rag_client import RagError
from .logging_util import event
import logging

logger = logging.getLogger("voice")


@dataclass
class TurnResult:
    turn_id: str
    transcript: str
    answer_text: str
    audio_frames: int
    cancelled: bool = False
    error: str | None = None
    # True when a deterministic fast path (e.g. a greeting) answered without
    # a RAG round trip.
    fast_path: bool = False


@dataclass
class TurnTimings:
    """Stage durations for one turn, in milliseconds, monotonic from turn
    start (end of speech / end_utterance receipt). All fields are plain ints
    so they serialize directly into the structured timing log."""

    stt_ms: int = 0
    rag_ms: int = 0
    tts_first_ms: int = 0
    tts_total_ms: int = 0
    # End of speech -> first audio frame emitted (the server-side "first
    # audible" proxy; the browser adds decode + scheduling on top).
    first_audio_ms: int = 0
    total_ms: int = 0


def turn_end_reason(result: "TurnResult") -> str | None:
    """The `turn_end` reason to send for a finished turn, or None when no
    turn_end should be sent because the turn was superseded/interrupted
    (the interrupt path already emitted its own turn_end).

    This exists because a completed turn has *already* transitioned the
    TurnState back to idle, so any `state.is_current(turn_id)` check run
    after it is false — gating the completion event on it silently swallowed
    `turn_end` and left the client stuck 'speaking'. The decision must come
    from the result alone."""
    if result.cancelled:
        return None
    if result.error:
        return result.error
    if not result.transcript:
        return "empty"
    return "complete"


def _silent_pcm16(duration_seconds: float, sample_rate: int = 16000) -> bytes:
    return b"\x00" * int(duration_seconds * sample_rate) * 2


async def _static_stream(text: str):
    """One-shot async source for the deterministic fast path."""
    yield text


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
    on_transcript: Callable[[str], Any] | None = None,
    rag_stream=None,
    segmentation: dict | None = None,
    fast_reply: Callable[[str, Any], str | None] | None = None,
) -> tuple[TurnResult, TurnTimings]:
    """Execute one turn. Pure orchestration; all side effects via callables.
    `emit_audio_frame(pcm_bytes, final)` may be sync or async. `on_transcript`
    fires once with the finalized transcript (may be sync or async)."""
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
    event(logger, "turn_stt", turn=turn_id, vad_prob=round(float(speech_prob), 3),
          audio_secs=round(len(audio) / 32000, 2), chars=len(transcript),
          logprob=getattr(transcript_obj, "avg_logprob", None),
          no_speech=bool(getattr(transcript_obj, "no_speech", False)),
          ms=timings.stt_ms)
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
    if on_transcript is not None:
        res = on_transcript(transcript)
        if asyncio.iscoroutine(res):
            await res

    # Deterministic fast path (e.g. a strict greeting): answer locally with a
    # short reply instead of a remote RAG round trip. Strict and confidence
    # gated upstream; returns None for anything with extra content.
    fast_text: str | None = None
    if fast_reply is not None:
        try:
            fast_text = fast_reply(transcript, transcript_obj)
        except Exception:
            fast_text = None

    # 3s. Incremental path (preferred). Consume the RAG token stream, split
    # it into natural segments with the bounded buffer, and synthesize each
    # segment as soon as it is ready — first audio starts before the answer
    # finishes. Cancellation is checked before every synthesis and after the
    # stream, and the buffer is discarded on cancel so no stale audio plays.
    if rag_stream is not None:
        t_tts = clock()
        t_rag = clock()
        frames = 0
        tts_started = False
        first_chunk = True
        t_first_synth: int | None = None
        seg = segmentation or {}
        buffer = SegmentationBuffer(now_ms=clock, **seg)
        answer_parts: list[str] = []
        # Poll faster than the segmentation max-wait so a quiet upstream can
        # never hold pending text (and therefore first audio) hostage.
        poll_s = max(0.05, min(0.25, seg.get("max_wait_ms", 1200) / 4000.0))

        async def speak(segment: str) -> None:
            nonlocal frames, tts_started, t_first_synth
            if not alive():
                return
            if state.state != "speaking":
                state.transition("speaking")
            if t_first_synth is None:
                t_first_synth = clock()
            # Stream TTS chunks as they are decoded so first audio arrives in
            # a fraction of a second instead of after the whole segment is
            # synthesized. `stop` lets barge-in abort mid-generation. The queue
            # is bounded so a slow/stalled consumer applies backpressure to the
            # synthesizer instead of buffering audio without limit.
            stop = threading.Event()
            queue: "asyncio.Queue" = asyncio.Queue(maxsize=64)
            sentinel = object()
            loop = asyncio.get_running_loop()

            def put_blocking(item) -> None:
                try:
                    asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()
                except Exception:
                    pass  # loop closing / consumer gone

            def produce() -> None:
                try:
                    for chunk in tts.synthesize_stream(segment, tts_config, stop=stop):
                        if stop.is_set():
                            break
                        put_blocking(chunk)
                except BaseException as exc:  # surfaced to the consumer
                    put_blocking(exc)
                finally:
                    put_blocking(sentinel)

            producer = loop.run_in_executor(None, produce)
            try:
                while True:
                    item = await queue.get()
                    if item is sentinel:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    if not alive():
                        stop.set()
                        break
                    if not tts_started:
                        # Pure TTS latency for the first streamed chunk.
                        timings.tts_first_ms = clock() - t_first_synth
                        tts_started = True
                    if timings.first_audio_ms == 0:
                        timings.first_audio_ms = clock() - t0
                    frames += 1
                    res = emit_audio_frame(item, False)
                    if asyncio.iscoroutine(res):
                        await res
            finally:
                stop.set()
                # Drain so a producer blocked on a full queue can observe
                # `stop` and exit; then release its thread.
                try:
                    while True:
                        queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                await producer

        ait = (_static_stream(fast_text) if fast_text else rag_stream(transcript)).__aiter__()
        fast_path = fast_text is not None
        pending: "asyncio.Future | None" = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(ait.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=poll_s)
                if not done:
                    # Upstream is quiet right now: flush text that already
                    # waited past the segmentation max-wait, then keep waiting.
                    # This bounds first audio even when the RAG stream stalls.
                    if not alive():
                        break
                    for segment in buffer.poll(clock()):
                        await speak(segment)
                        if not alive():
                            break
                    continue
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    pending = None
                    break
                except Exception:
                    pending = None
                    raise
                pending = None
                if first_chunk:
                    timings.rag_ms = clock() - t_rag
                    first_chunk = False
                if not alive():
                    buffer.reset()
                    return TurnResult(turn_id, transcript, "".join(answer_parts), frames, cancelled=True, fast_path=fast_path), timings
                answer_parts.append(chunk)
                for segment in buffer.push(chunk, now_ms=clock()):
                    await speak(segment)
                    if not alive():
                        buffer.reset()
                        return TurnResult(turn_id, transcript, "".join(answer_parts), frames, cancelled=True, fast_path=fast_path), timings
            if alive():
                for segment in buffer.flush():
                    await speak(segment)
            if alive():
                res = emit_audio_frame(b"", True)
                if asyncio.iscoroutine(res):
                    await res
        except asyncio.CancelledError:
            buffer.reset()
            raise
        except RagError:
            if frames == 0:
                return _fail(state, turn_id, timings, "rag_failed")
            # Some audio already played: finalize instead of failing outright.
            if alive():
                res = emit_audio_frame(b"", True)
                if asyncio.iscoroutine(res):
                    await res
        except Exception:
            if frames == 0:
                return _fail(state, turn_id, timings, "tts_failed")
            if alive():
                res = emit_audio_frame(b"", True)
                if asyncio.iscoroutine(res):
                    await res
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
        timings.tts_total_ms = clock() - t_tts
        timings.total_ms = clock() - t0
        answer_text = "".join(answer_parts).strip()[:max_answer_chars]
        if not alive():
            return TurnResult(turn_id, transcript, answer_text, frames, cancelled=True, fast_path=fast_path), timings
        if not answer_text:
            return _fail(state, turn_id, timings, "rag_failed")
        state.transition("idle")
        return TurnResult(turn_id, transcript, answer_text, frames, fast_path=fast_path), timings

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
            if timings.first_audio_ms == 0:
                timings.first_audio_ms = clock() - t0
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
    timings.total_ms = clock() - t0
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
