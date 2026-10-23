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
import logging
import threading
import time
import weakref
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable

from .logging_util import event
from .rag_client import RagError
from .segmentation import SegmentationBuffer
from .turns import TurnState, split_sentences

logger = logging.getLogger("voice")

# How often a consumer/producer re-checks cancellation while waiting on the
# bounded audio queue. Short enough for responsive barge-in, long enough to
# avoid busy-looping while TTS is still generating the next chunk.
_QUEUE_POLL_S = 0.05

# A stopped producer normally exits immediately. If a backend ignores `stop`,
# Python cannot safely kill its thread, so bound how long the turn waits before
# declaring it stalled. The gate below then refuses new synthesis until that
# producer actually exits, instead of spawning more stuck threads/turns.
_PRODUCER_STOP_TIMEOUT_S = 2.0
_PRODUCER_GATE_POLL_S = 0.1


class TtsProducerUnavailable(RuntimeError):
    """A previous TTS producer is still alive after cancellation."""


class _ProducerGate:
    """At most one pipeline-managed TTS producer per backend at a time.

    This is both normal serialization (the model is single-instance) and
    containment for a non-cooperative backend: a producer that outlives the
    stop timeout keeps the gate closed, so repeated turns fail closed instead
    of accumulating worker threads and model-lock waiters. The gate reopens
    only when that producer's future completes.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._sem = asyncio.Semaphore(1)
        self._stalled = False

    async def acquire(self) -> None:
        while True:
            if self._stalled:
                raise TtsProducerUnavailable("tts producer is still shutting down")
            try:
                await asyncio.wait_for(
                    self._sem.acquire(), timeout=_PRODUCER_GATE_POLL_S
                )
            except TimeoutError:
                # Keep polling so a stall declared by the active turn wakes
                # waiters promptly; normal waits remain unbounded (a segment
                # can legitimately take longer than the poll interval).
                continue
            if self._stalled:
                self._sem.release()
                raise TtsProducerUnavailable("tts producer is still shutting down")
            return

    def mark_stalled(self) -> None:
        self._stalled = True

    def release(self) -> None:
        self._stalled = False
        self._sem.release()


# TTS backends are process singletons in production, but tests construct
# several; keying by identity keeps their gates independent.
_PRODUCER_GATES: weakref.WeakKeyDictionary[object, _ProducerGate] = (
    weakref.WeakKeyDictionary()
)


def _producer_gate(tts, loop: asyncio.AbstractEventLoop) -> _ProducerGate:
    gate = _PRODUCER_GATES.get(tts)
    if gate is None or gate.loop is not loop:
        gate = _ProducerGate(loop)
        _PRODUCER_GATES[tts] = gate
    return gate


@dataclass
class TurnResult:
    turn_id: str
    transcript: str
    answer_text: str
    audio_frames: int
    cancelled: bool = False
    error: str | None = None
    # True when a deterministic fast path (e.g. social/greeting) answered without
    # a RAG round trip.
    fast_path: bool = False


@dataclass
class TurnTimings:
    """Stage durations for one turn, in milliseconds, monotonic from turn
    start (end of speech / end_utterance receipt). All fields are plain ints
    so they serialize directly into the structured timing log."""

    stt_ms: int = 0
    # True when the turn reused an exact-length incremental decode instead of
    # running the final STT (identical bytes → identical transcript).
    stt_reused: bool = False
    rag_ms: int = 0
    # RAG request → first response-BODY chunk (time-to-first-byte), reported by
    # the RAG client via `ttfb_out`. Distinct from rag_ms (request → first
    # usable token): TTFB isolates connection/queue/remote-precompute from the
    # post-byte wait to the first token. 0 = not measured (no body byte arrived).
    rag_ttfb_ms: int = 0
    tts_first_ms: int = 0
    tts_total_ms: int = 0
    # RAG first token → first synthesis start: how long the segmentation
    # buffer held text before speaking. The directly-attributable first-audio
    # wait (the first-segment max-wait lever). 0 when the RAG path is not used.
    seg_wait_ms: int = 0
    # RAG first-token → last-token (the tail rag_ms misses); when no token
    # ever arrived, time spent waiting instead, so stalls read nonzero.
    # Stays 0 only if the RAG section is never entered.
    rag_total_ms: int = 0
    # End of speech -> first audio frame emitted (the server-side "first
    # audible" proxy; the browser adds decode + scheduling on top).
    first_audio_ms: int = 0
    total_ms: int = 0
    # Audio-path health. These separate synthesis stalls from transport stalls:
    # queue_max is high-water backpressure, send_ms is time spent in the
    # WebSocket emit callback, frame_gap_max is the longest between-frame gap
    # (a server-side underrun-risk proxy), and producer_wait_ms is time the TTS
    # thread spent blocked because the consumer could not keep up.
    tts_queue_max: int = 0
    tts_send_ms: int = 0
    tts_max_frame_gap_ms: int = 0
    tts_underrun_risk_frames: int = 0
    tts_producer_wait_ms: int = 0


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
    # Temporary P2Y diagnostic capture of the exact Whisper input. None (the
    # default) disables it entirely; a directory path enables WAV+JSON writes
    # for every transcribed turn. Local diagnostics only — never production.
    diag_dir: str | None = None,
    # A decode the caller already holds for EXACTLY this audio (from the
    # incremental partial path). When present it replaces the final STT call;
    # the caller guarantees byte-identical input, so the transcript cannot
    # differ from a fresh decode. None (default) = normal full decode.
    preexisting_transcript: Any | None = None,
    # A typed message can drive the same RAG/TTS turn without STT/VAD.
    text_input: str | None = None,
    # Per-turn sink the RAG stream writes its request→first-body-byte duration
    # into (key "ms"). Scoped to one turn by the caller so concurrent turns can
    # never mix timings. None (default) = no TTFB capture.
    ttfb_out: dict | None = None,
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

    # 1. A typed message bypasses VAD/STT entirely and enters the same
    # transcript → RAG → TTS path. Voice audio still performs the checks below.
    typed = (text_input or "").strip()
    speech_prob = 1.0
    diag_id: str | None = None
    if typed:
        from types import SimpleNamespace
        transcript_obj = SimpleNamespace(text=typed, avg_logprob=None, no_speech=False)
        timings.stt_ms = 0
        timings.stt_reused = False
    else:
        # Server-side voice check: never spend STT/RAG on silence or noise.
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

        # 2. STT in a worker thread (blocking native call). When the incremental
        # path already decoded exactly this audio, reuse that decode: the input
        # bytes are identical, so the transcript is identical, and the turn skips
        # the final decode entirely (the end-of-speech → final latency win).
        reuse = (
            preexisting_transcript
            if (getattr(preexisting_transcript, "text", "") or "").strip()
            else None
        )
        if reuse is not None:
            transcript_obj = reuse
            timings.stt_reused = True
        else:
            try:
                transcript_obj = await asyncio.to_thread(stt.transcribe, audio, stt_config)
            except asyncio.CancelledError:
                raise
            except Exception:
                return _fail(state, turn_id, timings, "stt_failed")
        timings.stt_ms = clock() - t0
    transcript = (transcript_obj.text or "").strip()
    if diag_dir and not timings.stt_reused and not typed:
        # Exact bytes just handed to stt.transcribe, plus everything needed
        # to replay the decision offline. Best-effort: capture must never
        # fail the turn, so errors are swallowed after one attempt.
        try:
            from .diagcapture import write_diag_capture

            cfg = stt_config
            diag_id = write_diag_capture(
                diag_dir,
                audio=audio,
                turn_id=turn_id,
                model_config={
                    "model": getattr(cfg, "model", "?"),
                    "beam_size": getattr(cfg, "beam_size", None),
                    "cpu_threads": getattr(cfg, "cpu_threads", None),
                    "initial_prompt": getattr(cfg, "initial_prompt", ""),
                    "hotwords": getattr(cfg, "hotwords", ""),
                    "temperature": getattr(cfg, "temperature", None),
                    "condition_on_previous_text": getattr(
                        cfg, "condition_on_previous_text", None
                    ),
                },
                vad_prob=speech_prob,
                vad_threshold=vad_threshold,
                transcript=transcript,
                avg_logprob=getattr(transcript_obj, "avg_logprob", None),
                no_speech=bool(getattr(transcript_obj, "no_speech", False)),
                stt_ms=timings.stt_ms,
            )
        except Exception:
            diag_id = None
    event(logger, "turn_stt", turn=turn_id, vad_prob=round(float(speech_prob), 3),
          audio_secs=round(len(audio) / 32000, 2), chars=len(transcript),
          logprob=getattr(transcript_obj, "avg_logprob", None),
          no_speech=bool(getattr(transcript_obj, "no_speech", False)),
          ms=timings.stt_ms, reused=timings.stt_reused, diag_id=diag_id)
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

    # Deterministic fast path (e.g. strict social/greeting): answer locally with a
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
        t_first_token: int | None = None
        seg = segmentation or {}
        buffer = SegmentationBuffer(now_ms=clock, **seg)
        answer_parts: list[str] = []
        # Poll faster than the (possibly shorter) first-segment max-wait so a
        # quiet upstream can never hold pending text — and first audio — hostage.
        poll_wait = min(seg.get("max_wait_ms", 1200), seg.get("first_max_wait_ms", 1200))
        poll_s = max(0.05, min(0.25, poll_wait / 4000.0))
        last_emit_at: int | None = None
        prev_frame_ms = 0

        async def speak(segment: str) -> None:
            nonlocal frames, tts_started, t_first_synth, last_emit_at, prev_frame_ms
            if not alive():
                return
            if state.state != "speaking":
                state.transition("speaking")
            if t_first_synth is None:
                t_first_synth = clock()
                # First-audio wait caused by segmentation, isolated from STT
                # and RAG (measured against the RAG first token).
                if t_first_token is not None:
                    timings.seg_wait_ms = t_first_synth - t_first_token
            # TEMPORARY TTS emission diagnostic (VOICE_TTS_DIAG=1): measures
            # per-segment synthesis span and frame count so inter-segment gaps
            # (segmentation/RAG waits) can be separated from intra-segment
            # synthesis speed. No audio or text content is logged.
            import os as _os

            _diag = _os.environ.get("VOICE_TTS_DIAG", "").strip() == "1"
            _seg_t0 = clock()
            _seg_frames = 0
            # Stream TTS chunks as they are decoded so first audio arrives in
            # a fraction of a second instead of after the whole segment is
            # synthesized. `stop` lets barge-in abort mid-generation. The queue
            # is bounded so a slow/stalled consumer applies backpressure to the
            # synthesizer instead of buffering audio without limit.
            loop = asyncio.get_running_loop()
            gate = _producer_gate(tts, loop)
            try:
                # Admission is asynchronous: a second session/segment waits
                # here instead of creating another thread that blocks on the
                # model lock. If a prior producer stalled, this fails closed.
                await gate.acquire()
            except TtsProducerUnavailable:
                event(logger, "tts_producer_unavailable", turn=turn_id)
                raise
            if not alive():
                gate.release()
                return
            stop = threading.Event()
            queue: "asyncio.Queue" = asyncio.Queue(maxsize=64)
            sentinel = object()
            sample_rate = max(1, int(getattr(tts, "sample_rate", 24000)))

            def put_blocking(item) -> bool:
                """Queue one item without letting a full queue outlive a
                cancelled turn. Returns False when stopped/consumer is gone."""
                try:
                    fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                except Exception:  # noqa: BLE001 - loop closing/consumer gone
                    return False
                while True:
                    try:
                        fut.result(timeout=_QUEUE_POLL_S)
                        return True
                    except FutureTimeout:
                        if stop.is_set() or loop.is_closed():
                            fut.cancel()
                            return False
                    except Exception:  # noqa: BLE001 - failed queue put
                        return False

            _prod = {"synth_ms": 0, "put_ms": 0}

            def produce() -> None:
                it = None
                try:
                    it = tts.synthesize_stream(segment, tts_config, stop=stop)
                    while not stop.is_set():
                        _s = clock()
                        try:
                            chunk = next(it)
                        except StopIteration:
                            break
                        _prod["synth_ms"] += clock() - _s
                        if stop.is_set():
                            break
                        _p = clock()
                        queued = put_blocking(chunk)
                        _prod["put_ms"] += clock() - _p
                        if not queued:
                            break
                except BaseException as exc:  # surfaced to the consumer
                    put_blocking(exc)
                finally:
                    if it is not None:
                        close = getattr(it, "close", None)
                        if callable(close):
                            # Release model/lock resources on abort; close()
                            # is best-effort and must not mask cancellation.
                            with suppress(Exception):
                                close()
                    if not stop.is_set():
                        put_blocking(sentinel)

            producer = None
            gate_released = False

            def release_gate() -> None:
                nonlocal gate_released
                if gate_released:
                    return
                gate_released = True
                report_recovered = getattr(tts, "report_producer_recovered", None)
                if callable(report_recovered):
                    report_recovered()
                gate.release()

            seg_audio_ms = 0
            seg_queue_max = 0
            seg_max_gap = 0
            seg_underruns = 0
            seg_send_ms = 0
            try:
                producer = loop.run_in_executor(None, produce)
                # If the producer outlives this turn, its completion (not the
                # turn) reopens the gate. The callback is idempotent with the
                # normal-path release below.
                def _release_when_done(_f) -> None:
                    with suppress(RuntimeError):
                        loop.call_soon_threadsafe(release_gate)

                producer.add_done_callback(_release_when_done)
                while True:
                    # Do not sleep on an unbounded queue.get(): an interrupt
                    # must stop the producer even when no further chunk arrives.
                    if not alive():
                        stop.set()
                        break
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=_QUEUE_POLL_S
                        )
                    except TimeoutError:
                        continue
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
                    # Include the frame just dequeued: qsize() excludes it.
                    qsize = queue.qsize() + 1
                    seg_queue_max = max(seg_queue_max, qsize)
                    frame_ms = max(1, round((len(item) / 2) / sample_rate * 1000))
                    now = clock()
                    if last_emit_at is not None:
                        gap = max(0, now - last_emit_at)
                        seg_max_gap = max(seg_max_gap, gap)
                        if gap > prev_frame_ms + 60:
                            seg_underruns += 1
                    frames += 1
                    _seg_frames += 1
                    seg_audio_ms += frame_ms
                    send_started = clock()
                    res = emit_audio_frame(item, False)
                    if asyncio.iscoroutine(res):
                        await res
                    seg_send_ms += max(0, clock() - send_started)
                    last_emit_at = clock()
                    prev_frame_ms = frame_ms
            finally:
                stop.set()
                # Drain so a producer blocked on a full queue can observe
                # `stop` and exit; then release its thread. Never wait forever
                # on a backend that ignores `stop`: a stuck synthesis thread
                # must not hold the turn/socket open indefinitely.
                try:
                    while True:
                        queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                if producer is None:
                    release_gate()
                else:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(producer),
                            timeout=_PRODUCER_STOP_TIMEOUT_S,
                        )
                    except TimeoutError:
                        # No safe in-process kill exists for a non-cooperative
                        # backend. Keep the gate closed until the future ends;
                        # meanwhile new TTS work fails closed as `tts_failed`.
                        gate.mark_stalled()
                        report_stalled = getattr(
                            tts, "report_producer_stalled", None
                        )
                        if callable(report_stalled):
                            report_stalled()
                        event(
                            logger,
                            "tts_producer_stalled",
                            turn=turn_id,
                            stop_set=stop.is_set(),
                        )
                    except asyncio.CancelledError:
                        # A socket/turn cancel can arrive while waiting for a
                        # producer that ignores stop. Treat it like a timeout,
                        # but still propagate the cancellation.
                        if not producer.done():
                            gate.mark_stalled()
                            report_stalled = getattr(
                                tts, "report_producer_stalled", None
                            )
                            if callable(report_stalled):
                                report_stalled()
                        raise
                    else:
                        release_gate()
                timings.tts_queue_max = max(timings.tts_queue_max, seg_queue_max)
                timings.tts_send_ms += seg_send_ms
                timings.tts_max_frame_gap_ms = max(
                    timings.tts_max_frame_gap_ms, seg_max_gap
                )
                timings.tts_underrun_risk_frames += seg_underruns
                timings.tts_producer_wait_ms += int(_prod.get("put_ms", 0))
                if _diag:
                    event(
                        logger,
                        "tts_segment",
                        turn=turn_id,
                        chars=len(segment),
                        frames=_seg_frames,
                        audio_ms=seg_audio_ms,
                        span_ms=clock() - _seg_t0,
                        synth_ms=_prod["synth_ms"],
                        put_ms=_prod["put_ms"],
                        queue_max=seg_queue_max,
                        frame_gap_max_ms=seg_max_gap,
                        underrun_risk_frames=seg_underruns,
                        send_ms=seg_send_ms,
                    )

        ait = (_static_stream(fast_text) if fast_text else rag_stream(transcript)).__aiter__()
        fast_path = fast_text is not None
        pending: "asyncio.Future | None" = None
        t_last_token: int | None = None
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
                t_last_token = clock()
                if first_chunk:
                    timings.rag_ms = clock() - t_rag
                    t_first_token = clock()
                    if ttfb_out is not None:
                        ms = ttfb_out.get("ms")
                        if isinstance(ms, int) and ms >= 0:
                            timings.rag_ttfb_ms = ms
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
            # RAG first-token → last-token on every path; when no token ever
            # arrived (hung/failed stream), fall back to time spent waiting so
            # stalls are visible instead of zero.
            timings.rag_total_ms = (
                t_last_token - t_rag if t_last_token is not None else clock() - t_rag
            )
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
