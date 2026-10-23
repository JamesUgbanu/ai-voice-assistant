"""Incremental pipeline path: segment-ordered synthesis, flush, cancel,
error recovery, and first-audio-before-stream-end. Stubbed collaborators
only — no models, no network, no DB.
"""
import asyncio
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.pipeline import run_turn
from app.rag_client import RagError
from app.stt import StubSttBackend, SttConfig
from app.tts import StubTtsBackend, TtsConfig
from app.turns import TurnState
from app.vad import StubVadBackend


class RecordingTts(StubTtsBackend):
    def __init__(self, timeline=None):
        super().__init__()
        self.segments: list = []
        self.timeline = timeline

    def synthesize_full(self, text, config):
        self.segments.append(text)
        if self.timeline is not None:
            self.timeline.append(f"tts:{text}")
        return super().synthesize_full(text, config)


class FailingTts(StubTtsBackend):
    def synthesize_full(self, text, config):
        raise RuntimeError("tts engine down")


def collecting():
    s = TurnState()
    return s, s.begin_turn()


async def stream_of(chunks, timeline=None, fail_after=None):
    for i, c in enumerate(chunks):
        if fail_after is not None and i == fail_after:
            raise RagError("transport", "stream broke")
        if timeline is not None:
            timeline.append(f"gen:{c}")
        yield c
    if fail_after == len(chunks):
        raise RagError("transport", "stream broke")


class IncrementalTurnTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stt = StubSttBackend("hello james")
        self.tts = RecordingTts()
        self.vad = StubVadBackend(threshold=0.0)
        self.frames: list = []

    async def _emit(self, pcm: bytes, final: bool):
        self.frames.append((pcm, final))

    async def _run(self, state, tid, chunks, tts=None, timeline=None,
                   fail_after=None, emit=None, cancelled=None):
        emit = emit or self._emit
        return await run_turn(
            state=state, turn_id=tid, audio=b"\x10\x00" * 800,
            cancelled=cancelled or asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.5,
            rag_ask=lambda q: None, tts=tts or self.tts, tts_config=TtsConfig(),
            emit_audio_frame=emit, now_ms=lambda: 1000,
            rag_stream=lambda q: stream_of(chunks, timeline=timeline, fail_after=fail_after),
        )

    async def test_segments_emitted_in_order_and_finalized(self):
        s, tid = collecting()
        result, timings = await self._run(
            s, tid, ["Hello ", "world. ", "How are ", "you?"]
        )
        self.assertIsNone(result.error)
        self.assertFalse(result.cancelled)
        self.assertEqual(self.tts.segments, ["Hello world.", "How are you?"])
        self.assertEqual(result.audio_frames, 2)
        self.assertTrue(self.frames[-1][1] is True)  # final frame
        self.assertEqual(result.answer_text, "Hello world. How are you?")
        self.assertEqual(s.state, "idle")

    async def test_unterminated_tail_is_flushed_at_end(self):
        s, tid = collecting()
        result, _ = await self._run(s, tid, ["Just text with no end"])
        self.assertEqual(self.tts.segments, ["Just text with no end"])
        self.assertEqual(result.audio_frames, 1)
        self.assertTrue(self.frames[-1][1] is True)

    async def test_first_audio_before_stream_ends(self):
        timeline: list = []
        s, tid = collecting()
        tts = RecordingTts(timeline)
        await self._run(
            s, tid, ["First sentence. ", "Second sentence. ", "Third sentence."],
            tts=tts, timeline=timeline,
        )
        # The first TTS call must precede the generator's final token.
        first_tts = next(i for i, e in enumerate(timeline) if e.startswith("tts:"))
        last_gen = max(i for i, e in enumerate(timeline) if e.startswith("gen:"))
        self.assertLess(first_tts, last_gen)

    async def test_cancel_mid_stream_discards_remaining_segments(self):
        s, tid = collecting()
        cancelled = asyncio.Event()
        emitted: list = []

        async def emit(pcm, final):
            emitted.append((pcm, final))
            if not final and len([f for f in emitted if not f[1]]) == 1:
                cancelled.set()  # interrupt after the first audio frame

        result, _ = await self._run(
            s, tid,
            ["First sentence. ", "Second sentence. ", "Third sentence."],
            emit=emit, cancelled=cancelled,
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(self.tts.segments, ["First sentence."])
        self.assertFalse(any(final for _, final in emitted))  # no final frame

    async def test_rag_error_after_partial_audio_finalizes(self):
        s, tid = collecting()
        result, _ = await self._run(
            s, tid, ["Hello there. ", "More text"], fail_after=1
        )
        self.assertIsNone(result.error)
        self.assertEqual(self.tts.segments, ["Hello there."])
        self.assertTrue(self.frames[-1][1] is True)

    async def test_rag_error_before_audio_maps_to_rag_failed(self):
        s, tid = collecting()
        result, _ = await self._run(s, tid, [], fail_after=0)
        self.assertEqual(result.error, "rag_failed")
        self.assertEqual(self.frames, [])
        self.assertEqual(s.state, "idle")

    async def test_tts_failure_before_audio_maps_to_tts_failed(self):
        s, tid = collecting()
        result, _ = await self._run(s, tid, ["Hello world. "], tts=FailingTts())
        self.assertEqual(result.error, "tts_failed")
        self.assertEqual(s.state, "idle")

    async def test_tts_failure_after_partial_audio_finalizes(self):
        class PartialFailTts(StubTtsBackend):
            def synthesize_stream(self, text, config, stop=None):
                yield b"\x05\x00" * 10
                raise RuntimeError("decoder died")

        s, tid = collecting()
        frames: list = []

        async def emit(pcm, final):
            frames.append((pcm, final))

        res, _ = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800,
            cancelled=asyncio.Event(), stt=self.stt, stt_config=SttConfig(),
            vad=self.vad, vad_threshold=0.0, rag_ask=lambda q: None,
            rag_stream=lambda q: stream_of(["Hello world. "]),
            tts=PartialFailTts(), tts_config=TtsConfig(), emit_audio_frame=emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        )
        self.assertIsNone(res.error)
        self.assertEqual(len([f for f in frames if not f[1]]), 1)
        self.assertTrue(frames[-1][1])
        self.assertEqual(s.state, "idle")

    async def test_stale_turn_is_cancelled(self):
        s, tid = collecting()
        s.cancel()
        result, _ = await self._run(s, tid, ["Hello world. "])
        self.assertTrue(result.cancelled)
        self.assertEqual(self.tts.segments, [])
        self.assertEqual(self.frames, [])

    async def test_empty_stream_maps_to_rag_failed(self):
        s, tid = collecting()
        result, _ = await self._run(s, tid, [])
        self.assertEqual(result.error, "rag_failed")

    async def test_stalled_stream_flushes_pending_text(self):
        """A quiet upstream must never hold first audio hostage: once text has
        waited past the segmentation max-wait, it is spoken even though the
        RAG stream has not yielded again."""
        s, tid = collecting()
        emitted: list = []

        async def emit(pcm, final):
            if not final and pcm:
                emitted.append(time.monotonic())

        async def stall_stream(_q):
            yield "This is pending text with no terminator"  # no boundary
            await asyncio.sleep(0.6)  # stall longer than the max-wait
            yield " and then it finishes."

        t0 = time.monotonic()
        res, tim = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None, rag_stream=stall_stream, tts=self.tts,
            tts_config=TtsConfig(), emit_audio_frame=emit,
            now_ms=lambda: int(time.monotonic() * 1000),
            segmentation={"max_wait_ms": 150, "min_chars": 5},
        )
        self.assertTrue(emitted, "expected audio flushed during the stream stall")
        self.assertLess(emitted[0] - t0, 0.6, "first audio must arrive before the stream resumes")
        self.assertIsNone(res.error)
        self.assertFalse(res.cancelled)

    async def test_streamed_tts_emits_all_chunks_in_order(self):
        class TwoChunkTts(StubTtsBackend):
            def synthesize_stream(self, text, config, stop=None):
                yield b"\x01\x00" * 10
                yield b"\x02\x00" * 10

        s, tid = collecting()
        frames: list = []

        async def emit(pcm, final):
            frames.append((pcm, final))

        res, tim = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None, rag_stream=lambda q: stream_of(["One. ", "Two. "]),
            tts=TwoChunkTts(), tts_config=TtsConfig(), emit_audio_frame=emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        )
        self.assertIsNone(res.error)
        audio = [f for f in frames if not f[1]]
        # 2 segments x 2 streamed chunks each, plus one final frame.
        self.assertEqual(len(audio), 4)
        self.assertTrue(frames[-1][1] is True)
        self.assertGreater(tim.first_audio_ms, 0)

    async def test_fast_reply_bypasses_rag(self):
        s, tid = collecting()
        called = {"rag": 0}

        async def rag_stream(_q):
            called["rag"] += 1
            yield "should not be used"

        frames: list = []

        async def emit(pcm, final):
            frames.append((pcm, final))

        res, _ = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None, rag_stream=rag_stream,
            fast_reply=lambda text, obj: "Hi there, good to hear from you.",
            tts=self.tts, tts_config=TtsConfig(), emit_audio_frame=emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        )
        self.assertIsNone(res.error)
        self.assertTrue(res.fast_path)
        self.assertEqual(called["rag"], 0)  # RAG never contacted
        self.assertGreaterEqual(res.audio_frames, 1)
        self.assertTrue(frames[-1][1] is True)
        self.assertEqual(res.answer_text, "Hi there, good to hear from you.")

    async def test_rag_total_covers_stream_duration(self):
        """RAG duration must span stream open → close, not just first token:
        the missing half of the bottleneck breakdown next to rag_ms."""
        s, tid = collecting()
        tick = {"t": 0}

        def now_ms():
            tick["t"] += 100
            return tick["t"]

        res, tim = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.5,
            rag_ask=lambda q: None,
            rag_stream=lambda q: stream_of(["Hello world. ", "Second sentence."]),
            tts=self.tts, tts_config=TtsConfig(),
            emit_audio_frame=self._emit, now_ms=now_ms,
        )
        self.assertIsNone(res.error)
        self.assertGreater(tim.rag_total_ms, 0)
        self.assertGreaterEqual(tim.rag_total_ms, tim.rag_ms)

    async def test_timings_capture_first_audio_and_total(self):
        s, tid = collecting()
        tick = {"t": 0}

        def now_ms():
            tick["t"] += 100
            return tick["t"]

        res, tim = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None,
            rag_stream=lambda q: stream_of(["Hello world. ", "Second sentence."]),
            tts=self.tts, tts_config=TtsConfig(),
            emit_audio_frame=self._emit, now_ms=now_ms,
        )
        self.assertIsNone(res.error)
        self.assertGreater(tim.first_audio_ms, 0)
        self.assertGreaterEqual(tim.total_ms, tim.first_audio_ms)
        self.assertGreater(tim.rag_ms, 0)

    async def test_frame_gap_includes_wait_between_segments(self):
        class SlowSecondTts(StubTtsBackend):
            def synthesize_stream(self, text, config, stop=None):
                if "Two" in text:
                    time.sleep(0.15)
                yield self.synthesize_full(text, config)

        s, tid = collecting()
        res, tim = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None,
            rag_stream=lambda q: stream_of(["One. ", "Two. "]),
            tts=SlowSecondTts(), tts_config=TtsConfig(),
            emit_audio_frame=self._emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        )
        self.assertIsNone(res.error)
        # The inter-segment wait is an underrun risk too; it must not be hidden
        # by resetting gap tracking at each segment.
        self.assertGreaterEqual(tim.tts_max_frame_gap_ms, 140)
        self.assertGreaterEqual(tim.tts_underrun_risk_frames, 1)

    async def test_slow_consumer_applies_backpressure_without_loss(self):
        # 200 chunks > the bounded queue (64): the producer must block and the
        # consumer must drain every chunk, in order, with no deadlock.
        class ManyChunkTts(StubTtsBackend):
            def synthesize_stream(self, text, config, stop=None):
                for i in range(200):
                    yield bytes([i % 256, 0])

        s, tid = collecting()
        got: list = []

        async def slow_emit(pcm, final):
            if not final:
                got.append(pcm[0])
                await asyncio.sleep(0.001)  # consumer slower than producer

        res, tim = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None, rag_stream=lambda q: stream_of(["Just one segment. "]),
            tts=ManyChunkTts(), tts_config=TtsConfig(), emit_audio_frame=slow_emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        )
        self.assertIsNone(res.error)
        self.assertEqual(len(got), 200)
        self.assertEqual(got, [i % 256 for i in range(200)])
        # The bounded queue was exercised and the transport cost was measured:
        # this is the evidence needed to distinguish synthesis lag from send
        # stalls during crackle reports.
        self.assertGreater(tim.tts_queue_max, 0)
        self.assertGreater(tim.tts_send_ms, 0)
        self.assertGreater(tim.tts_producer_wait_ms, 0)


    async def test_interrupt_closes_the_rag_stream(self):
        """Interrupt must abort the upstream RAG stream, not just stop audio:
        the generator is closed (its finally runs) so the remote request can
        be torn down and no further tokens are generated."""
        s, tid = collecting()
        cancelled = asyncio.Event()
        closed: list = []
        emitted: list = []

        async def emit(pcm, final):
            emitted.append((pcm, final))

        async def slow_closing_stream(_q):
            try:
                yield "First sentence. "
                await asyncio.sleep(5)  # a silent upstream: interrupt lands here
                yield "Second sentence."
            finally:
                closed.append(True)

        async def canceller():
            await asyncio.sleep(0.05)
            cancelled.set()

        task = asyncio.create_task(canceller())
        res, _ = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=cancelled,
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None, rag_stream=slow_closing_stream, tts=self.tts,
            tts_config=TtsConfig(), emit_audio_frame=emit,
            now_ms=lambda: int(time.monotonic() * 1000),
            segmentation={"max_wait_ms": 200, "min_chars": 5},
        )
        await task
        await asyncio.sleep(0.05)  # let the cancelled __anext__ unwind
        self.assertTrue(res.cancelled)
        self.assertTrue(closed, "RAG stream must be closed on interrupt")
        self.assertFalse(any(final for _, final in emitted), "no final frame after interrupt")

    async def test_interrupt_mid_tts_stream_observes_stop(self):
        """The streaming TTS producer must observe the stop event and stop
        generating, so an interrupted response does not keep synthesizing."""
        class StoppableTts(StubTtsBackend):
            def __init__(self):
                super().__init__()
                self.stop_event = None

            def synthesize_stream(self, text, config, stop=None):
                self.stop_event = stop
                for i in range(60):
                    if stop is not None and stop.is_set():
                        return
                    yield bytes([i % 256, 0])
                    time.sleep(0.002)

        s, tid = collecting()
        cancelled = asyncio.Event()
        emitted: list = []

        async def emit(pcm, final):
            emitted.append((pcm, final))
            if not final:
                cancelled.set()  # interrupt after the first audio frame

        tts = StoppableTts()
        res, _ = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=cancelled,
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None,
            rag_stream=lambda q: stream_of(["One long segment with no terminator"]),
            tts=tts, tts_config=TtsConfig(), emit_audio_frame=emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        )
        self.assertTrue(res.cancelled)
        self.assertTrue(tts.stop_event.is_set(), "TTS producer must receive a set stop event")
        self.assertEqual(len([f for f in emitted if not f[1]]), 1, "only pre-interrupt audio")
        self.assertFalse(any(final for _, final in emitted), "no final frame after interrupt")


    async def test_cancel_while_waiting_for_next_chunk_returns_without_new_chunk(self):
        """Cancellation must not wait for the producer's next chunk. Before the
        queue poll, a silent TTS could hold the turn open until its next yield."""

        class WaitingTts(StubTtsBackend):
            def __init__(self):
                super().__init__()
                self.stop_event = None

            def synthesize_stream(self, text, config, stop=None):
                self.stop_event = stop
                yield b"\x01\x00" * 10
                stop.wait(2.0)
                if stop.is_set():
                    return
                yield b"\x02\x00" * 10

        s, tid = collecting()
        cancelled = asyncio.Event()
        emitted: list = []
        tts = WaitingTts()

        async def emit(pcm, final):
            emitted.append((pcm, final))
            if not final:
                cancelled.set()

        started = time.monotonic()
        res, _ = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=cancelled,
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None,
            rag_stream=lambda q: stream_of(["One long segment with no terminator"]),
            tts=tts, tts_config=TtsConfig(), emit_audio_frame=emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        )
        elapsed = time.monotonic() - started
        self.assertTrue(res.cancelled)
        self.assertTrue(tts.stop_event.is_set())
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len([f for f in emitted if not f[1]]), 1)
        self.assertFalse(any(final for _, final in emitted))

    async def test_cancel_while_producer_blocked_on_full_queue_is_bounded(self):
        class FloodTts(StubTtsBackend):
            def __init__(self):
                super().__init__()
                self.yielded = 0

            def synthesize_stream(self, text, config, stop=None):
                for i in range(500):
                    if stop is not None and stop.is_set():
                        return
                    self.yielded += 1
                    yield bytes([i % 256, 0])

        s, tid = collecting()
        cancelled = asyncio.Event()
        frames: list = []
        tts = FloodTts()

        async def slow_emit(pcm, final):
            frames.append((pcm, final))
            if not final:
                cancelled.set()
                await asyncio.sleep(0.2)  # let the bounded queue fill

        started = time.monotonic()
        res, tim = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=cancelled,
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None, rag_stream=lambda q: stream_of(["Flood segment."]),
            tts=tts, tts_config=TtsConfig(), emit_audio_frame=slow_emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        )
        elapsed = time.monotonic() - started
        self.assertTrue(res.cancelled)
        self.assertLess(elapsed, 1.5)
        self.assertLess(tts.yielded, 200, "producer must stop near the queue bound")
        self.assertGreater(tim.tts_producer_wait_ms, 0)
        self.assertFalse(any(final for _, final in frames))

        # The cancelled queue/producer must not contaminate the next turn.
        class TwoChunkTts(StubTtsBackend):
            def synthesize_stream(self, text, config, stop=None):
                yield b"\x03\x00" * 10
                yield b"\x04\x00" * 10

        s2, tid2 = collecting()
        got: list = []

        async def emit(pcm, final):
            got.append((pcm, final))

        res2, _ = await run_turn(
            state=s2, turn_id=tid2, audio=b"\x10\x00" * 800,
            cancelled=asyncio.Event(), stt=self.stt, stt_config=SttConfig(),
            vad=self.vad, vad_threshold=0.0, rag_ask=lambda q: None,
            rag_stream=lambda q: stream_of(["One. "]), tts=TwoChunkTts(),
            tts_config=TtsConfig(), emit_audio_frame=emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        )
        self.assertIsNone(res2.error)
        self.assertEqual([pcm for pcm, final in got if not final], [b"\x03\x00" * 10, b"\x04\x00" * 10])
        self.assertTrue(got[-1][1])

    async def test_cancel_during_slow_transport_returns_promptly(self):
        class EndlessTts(StubTtsBackend):
            def __init__(self):
                super().__init__()
                self.stop_event = None

            def synthesize_stream(self, text, config, stop=None):
                self.stop_event = stop
                i = 0
                while stop is None or not stop.is_set():
                    yield bytes([i % 256, 0])
                    i += 1

        s, tid = collecting()
        cancelled = asyncio.Event()
        started = asyncio.Event()
        tts = EndlessTts()

        async def emit(pcm, final):
            if final:
                return
            started.set()
            await asyncio.sleep(5)  # a stalled WebSocket send

        task = asyncio.create_task(run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=cancelled,
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None, rag_stream=lambda q: stream_of(["One segment."]),
            tts=tts, tts_config=TtsConfig(), emit_audio_frame=emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        ))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        t0 = time.monotonic()
        cancelled.set()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(tts.stop_event.is_set())


