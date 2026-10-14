"""Contract tests: turn orchestration with stubbed collaborators.

No models, no network, no DB. asyncio via stdlib IsolatedAsyncioTestCase.
"""
import asyncio
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.pipeline import run_turn, turn_end_reason
from app.stt import StubSttBackend, SttConfig
from app.tts import StubTtsBackend, TtsConfig
from app.turns import TurnState
from app.vad import StubVadBackend


def collecting():
    s = TurnState()
    tid = s.begin_turn()
    return s, tid


class RunTurnTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stt = StubSttBackend("hello james")
        self.tts = StubTtsBackend()
        self.vad = StubVadBackend(threshold=0.0)
        self.stt_config = SttConfig()
        self.tts_config = TtsConfig()
        self.frames: list = []

    async def _emit(self, pcm: bytes, final: bool):
        self.frames.append((pcm, final))

    async def _run(self, state, tid, audio, **kw):
        cancelled = asyncio.Event()
        return await run_turn(
            state=state, turn_id=tid, audio=audio, cancelled=cancelled,
            stt=kw.get("stt", self.stt), stt_config=self.stt_config, vad=self.vad,
            vad_threshold=0.5, rag_ask=kw.get("rag_ask", _ok_rag),
            tts=self.tts, tts_config=self.tts_config,
            emit_audio_frame=self._emit,
            now_ms=lambda: kw.get("now", 1000),
            on_transcript=kw.get("on_transcript", None),
        )

    async def test_full_turn_emits_speakable_audio(self):
        s, tid = collecting()
        result, timings = await self._run(s, tid, b"\x10\x00" * 800)
        self.assertFalse(result.cancelled)
        self.assertIsNone(result.error)
        self.assertEqual(result.transcript, "hello james")
        self.assertTrue(result.audio_frames >= 1)
        self.assertTrue(self.frames and self.frames[-1][1] is True)
        self.assertEqual(s.state, "idle")
        self.assertGreaterEqual(timings.stt_ms, 0)

    async def test_silence_never_reaches_stt_or_rag(self):
        calls = []
        s, tid = collecting()

        async def rag_ask(text):
            calls.append(text)
            return "x"

        result, _ = await self._run(s, tid, b"\x00" * 1600, rag_ask=rag_ask)
        self.assertEqual(result.transcript, "")
        self.assertEqual(self.stt.calls, [])
        self.assertEqual(calls, [])
        self.assertEqual(self.frames, [])
        self.assertEqual(s.state, "idle")

    async def test_cancel_mid_turn_stops_everything(self):
        s, tid = collecting()
        cancelled = asyncio.Event()
        cancelled.set()

        async def slow_rag(text):
            await asyncio.sleep(30)
            return "late"

        result, _ = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=cancelled,
            stt=self.stt, stt_config=self.stt_config, vad=self.vad,
            vad_threshold=0.0, rag_ask=slow_rag, tts=self.tts,
            tts_config=self.tts_config, emit_audio_frame=self._emit,
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(self.frames, [])

    async def test_stale_turn_results_are_dropped(self):
        s, tid = collecting()
        s.cancel()
        result, _ = await self._run(s, tid, b"\x10\x00" * 800)
        self.assertTrue(result.cancelled)
        self.assertEqual(self.frames, [])

    async def test_failures_map_to_error_codes(self):
        from app.stt import SttBackend

        class Boom(SttBackend):
            def transcribe(self, audio, config):
                raise RuntimeError("model gone")

        s, tid = collecting()
        cancelled = asyncio.Event()
        result, _ = await run_turn(
            state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=cancelled,
            stt=Boom(), stt_config=self.stt_config, vad=self.vad,
            vad_threshold=0.0, rag_ask=_ok_rag, tts=self.tts,
            tts_config=self.tts_config, emit_audio_frame=self._emit,
        )
        self.assertEqual(result.error, "stt_failed")
        self.assertEqual(s.state, "idle")

    async def test_rag_failure_never_fabricates(self):
        async def rag_boom(text):
            raise ConnectionError("rag down")

        s, tid = collecting()
        result, _ = await self._run(s, tid, b"\x10\x00" * 800, rag_ask=rag_boom)
        self.assertEqual(result.error, "rag_failed")
        self.assertEqual(result.answer_text, "")
        self.assertEqual(self.frames, [])

    async def test_empty_transcript_skips_rag(self):
        from app.stt import SttBackend

        class Silent(SttBackend):
            def transcribe(self, audio, config):
                from app.stt import Transcript
                return Transcript(text="   ")

        calls = []

        async def rag_ask(text):
            calls.append(text)
            return "x"

        s, tid = collecting()
        result, _ = await self._run(s, tid, b"\x10\x00" * 800, rag_ask=rag_ask, stt=Silent())
        self.assertEqual(calls, [])
        self.assertEqual(result.transcript, "")

    async def test_no_speech_transcript_skips_rag(self):
        # A hallucination-guarded backend reports no_speech for garbage audio:
        # no RAG request, no audio, clean idle — the client returns to
        # listening instead of answering a hallucination.
        from app.stt import SttBackend

        class Guarded(SttBackend):
            def transcribe(self, audio, config):
                from app.stt import Transcript
                return Transcript(text="", no_speech=True, avg_logprob=-1.5)

        calls = []

        async def rag_ask(text):
            calls.append(text)
            return "x"

        s, tid = collecting()
        result, _ = await self._run(s, tid, b"\x10\x00" * 800, rag_ask=rag_ask, stt=Guarded())
        self.assertEqual(calls, [])
        self.assertEqual(result.transcript, "")
        self.assertFalse(result.cancelled)
        self.assertIsNone(result.error)
        self.assertEqual(self.frames, [])
        self.assertEqual(s.state, "idle")

    async def test_cancelled_turn_cannot_cancel_next(self):
        # Interrupting turn 1 must not poison turn 2: each turn observes only
        # its own cancellation event, so the next utterance starts clean.
        s1, tid1 = collecting()
        cancelled = asyncio.Event()
        cancelled.set()
        r1, _ = await run_turn(
            state=s1, turn_id=tid1, audio=b"\x10\x00" * 800, cancelled=cancelled,
            stt=self.stt, stt_config=self.stt_config, vad=self.vad,
            vad_threshold=0.0, rag_ask=_ok_rag, tts=self.tts,
            tts_config=self.tts_config, emit_audio_frame=self._emit,
        )
        self.assertTrue(r1.cancelled)
        s2, tid2 = collecting()
        r2, _ = await self._run(s2, tid2, b"\x10\x00" * 800)
        self.assertFalse(r2.cancelled)
        self.assertIsNone(r2.error)
        self.assertEqual(r2.transcript, "hello james")
        self.assertTrue(self.frames and self.frames[-1][1] is True)

    async def test_transcript_hook_fires_once_with_final_text(self):
        seen = []
        s, tid = collecting()
        result, _ = await self._run(s, tid, b"\x10\x00" * 800, on_transcript=seen.append)
        self.assertFalse(result.cancelled)
        self.assertEqual(seen, ["hello james"])

    async def test_transcript_hook_skipped_for_empty_transcript(self):
        from app.stt import SttBackend

        class Silent(SttBackend):
            def transcribe(self, audio, config):
                from app.stt import Transcript
                return Transcript(text="")

        seen = []
        s, tid = collecting()
        await self._run(s, tid, b"\x10\x00" * 800, stt=Silent(), on_transcript=seen.append)
        self.assertEqual(seen, [])


async def _ok_rag(text: str) -> str:
    return f"Answer about {text}."


class TurnEndReasonTest(unittest.TestCase):
    """Regression: turn_end must come from the result, not state liveness.

    A completed turn has already transitioned to idle, so the old
    `state.is_current(turn_id)` gate was false and silently suppressed
    turn_end(complete) and turn_end(error) — the client hung 'speaking' or
    waited forever."""

    def _r(self, **kw):
        from app.pipeline import TurnResult
        base = dict(turn_id="t1", transcript="hello", answer_text="hi", audio_frames=1)
        base.update(kw)
        return TurnResult(**base)

    def test_success_is_complete(self):
        self.assertEqual(turn_end_reason(self._r()), "complete")

    def test_error_is_surfaced(self):
        self.assertEqual(turn_end_reason(self._r(error="stt_failed")), "stt_failed")
        self.assertEqual(turn_end_reason(self._r(error="rag_failed")), "rag_failed")

    def test_cancelled_emits_nothing(self):
        self.assertIsNone(turn_end_reason(self._r(cancelled=True)))

    def test_empty_transcript(self):
        self.assertEqual(turn_end_reason(self._r(transcript="")), "empty")


if __name__ == "__main__":
    unittest.main()
