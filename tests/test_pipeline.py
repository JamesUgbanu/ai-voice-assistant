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
        # Exactly one transcription per turn: duplicates would double billed
        # latency on the slowest stage for zero benefit.
        self.assertEqual(len(self.stt.calls), 1)

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

    async def test_session_usable_after_a_silence_utterance(self):
        """Silence must not poison the session: no STT/RAG/audio, back to idle,
        and the very next valid utterance on the same session still works."""
        s, tid = collecting()
        r1, _ = await self._run(s, tid, b"\x00" * 1600)
        self.assertEqual(r1.transcript, "")
        self.assertEqual(r1.audio_frames, 0)
        self.assertEqual(self.stt.calls, [])  # silence never reached STT
        self.assertEqual(s.state, "idle")
        # Same session, next turn: begin fresh and run real audio.
        s.begin_turn()
        r2, _ = await self._run(s, s.turn_id, b"\x10\x00" * 800)
        self.assertFalse(r2.cancelled)
        self.assertEqual(r2.transcript, "hello james")
        self.assertEqual(len(self.stt.calls), 1)
        self.assertTrue(self.frames and self.frames[-1][1] is True)

    async def test_confident_misrecognition_is_forwarded_verbatim(self):
        """A fluent but wrong transcript (dropped "not"; misheard technical
        name) carries no detectable signal, so the pipeline must NOT silently
        rewrite or substitute it, and must never fabricate an answer. The
        transcript reaches RAG exactly as STT produced it, and only the
        grounded answer is spoken."""
        from app.stt import SttBackend, Transcript

        for wrong in (
            "I did configure the production database",   # "not" dropped
            "Tell me about AWS Lander",                   # Lambda misheard
            "Do you have fifty years of experience",      # 15 -> 50
        ):
            class Wrong(SttBackend):
                def __init__(self, text):
                    self.text = text

                def transcribe(self, audio, config):
                    return Transcript(text=self.text, confidence=0.9, avg_logprob=-0.2)

            seen = []
            self.frames = []

            async def rag_ask(text):
                seen.append(text)
                return "Grounded, evidence-based answer."

            s, tid = collecting()
            r, _ = await self._run(s, tid, b"\x10\x00" * 800, stt=Wrong(wrong), rag_ask=rag_ask)
            # Verbatim: not corrected, not replaced, no stale substitution.
            self.assertEqual(seen, [wrong])
            self.assertEqual(r.transcript, wrong)
            # Only the grounded answer is spoken; nothing invented.
            self.assertEqual(r.answer_text, "Grounded, evidence-based answer.")
            self.assertTrue(self.frames and self.frames[-1][1] is True)

    async def test_diag_capture_writes_whisper_input(self):
        import asyncio
        import json
        import os
        import tempfile
        import wave

        from app.stt import SttConfig

        with tempfile.TemporaryDirectory() as tmp:
            s, tid = collecting()
            cancelled = asyncio.Event()
            result, _ = await run_turn(
                state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=cancelled,
                stt=self.stt, stt_config=SttConfig(), vad=self.vad,
                vad_threshold=0.5, rag_ask=_ok_rag, tts=self.tts,
                tts_config=self.tts_config, emit_audio_frame=self._emit,
                diag_dir=tmp,
            )
            self.assertEqual(result.transcript, "hello james")
            names = os.listdir(tmp)
            wavs = [n for n in names if n.endswith(".wav")]
            jsons = [n for n in names if n.endswith(".json")]
            self.assertEqual(len(wavs), 1)
            self.assertEqual(len(jsons), 1)
            self.assertEqual(
                wavs[0][:-4], jsons[0][:-5],
                "WAV and sidecar share the diagnostic ID",
            )
            with wave.open(os.path.join(tmp, wavs[0]), "rb") as fh:
                self.assertEqual((fh.getnchannels(), fh.getsampwidth(), fh.getframerate()), (1, 2, 16000))
            with open(os.path.join(tmp, jsons[0])) as fh:
                sidecar = json.load(fh)
            self.assertEqual(sidecar["turn_id"], tid)
            self.assertEqual(sidecar["result"]["transcript"], "hello james")

    async def test_diag_capture_disabled_writes_nothing(self):
        import asyncio
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "caps")
            s, tid = collecting()
            cancelled = asyncio.Event()
            await run_turn(
                state=s, turn_id=tid, audio=b"\x10\x00" * 800, cancelled=cancelled,
                stt=self.stt, stt_config=self.stt_config, vad=self.vad,
                vad_threshold=0.5, rag_ask=_ok_rag, tts=self.tts,
                tts_config=self.tts_config, emit_audio_frame=self._emit,
                diag_dir=None,
            )
            self.assertFalse(os.path.exists(target))

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