class ProducerContainmentTest(unittest.IsolatedAsyncioTestCase):
    """A backend that ignores `stop` must not accumulate producers.

    Python cannot kill a running thread. The pipeline therefore closes the
    admission gate after the bounded stop timeout and keeps it closed until
    that producer actually exits; subsequent turns fail closed (and do not
    start another synthesis thread), then recover automatically.
    """

    def setUp(self):
        self.stt = StubSttBackend("hello james")
        self.vad = StubVadBackend(threshold=0.0)

    async def _run(self, tts, cancelled=None, emit=None):
        s, tid = collecting()
        return await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800,
            cancelled=cancelled or asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad,
            vad_threshold=0.0, rag_ask=lambda q: None,
            rag_stream=lambda q: stream_of(["One segment. "]),
            tts=tts, tts_config=TtsConfig(),
            emit_audio_frame=emit or (lambda pcm, final: None),
            now_ms=lambda: int(time.monotonic() * 1000),
        )

    async def test_stalled_producer_fails_closed_then_recovers(self):
        class NonCooperativeTts(StubTtsBackend):
            def __init__(self):
                super().__init__()
                self.starts = 0
                self.delay = 0.45
                self.sleeping = threading.Event()
                self.done = threading.Event()
                self.stalled_reports = 0
                self.recovered_reports = 0

            def report_producer_stalled(self):
                self.stalled_reports += 1

            def report_producer_recovered(self):
                self.recovered_reports += 1

            def synthesize_stream(self, text, config, stop=None):
                self.starts += 1
                try:
                    yield b"\x01\x00" * 10
                    self.sleeping.set()
                    time.sleep(self.delay)  # deliberately ignore `stop`
                    yield b"\x02\x00" * 10
                finally:
                    self.done.set()

        tts = NonCooperativeTts()
        cancelled = asyncio.Event()

        async def first_emit(pcm, final):
            if not final:
                # Cancel only once the producer is inside its uncooperative
                # block, so this proves containment rather than cooperative
                # iterator close.
                await asyncio.to_thread(tts.sleeping.wait, 1.0)
                cancelled.set()

        with patch("app.pipeline._PRODUCER_STOP_TIMEOUT_S", 0.12):
            started = time.monotonic()
            res1, _ = await self._run(tts, cancelled=cancelled, emit=first_emit)
            first_elapsed = time.monotonic() - started

        self.assertTrue(res1.cancelled)
        self.assertLess(first_elapsed, 0.6)
        self.assertEqual(tts.starts, 1)
        self.assertEqual(tts.stalled_reports, 1)
        self.assertFalse(tts.done.is_set(), "producer should still be stuck")

        # Repeated turns while the stuck producer lives must not start another
        # synthesis thread; they fail closed promptly.
        started = time.monotonic()
        res2, _ = await self._run(tts)
        second_elapsed = time.monotonic() - started
        self.assertEqual(res2.error, "tts_failed")
        self.assertLess(second_elapsed, 0.4)
        self.assertEqual(tts.starts, 1)

        # Let the original producer exit; the completion callback reopens the
        # gate and the same backend becomes usable again.
        await asyncio.wait_for(asyncio.to_thread(tts.done.wait), timeout=2.0)
        tts.delay = 0.0
        await asyncio.sleep(0.05)
        self.assertEqual(tts.recovered_reports, 1)
        frames = []

        def emit(pcm, final):
            frames.append((pcm, final))

        res3, _ = await self._run(tts, emit=emit)
        self.assertIsNone(res3.error)
        self.assertEqual(tts.starts, 2)
        self.assertTrue(frames[-1][1])


