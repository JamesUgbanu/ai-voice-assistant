"""Rolling-window incremental STT: scheduling, staleness, isolation,
failure/backpressure, exact-reuse, cleanup, and end-to-end turn shape.

Stubbed backends only — no models, no network, no DB.
"""
import asyncio
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.partial_stt import PartialTranscriber, should_start_partial
from app.pipeline import run_turn
from app.stt import SttConfig, Transcript
from app.tts import StubTtsBackend, TtsConfig
from app.turns import TurnState
from app.vad import StubVadBackend

SR = 16000
#: `b"\x10\x00"` is one PCM16 sample; repeating it SR times is one second.
SEC = b"\x10\x00" * SR


class GrowingStt:
    """Returns a transcript derived from the audio length, so a growing
    buffer produces a changing hypothesis (models a real partial)."""

    def __init__(self, prefix="words"):
        self.calls: list = []
        self.prefix = prefix

    def transcribe(self, audio, config):
        self.calls.append(len(audio))
        seconds = len(audio) // len(SEC)
        return Transcript(text=f"{self.prefix} {seconds}", confidence=0.9, avg_logprob=-0.2)


class RecordingStt:
    def __init__(self, transcript):
        self.transcript = transcript
        self.calls = []

    def transcribe(self, audio, config):
        self.calls.append(len(audio))
        return self.transcript


class BlockingStt:
    """Holds a decode open until released, for in-flight/backpressure tests."""

    def __init__(self, text="late"):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.text = text

    def transcribe(self, audio, config):
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        return Transcript(text=self.text, confidence=0.9, avg_logprob=-0.2)


class BoomStt:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, config):
        self.calls += 1
        raise RuntimeError("engine gone")


def make_partial(stt, emit, **kw):
    params = dict(
        stt=stt,
        stt_config=SttConfig(),
        emit=emit,
        interval_ms=100,
        min_audio_ms=700,
        max_audio_seconds=30,
    )
    params.update(kw)
    return PartialTranscriber(**params)


class PredicateTest(unittest.TestCase):
    """The scheduling rule is pure, so its boundaries are pinned directly."""

    def test_allows_when_every_condition_met(self):
        self.assertTrue(should_start_partial(
            enabled=True, stopped=False, in_flight=False, gate_locked=False,
            audio_bytes=6400, last_considered_bytes=0, min_bytes=6400,
            max_bytes=10**9, now_ms=10_000, last_start_ms=0, interval_ms=100,
        ))

    def test_rejects_each_gate(self):
        base = dict(enabled=True, stopped=False, in_flight=False, gate_locked=False,
                    audio_bytes=6400, last_considered_bytes=0, min_bytes=6400,
                    max_bytes=10**9, now_ms=10_000, last_start_ms=0, interval_ms=100)
        for key, value in (
            ("enabled", False), ("stopped", True), ("in_flight", True),
            ("gate_locked", True), ("audio_bytes", 100), ("audio_bytes", 2 * 10**9),
        ):
            args = dict(base)
            args[key] = value
            self.assertFalse(should_start_partial(**args), key)

    def test_rejects_thin_increment_and_debounce(self):
        base = dict(enabled=True, stopped=False, in_flight=False, gate_locked=False,
                    audio_bytes=10_000, last_considered_bytes=0, min_bytes=6400,
                    max_bytes=10**9, now_ms=10_000, last_start_ms=0, interval_ms=100)
        self.assertFalse(should_start_partial(**{**base, "last_considered_bytes": 5_000}))
        self.assertFalse(should_start_partial(**{**base, "last_start_ms": 9_950}))


class PartialTranscriberTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.emitted: list = []

    async def emit(self, text, seq, turn_id):
        self.emitted.append((text, seq, turn_id))

    async def test_partial_emitted_during_speech_and_text_grows(self):
        stt = GrowingStt()
        p = make_partial(stt, self.emit)
        p.begin("t1")
        p.maybe_start(b"\x10\x00" * (SR // 2), "t1", 0)  # 0.5 s < min → none
        await p.settle(-1)
        self.assertEqual(self.emitted, [])
        p.maybe_start(SEC, "t1", 200)  # 1.0 s
        await p.settle(-1)
        p.maybe_start(SEC + SEC, "t1", 400)  # 2.0 s
        await p.settle(-1)
        self.assertEqual([e[0] for e in self.emitted], ["words 1", "words 2"])
        self.assertEqual([e[1] for e in self.emitted], [1, 2])
        self.assertEqual([e[2] for e in self.emitted], ["t1", "t1"])
        self.assertEqual(p.last.covered_bytes, len(SEC) * 2)

    async def test_debounce_blocks_rapid_restarts(self):
        stt = GrowingStt()
        p = make_partial(stt, self.emit)
        p.begin("t1")
        p.maybe_start(SEC, "t1", 0)
        await p.settle(-1)
        # Same wall-clock window: debounce must suppress a second decode.
        p.maybe_start(SEC + SEC, "t1", 50)
        await p.settle(-1)
        self.assertEqual(len(self.emitted), 1)

    async def test_empty_and_hallucinated_partials_are_not_emitted(self):
        for result in (Transcript(text="   "), Transcript(text="No. No.", no_speech=True)):
            with self.subTest(result=result):
                self.emitted = []
                p = make_partial(RecordingStt(result), self.emit)
                p.begin("t1")
                p.maybe_start(SEC, "t1", 0)
                await p.settle(-1)
                self.assertEqual(self.emitted, [])
                self.assertIsNone(p.last)

    async def test_superseded_turn_never_emits(self):
        stt = BlockingStt("stale")
        p = make_partial(stt, self.emit)
        p.begin("t1")
        p.maybe_start(SEC, "t1", 0)
        await asyncio.to_thread(stt.started.wait, 2)
        p.begin("t2")  # a new turn supersedes t1 while t1's decode is open
        stt.release.set()
        await p.settle(-1)
        self.assertEqual(self.emitted, [])

    async def test_stt_failure_is_recoverable_and_reported(self):
        errors: list = []
        p = make_partial(BoomStt(), self.emit, on_error=errors.append)
        p.begin("t1")
        p.maybe_start(SEC, "t1", 0)
        await p.settle(-1)
        self.assertEqual(self.emitted, [])
        self.assertEqual(errors, ["RuntimeError"])

    async def test_single_flight_applies_backpressure(self):
        stt = BlockingStt("busy")
        p = make_partial(stt, self.emit)
        p.begin("t1")
        p.maybe_start(SEC, "t1", 0)
        await asyncio.to_thread(stt.started.wait, 2)
        # New audio arrives while a decode is in flight: no new decode starts.
        p.maybe_start(SEC + SEC + SEC, "t1", 1000)
        self.assertTrue(p.in_flight)
        self.assertEqual(stt.calls, 1)
        stt.release.set()
        await p.settle(-1)

    async def test_gate_prevents_overlapping_partials(self):
        gate = asyncio.Semaphore(1)
        await gate.acquire()
        p = make_partial(GrowingStt(), self.emit, gate=gate)
        p.begin("t1")
        p.maybe_start(SEC, "t1", 0)
        self.assertFalse(p.in_flight)
        gate.release()
        p.maybe_start(SEC + SEC, "t1", 1000)
        self.assertTrue(p.in_flight)
        await p.settle(-1)

    async def test_settle_reuses_only_exact_coverage(self):
        p = make_partial(GrowingStt(), self.emit)
        p.begin("t1")
        p.maybe_start(SEC, "t1", 0)
        # Final buffer grew after the partial: a suffix was never decoded, so
        # settling must refuse to reuse (trailing words preserved).
        self.assertIsNone(await p.settle(len(SEC) + 400))
        # Re-arm and let a partial cover the whole (now final) buffer.
        p.begin("t2")
        p.maybe_start(SEC + SEC, "t2", 0)
        reused = await p.settle(len(SEC) * 2)
        self.assertIsNotNone(reused)
        self.assertEqual(reused.text, "words 2")

    async def test_stop_disarms_and_clears(self):
        p = make_partial(GrowingStt(), self.emit)
        p.begin("t1")
        await p.stop()
        p.maybe_start(SEC, "t1", 0)  # disarmed → no task
        self.assertFalse(p.in_flight)
        self.assertIsNone(p.last)
        self.assertEqual(self.emitted, [])

    async def test_disabled_emits_nothing(self):
        p = make_partial(GrowingStt(), self.emit, enabled=False)
        p.begin("t1")
        p.maybe_start(SEC, "t1", 0)
        self.assertFalse(p.in_flight)
        self.assertEqual(self.emitted, [])

    async def test_max_window_bounds_cost(self):
        # 2 s cap; a 3 s buffer must not start a decode.
        p = make_partial(GrowingStt(), self.emit, max_audio_seconds=2)
        p.begin("t1")
        p.maybe_start(SEC + SEC + SEC, "t1", 0)
        self.assertFalse(p.in_flight)

    async def test_instances_are_isolated(self):
        a_events: list = []
        b_events: list = []

        async def emit_a(text, seq, tid):
            a_events.append((text, tid))

        async def emit_b(text, seq, tid):
            b_events.append((text, tid))

        pa = make_partial(GrowingStt("a"), emit_a)
        pb = make_partial(GrowingStt("b"), emit_b)
        pa.begin("ta")
        pb.begin("tb")
        pa.maybe_start(SEC, "ta", 0)
        pb.maybe_start(SEC, "tb", 0)
        await pa.settle(-1)
        await pb.settle(-1)
        self.assertEqual(a_events, [("a 1", "ta")])
        self.assertEqual(b_events, [("b 1", "tb")])


class StreamingTurnIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """The intended turn shape end to end: partials while speaking, one
    exact-reuse final that skips STT, and exactly one RAG request."""

    async def test_partials_then_reuse_then_single_rag(self):
        stt = GrowingStt()
        emitted: list = []

        async def emit(text, seq, tid):
            emitted.append(text)

        p = make_partial(stt, emit)
        p.begin("t1")
        # User speaks; audio accumulates and partials stream out.
        p.maybe_start(SEC, "t1", 0)
        await p.settle(-1)
        p.maybe_start(SEC + SEC, "t1", 200)
        reused = await p.settle(len(SEC) * 2)
        await p.stop()
        self.assertEqual(emitted, ["words 1", "words 2"])
        self.assertIsNotNone(reused)

        state = TurnState()
        tid = state.begin_turn()
        frames: list = []
        rag_calls: list = []

        async def emit_audio(pcm, final):
            frames.append((pcm, final))

        async def rag_stream(text):
            rag_calls.append(text)
            yield "Here is the answer."

        result, timings = await run_turn(
            state=state, turn_id=tid, audio=SEC + SEC,
            cancelled=asyncio.Event(), stt=stt, stt_config=SttConfig(),
            vad=StubVadBackend(threshold=0.0), vad_threshold=0.5,
            rag_ask=lambda q: None, rag_stream=rag_stream,
            tts=StubTtsBackend(), tts_config=TtsConfig(),
            emit_audio_frame=emit_audio, preexisting_transcript=reused,
            now_ms=lambda: 1000,
        )
        self.assertTrue(timings.stt_reused)
        self.assertEqual(result.transcript, "words 2")
        self.assertEqual(rag_calls, ["words 2"])  # exactly one RAG request
        self.assertTrue(frames and frames[-1][1] is True)


if __name__ == "__main__":
    unittest.main()
