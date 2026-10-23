"""Production-path social conversation tests.

These drive the real FastAPI WebSocket endpoint with stub STT/TTS and a fake
availability client. They prove the fix in the path users hit: final
transcript -> fast reply -> emit text/audio, not just the helper in isolation.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _FakeResponse:
    status_code = 200

    def json(self):
        return {
            "enabled": True,
            "configured": True,
            "timezone": "Africa/Lagos",
            "durationMinutes": 30,
            "slots": [
                {
                    "start": "2099-10-19T09:00:00Z",
                    "label": "Mon 19 Oct, 10:00",
                    "durationMinutes": 30,
                }
            ],
        }


class _FakeHttpClient:
    calls = 0

    async def get(self, url, params=None, timeout=None):
        type(self).calls += 1
        return _FakeResponse()


class SocialConversationIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="voice-social-test-")
        os.environ.update({
            "VOICE_USE_STUBS": "1",
            "VOICE_HMAC_SECRET": "test-secret-" + "x" * 32,
            "VOICE_RAG_API_URL": "http://127.0.0.1:9/api/rag",
            "VOICE_RAG_SERVICE_TOKEN": "token",
            "VOICE_BOOKING_ONLY": "1",
            "VOICE_DB_PATH": os.path.join(cls.tmpdir, "test.sqlite3"),
            "VOICE_TTS_MODE": "preset",
            "VOICE_ALLOWED_ORIGINS": "http://testserver",
            "VOICE_STT_STREAMING": "0",
            "VOICE_SESSION_ATTEMPTS": "100",
        })

        try:
            from fastapi.testclient import TestClient
        except ImportError as exc:
            raise unittest.SkipTest("fastapi/httpx not installed") from exc

        from app import main

        cls.main = main

        async def fake_http_client():
            return _FakeHttpClient()

        cls.http_patch = mock.patch.object(main, "http_client", fake_http_client)
        cls.http_patch.start()
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.http_patch.stop()
        db = getattr(cls.main, "_db", None)
        if db is not None:
            db.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _FakeHttpClient.calls = 0
        response = self.client.post("/v1/sessions", json={})
        self.assertEqual(response.status_code, 200)
        self.ticket = response.json()["ticket"]
        self.ws_cm = self.client.websocket_connect(
            "/v1/voice", headers={"origin": "http://testserver"}
        )
        self.ws = self.ws_cm.__enter__()
        self.addCleanup(self.ws_cm.__exit__, None, None, None)
        self.ws.send_json({
            "v": 1, "type": "hello", "seq": 0, "turn_id": None,
            "payload": {"ticket": self.ticket},
        })
        self.assertEqual(self.ws.receive_json()["type"], "session_created")
        self.assertEqual(self.ws.receive_json()["type"], "listening")
        self.seq = 0

    def _ask(self, text):
        self.seq += 1
        self.ws.send_json({
            "v": 1, "type": "text_message", "seq": self.seq, "turn_id": None,
            "payload": {"text": text},
        })
        answer = None
        while True:
            message = self.ws.receive_json()
            kind = message["type"]
            if kind == "audio_frame":
                continue
            if kind == "assistant_text":
                answer = message["payload"]["text"]
            elif kind == "error":
                self.fail(f"turn error: {message['payload']}")
            elif kind == "turn_end":
                self.assertEqual(message["payload"]["reason"], "complete")
                break
        self.assertIsInstance(answer, str)
        return answer

    def test_reported_reproduction_gets_social_replies(self):
        first = self._ask("Hello, how are you doing?")
        self.assertIn("doing well", first.lower())
        self.assertNotIn("book", first.lower())
        self.assertNotEqual(first, self.main.booking_flow.booking_guidance_message())

        second = self._ask("I said, how are you doing?")
        self.assertTrue(second.startswith("You're right, I missed that."))
        self.assertIn("doing well", second.lower())
        self.assertNotIn("book", second.lower())

    def test_greeting_and_pleasantry_variants(self):
        cases = {
            "Good morning": "Good morning!",
            "How's it going?": "doing well",
            "Thank you": "You're welcome!",
            "Nice to meet you": "Nice to meet you",
            "I'm good, thanks": "Glad to hear it",
        }
        for text, expected in cases.items():
            reply = self._ask(text)
            self.assertIn(expected, reply, text)
            self.assertNotIn("book", reply.lower(), text)

    def test_social_phrase_with_booking_intent_still_books(self):
        reply = self._ask("Good morning, I want to book a call.")
        self.assertEqual(_FakeHttpClient.calls, 1)
        self.assertIn("I have this time", reply)
        self.assertNotEqual(reply, self.main.booking_flow.booking_guidance_message())

    def test_unrelated_substantive_question_keeps_booking_guardrail(self):
        reply = self._ask("What projects has James built?")
        self.assertEqual(reply, self.main.booking_flow.booking_guidance_message())
        self.assertEqual(_FakeHttpClient.calls, 0)

    def test_social_then_booking_preserves_multi_turn_flow(self):
        social = self._ask("Hello, how are you doing?")
        self.assertIn("doing well", social.lower())
        booking = self._ask("I want to book a call")
        self.assertEqual(_FakeHttpClient.calls, 1)
        self.assertIn("I have this time", booking)


if __name__ == "__main__":
    unittest.main()