class RagTtfbTimingsTest(unittest.IsolatedAsyncioTestCase):
    """`ttfb_out` sink surfaces into `TurnTimings.rag_ttfb_ms` without changing
    `rag_ms` semantics or the streaming path."""

    def setUp(self):
        self.stt = StubSttBackend("hello james")
        self.vad = StubVadBackend(threshold=0.0)

    async def _run(self, ttfb_out):
        s, tid = collecting()
        frames: list = []

        async def emit(pcm, final):
            frames.append((pcm, final))

        result, timings = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800,
            cancelled=asyncio.Event(), stt=self.stt, stt_config=SttConfig(),
            vad=self.vad, vad_threshold=0.5, rag_ask=lambda q: None,
            rag_stream=lambda q: stream_of(["Hi there."]),
            tts=StubTtsBackend(), tts_config=TtsConfig(),
            emit_audio_frame=emit, now_ms=lambda: 1000, ttfb_out=ttfb_out,
        )
        return result, timings, frames

    async def test_ttfb_populated_from_sink(self):
        _result, timings, _frames = await self._run({"ms": 123})
        self.assertEqual(timings.rag_ttfb_ms, 123)
        # Existing rag_ms semantics unchanged (still measured, non-negative).
        self.assertGreaterEqual(timings.rag_ms, 0)

    async def test_ttfb_zero_when_no_sink(self):
        _result, timings, _frames = await self._run(None)
        self.assertEqual(timings.rag_ttfb_ms, 0)

    async def test_ttfb_zero_when_no_body_byte_arrived(self):
        _result, timings, _frames = await self._run({})  # client never reported
        self.assertEqual(timings.rag_ttfb_ms, 0)


if __name__ == "__main__":
    unittest.main()
