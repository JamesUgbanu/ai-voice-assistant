"""Multi-turn voice integration: history forwarding + booking across turns.

These tests exercise the same composition the WebSocket handler uses:
one TurnState, the pipeline, and a bounded history list that is passed to
the RAG call and appended only after a completed turn. No models/network.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.booking import VoiceBookingSession
from app.history import append_turn
from app.pipeline import run_turn
from app.rag_client import build_rag_body
from app.stt import StubSttBackend, SttConfig
from app.tts import StubTtsBackend, TtsConfig
from app.turns import TurnState
from app.vad import StubVadBackend

TZ = "Africa/Lagos"


class FakeRag:
    """Stands in for the portfolio RAG stream: records the question and the
    history it received, and answers using that history."""

    def __init__(self):
        self.questions: list[str] = []
        self.histories: list[list[dict]] = []

    async def stream(self, question: str, history: list[dict]):
        self.questions.append(question)
        self.histories.append(list(history))
        prior = " ".join(t["question"].lower() for t in history)
        if "voice service" in prior:
            yield "I built it with Python and FastAPI."
        else:
            yield "I have built several portfolio projects."


class MultiTurnConversationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tts = StubTtsBackend()
        self.vad = StubVadBackend(threshold=0.0)
        self.state = TurnState()
        self.history: list[dict] = []
        self.rag = FakeRag()
        self.frames: list = []

    async def _emit(self, pcm: bytes, final: bool):
        self.frames.append((pcm, final))

    async def _turn(self, text: str):
        tid = self.state.begin_turn()
        result, _ = await run_turn(
            state=self.state,
            turn_id=tid,
            audio=b"",
            cancelled=asyncio.Event(),
            stt=StubSttBackend(),  # unused for typed input
            stt_config=SttConfig(),
            vad=self.vad,
            vad_threshold=0.5,
            rag_ask=None,
            rag_stream=lambda q: self.rag.stream(q, list(self.history)),
            tts=self.tts,
            tts_config=TtsConfig(),
            emit_audio_frame=self._emit,
            now_ms=lambda: 1000,
            text_input=text,
        )
        if not result.cancelled and not result.error:
            append_turn(self.history, result.transcript, result.answer_text)
        return result

    async def test_follow_up_receives_preceding_turns_and_no_duplicate(self):
        first = await self._turn("What projects has James built?")
        self.assertFalse(first.cancelled)
        self.assertEqual(self.rag.histories[0], [])

        second = await self._turn("What about his voice service?")
        self.assertFalse(second.cancelled)

        # The follow-up is answered with the first turn as context.
        self.assertEqual(self.rag.questions, [
            "What projects has James built?",
            "What about his voice service?",
        ])
        self.assertEqual(self.rag.histories[1], [{
            "question": "What projects has James built?",
            "answer": "I have built several portfolio projects.",
        }])
        # The current utterance must never appear inside its own history.
        self.assertNotIn("What about his voice service?",
                         [t["question"] for t in self.rag.histories[1]])
        # And the history carries the earlier answer into the next request.
        body = build_rag_body("What about his voice service?", list(self.history))
        self.assertEqual(len(body["history"]), 2)
        self.assertEqual(body["history"][0]["question"], "What projects has James built?")

    async def test_ordered_history_survives_many_turns(self):
        await self._turn("Question one about React")
        await self._turn("Question two about AWS")
        await self._turn("Question three about RAG")
        questions = [t["question"] for t in self.history]
        self.assertEqual(questions, [
            "Question one about React",
            "Question two about AWS",
            "Question three about RAG",
        ])
        # Each request saw only the turns that preceded it, in order.
        self.assertEqual([len(h) for h in self.rag.histories], [0, 1, 2])


class BookingConversationTest(unittest.TestCase):
    OFFER = {
        "enabled": True,
        "timezone": TZ,
        "durationMinutes": 30,
        "slots": [
            {"start": "2026-10-22T09:00:00Z", "label": "Thu 22 Oct, 09:00", "durationMinutes": 30},
            {"start": "2026-10-22T10:00:00Z", "label": "Thu 22 Oct, 10:00", "durationMinutes": 30},
        ],
    }

    def test_gathers_details_one_turn_at_a_time_and_submits_once(self):
        s = VoiceBookingSession()
        s.capture_offer(self.OFFER)

        reply, payload = s.handle("the 10:00 one please", TZ)
        self.assertIn("name", reply or "")
        self.assertIsNone(payload)

        reply, payload = s.handle("My name is Ada", TZ)
        self.assertIn("email", reply or "")
        self.assertIsNone(payload)

        reply, payload = s.handle("ada@example.com", TZ)
        self.assertIn("Should I book it?", reply or "")
        self.assertIsNone(payload)

        reply, payload = s.handle("yes", TZ)
        self.assertIsNone(reply)
        self.assertEqual(payload["slotStart"], "2026-10-22T10:00:00Z")
        self.assertEqual(payload["name"], "Ada")
        self.assertEqual(payload["email"], "ada@example.com")
        self.assertEqual(payload["timeZone"], TZ)

        # A repeated confirmation cannot produce a second booking payload.
        self.assertEqual(s.handle("yes", TZ), (None, None))

    def test_uncertain_submission_keeps_idempotency_for_safe_retry(self):
        from app.booking import result_message

        s = VoiceBookingSession()
        s.capture_offer(self.OFFER)
        s.handle("first", TZ)
        s.handle("Ada, ada@example.com", TZ)
        _, payload = s.handle("yes", TZ)
        self.assertEqual(payload["idempotencyKey"], s.idempotency)

        # The provider result was uncertain: no success may be spoken, and the
        # key must survive so a retry reconciles instead of double-booking.
        s.mark_uncertain()
        self.assertEqual(s.stage, "confirm")

        # A different day must not discard the only key that can reconcile the
        # unknown submission, which could otherwise lead to a second booking.
        blocked, blocked_payload = s.handle("What about Friday?", TZ)
        self.assertIn("verify the earlier request", blocked or "")
        self.assertIsNone(blocked_payload)
        self.assertEqual(s.idempotency, payload["idempotencyKey"])
        blocked, _ = s.handle("What projects has James built?", TZ)
        self.assertIn("verify the earlier request", blocked or "")
        self.assertEqual(s.idempotency, payload["idempotencyKey"])
        blocked, _ = s.handle("no thanks", TZ)
        self.assertIn("verify the earlier request", blocked or "")
        self.assertEqual(s.stage, "confirm")
        self.assertEqual(s.idempotency, payload["idempotencyKey"])

        retry_reply, retry_payload = s.handle("yes", TZ)
        self.assertIsNone(retry_reply)
        self.assertEqual(retry_payload["idempotencyKey"], payload["idempotencyKey"])
        message = result_message({"status": "uncertain"}, s.slot or {}, TZ).lower()
        self.assertNotIn("confirmed", message)

    def test_topic_change_and_restart_release_booking(self):
        s = VoiceBookingSession()
        s.capture_offer(self.OFFER)
        reply, payload = s.handle("What projects has James built with React?", TZ)
        self.assertIsNone(reply)
        self.assertIsNone(payload)
        self.assertEqual(s.stage, "idle")

        s.capture_offer(self.OFFER)
        s.handle("first", TZ)
        reply, payload = s.handle("start over", TZ)
        self.assertIsNone(reply)
        self.assertIsNone(payload)
        self.assertEqual(s.stage, "idle")

    def test_weekday_change_does_not_select_another_days_slot(self):
        s = VoiceBookingSession()
        s.capture_offer(self.OFFER)  # Thursday slots
        reply, payload = s.handle("Friday at 10am", TZ)
        self.assertIsNone(reply)
        self.assertIsNone(payload)
        self.assertEqual(s.stage, "idle")

        # The same weekday (Thursday) still selects by clock time.
        s.capture_offer(self.OFFER)
        reply, payload = s.handle("Thursday at 10:00", TZ)
        self.assertIn("name", reply or "")
        self.assertEqual(s.slot["start"], "2026-10-22T10:00:00Z")

    def test_provider_response_contract(self):
        from app.booking import parse_booking_response

        self.assertEqual(
            parse_booking_response(201, {"status": "confirmed"})["status"], "confirmed")
        self.assertEqual(
            parse_booking_response(200, {"status": "failed"})["status"], "failed")
        self.assertEqual(
            parse_booking_response(200, {})["status"], "failed")
        self.assertEqual(
            parse_booking_response(202, {"status": "confirmed"})["status"], "uncertain")
        self.assertEqual(
            parse_booking_response(409, {})["status"], "slot_unavailable")
        # A server error can never be promoted to a confirmation, even if the
        # body wrongly claims one.
        self.assertEqual(
            parse_booking_response(500, {"status": "confirmed"})["status"], "failed")
        self.assertEqual(
            parse_booking_response(200, "not-a-dict")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