class ReuseTranscriptTest(unittest.IsolatedAsyncioTestCase):
    """The incremental path's exact-reuse contract: a decode the caller holds
    for byte-identical audio replaces the final STT — no extra call, same
    result, transcript still emitted exactly once."""

    def setUp(self):
        self.stt = StubSttBackend("fresh decode")
        self.tts = StubTtsBackend()
        self.vad = StubVadBackend(threshold=0.0)
        self.frames: list = []

    async def _emit(self, pcm, final):
        self.frames.append((pcm, final))

    async def _run(self, state, tid, **kw):
        return await run_turn(
            state=state, turn_id=tid, audio=b"\x10\x00" * 800,
            cancelled=asyncio.Event(), stt=self.stt, stt_config=SttConfig(),
            vad=self.vad, vad_threshold=0.5,
            rag_ask=kw.get("rag_ask", _ok_rag), tts=self.tts,
            tts_config=TtsConfig(), emit_audio_frame=self._emit,
            now_ms=lambda: 1000, on_transcript=kw.get("on_transcript"),
            fast_reply=kw.get("fast_reply"), rag_stream=kw.get("rag_stream"),
            preexisting_transcript=kw.get("preexisting"),
        )

    async def test_reused_transcript_skips_final_stt(self):
        from app.stt import Transcript

        s, tid = collecting()
        seen = []
        result, timings = await self._run(
            s, tid, preexisting=Transcript(text="words 2", confidence=0.9, avg_logprob=-0.2),
            on_transcript=seen.append,
        )
        self.assertEqual(self.stt.calls, [])  # final STT skipped
        self.assertTrue(timings.stt_reused)
        self.assertEqual(result.transcript, "words 2")
        self.assertEqual(seen, ["words 2"])  # emitted exactly once
        self.assertTrue(self.frames and self.frames[-1][1] is True)

    async def test_reuse_preserves_greeting_confidence(self):
        from app.stt import Transcript

        captured: dict = {}

        def fast_reply(text, obj):
            captured["logprob"] = getattr(obj, "avg_logprob", None)
            return "Hi there."

        async def never_stream(_q):
            if False:
                yield ""

        s, tid = collecting()
        result, _ = await self._run(
            s, tid, preexisting=Transcript(text="Hello.", confidence=1.0, avg_logprob=-0.1),
            fast_reply=fast_reply, rag_stream=never_stream,
        )
        # The reuse must carry the decode's confidence, not lose it — the
        # greeting fast path is confidence-gated.
        self.assertEqual(captured["logprob"], -0.1)
        self.assertTrue(result.fast_path)
        self.assertEqual(self.stt.calls, [])

    async def test_empty_preexisting_falls_back_to_stt(self):
        from app.stt import Transcript

        s, tid = collecting()
        result, timings = await self._run(s, tid, preexisting=Transcript(text="   "))
        self.assertEqual(len(self.stt.calls), 1)  # real decode runs
        self.assertFalse(timings.stt_reused)
        self.assertEqual(result.transcript, "fresh decode")

    async def test_reuse_still_makes_one_rag_request(self):
        from app.stt import Transcript

        calls: list = []

        async def rag_stream(text):
            calls.append(text)
            yield "Grounded answer."

        state = TurnState()
        tid = state.begin_turn()
        result, _ = await run_turn(
            state=state, turn_id=tid, audio=b"\x10\x00" * 800,
            cancelled=asyncio.Event(), stt=self.stt, stt_config=SttConfig(),
            vad=self.vad, vad_threshold=0.5, rag_ask=lambda q: None,
            rag_stream=rag_stream, tts=self.tts, tts_config=TtsConfig(),
            emit_audio_frame=self._emit, now_ms=lambda: 1000,
            preexisting_transcript=Transcript(text="words 2"),
        )
        self.assertEqual(calls, ["words 2"])
        self.assertIsNone(result.error)
        self.assertTrue(self.frames[-1][1] is True)


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
