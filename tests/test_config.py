"""Contract tests: settings defaults, bounds, and secret hygiene."""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import Settings


class ConfigTest(unittest.TestCase):
    def test_safe_defaults(self):
        s = Settings(env={})
        self.assertEqual(s.max_call_seconds, 300)
        self.assertEqual(s.daily_allowance_seconds, 600)
        self.assertEqual(s.session_attempts, 3)
        self.assertEqual(s.attempt_window_seconds, 600)
        self.assertEqual(s.stale_session_seconds, 600)
        self.assertEqual(s.sample_rate, 16000)
        self.assertEqual(s.stt_model, "small.en")
        self.assertEqual(s.vad_source, "snakers4/silero-vad")
        self.assertEqual(
            Settings(env={"VOICE_VAD_SOURCE": "local:/srv/voice/vendor/silero"}).vad_source,
            "local:/srv/voice/vendor/silero",
        )
        self.assertEqual(s.tts_language, "english")
        self.assertEqual(s.allowed_origins, ["https://www.jamesugbanu.com"])
        self.assertFalse(s.rag_configured)

    def test_bounds_and_invalid_values(self):
        s = Settings(env={"VOICE_MAX_CALL_SECONDS": "5", "VOICE_DAILY_ALLOWANCE_SECONDS": "abc",
                          "VOICE_SESSION_ATTEMPTS": "0", "VOICE_STALE_SESSION_SECONDS": "30"})
        self.assertEqual(s.max_call_seconds, 60)      # clamped to minimum
        self.assertEqual(s.daily_allowance_seconds, 600)  # invalid -> default
        self.assertEqual(s.session_attempts, 1)       # clamped to minimum
        self.assertEqual(s.stale_session_seconds, 120)

    def test_rag_configured_only_when_complete(self):
        self.assertFalse(Settings(env={"VOICE_RAG_API_URL": "https://x"}).rag_configured)
        self.assertFalse(Settings(env={"VOICE_RAG_SERVICE_TOKEN": "t"}).rag_configured)
        self.assertTrue(Settings(env={"VOICE_RAG_API_URL": "https://x",
                                       "VOICE_RAG_SERVICE_TOKEN": "t"}).rag_configured)


if __name__ == "__main__":
    unittest.main()
