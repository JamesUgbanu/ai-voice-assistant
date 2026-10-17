"""Incremental pipeline path: segment-ordered synthesis, flush, cancel,
error recovery, and first-audio-before-stream-end. Stubbed collaborators
only — no models, no network, no DB.
"""
import asyncio
import os
import sys
import time
import unittest

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
        res, tim = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None,
            rag_stream=lambda q: stream_of(["Hello world. ", "Second sentence."]),
            tts=self.tts, tts_config=TtsConfig(),
            emit_audio_frame=self._emit, now_ms=lambda: int(time.monotonic() * 1000),
        )
        self.assertIsNone(res.error)
        self.assertGreater(tim.first_audio_ms, 0)
        self.assertGreaterEqual(tim.total_ms, tim.first_audio_ms)
        self.assertGreater(tim.rag_ms, 0)


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

        res, _ = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=asyncio.Event(),
            stt=self.stt, stt_config=SttConfig(), vad=self.vad, vad_threshold=0.0,
            rag_ask=lambda q: None, rag_stream=lambda q: stream_of(["Just one segment. "]),
            tts=ManyChunkTts(), tts_config=TtsConfig(), emit_audio_frame=slow_emit,
            now_ms=lambda: int(time.monotonic() * 1000),
        )
        self.assertIsNone(res.error)
        self.assertEqual(len(got), 200)
        self.assertEqual(got, [i % 256 for i in range(200)])


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
                self.saw_stop = False

            def synthesize_stream(self, text, config, stop=None):
                for i in range(60):
                    if stop is not None and stop.is_set():
                        self.saw_stop = True
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
        self.assertTrue(tts.saw_stop, "TTS producer must observe the stop event")
        self.assertEqual(len([f for f in emitted if not f[1]]), 1, "only pre-interrupt audio")
        self.assertFalse(any(final for _, final in emitted), "no final frame after interrupt")


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
