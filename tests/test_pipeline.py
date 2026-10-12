"""Contract tests: turn orchestration with stubbed collaborators.

No models, no network, no DB. asyncio via stdlib IsolatedAsyncioTestCase.
"""
import asyncio
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.pipeline import run_turn
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


async def _ok_rag(text: str) -> str:
    return f"Answer about {text}."


if __name__ == "__main__":
    unittest.main()
