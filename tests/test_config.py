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
        self.assertEqual(s.stt_model, "base.en")
        self.assertEqual(s.stt_beam_size, 1)
        # Domain biasing is on by default: it restores proper-noun/technical
        # accuracy (PostgreSQL, Chidera, Alimosho) that the faster model misses.
        self.assertIn("PostgreSQL", s.stt_initial_prompt)
        self.assertIn("Chidera", s.stt_initial_prompt)
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

    def test_tts_first_segment_wait_default_and_bounds(self):
        # First spoken segment starts sooner; later segments keep the full wait.
        self.assertEqual(Settings(env={}).tts_first_max_wait_ms, 600)
        self.assertEqual(Settings(env={}).tts_max_wait_ms, 1200)
        self.assertEqual(Settings(env={"VOICE_TTS_FIRST_MAX_WAIT_MS": "450"}).tts_first_max_wait_ms, 450)
        self.assertEqual(Settings(env={"VOICE_TTS_FIRST_MAX_WAIT_MS": "50"}).tts_first_max_wait_ms, 100)
        self.assertEqual(Settings(env={"VOICE_TTS_FIRST_MAX_WAIT_MS": "99999"}).tts_first_max_wait_ms, 10000)

    def test_tts_mode_parsing(self):
        # Default is the built-in preset voice; "clone" is opt-in via env and
        # an unknown value fails closed to preset (never a silent clone).
        self.assertEqual(Settings(env={}).tts_mode, "preset")
        self.assertEqual(Settings(env={"VOICE_TTS_MODE": "clone"}).tts_mode, "clone")
        self.assertEqual(Settings(env={"VOICE_TTS_MODE": "CLONE"}).tts_mode, "clone")
        self.assertEqual(Settings(env={"VOICE_TTS_MODE": "weird"}).tts_mode, "preset")
        s = Settings(env={"VOICE_TTS_MODE": "clone",
                          "VOICE_TTS_VOICE_REF": "/srv/voice/ref.wav"})
        self.assertEqual(s.tts_voice_ref, "/srv/voice/ref.wav")
        # Preset voice name is configurable and defaults to the built-in.
        self.assertEqual(Settings(env={}).tts_preset_voice, "alba")

    def test_rag_configured_only_when_complete(self):
        self.assertFalse(Settings(env={"VOICE_RAG_API_URL": "https://x"}).rag_configured)
        self.assertFalse(Settings(env={"VOICE_RAG_SERVICE_TOKEN": "t"}).rag_configured)
        self.assertTrue(Settings(env={"VOICE_RAG_API_URL": "https://x",
                                       "VOICE_RAG_SERVICE_TOKEN": "t"}).rag_configured)

    def test_tts_listen_defaults_and_bounds(self):
        self.assertEqual(Settings(env={}).tts_listen_max_chars, 2000)
        self.assertEqual(Settings(env={"VOICE_TTS_LISTEN_MAX_CHARS": "50"}).tts_listen_max_chars, 100)
        self.assertEqual(Settings(env={"VOICE_TTS_LISTEN_MAX_CHARS": "99999"}).tts_listen_max_chars, 3000)
        self.assertEqual(Settings(env={}).tts_per_minute, 12)

    def test_responder_selection_and_timeout_bounds(self):
        self.assertEqual(Settings(env={}).responder, "portfolio")
        self.assertEqual(Settings(env={"VOICE_RESPONDER": "HTTP"}).responder, "http")
        self.assertEqual(Settings(env={}).responder_timeout_seconds, 25)
        self.assertEqual(
            Settings(env={"VOICE_RESPONDER_TIMEOUT_SECONDS": "1"}).responder_timeout_seconds, 5
        )
        self.assertEqual(
            Settings(env={"VOICE_RESPONDER_TIMEOUT_SECONDS": "999"}).responder_timeout_seconds, 120
        )

    def test_startup_validation_is_actionable_and_never_includes_secrets(self):
        missing = Settings(env={}).validation_errors()
        self.assertTrue(any("VOICE_HMAC_SECRET" in e for e in missing))
        self.assertTrue(any("VOICE_RAG_API_URL" in e for e in missing))

        portfolio_ok = Settings(env={
            "VOICE_HMAC_SECRET": "x" * 32,
            "VOICE_RAG_API_URL": "https://example.test/api/rag",
            "VOICE_RAG_SERVICE_TOKEN": "token",
        })
        self.assertEqual(portfolio_ok.validation_errors(), [])

        http_missing = Settings(env={"VOICE_HMAC_SECRET": "x" * 32, "VOICE_RESPONDER": "http"})
        self.assertTrue(any("VOICE_RESPONDER_URL" in e for e in http_missing.validation_errors()))
        http_ok = Settings(env={
            "VOICE_HMAC_SECRET": "x" * 32,
            "VOICE_RESPONDER": "http",
            "VOICE_RESPONDER_URL": "https://example.test/respond",
        })
        self.assertEqual(http_ok.validation_errors(), [])

        invalid = Settings(env={"VOICE_HMAC_SECRET": "x" * 32, "VOICE_RESPONDER": "nope"})
        self.assertTrue(any("must be 'portfolio' or 'http'" in e for e in invalid.validation_errors()))

        clone = Settings(env={
            "VOICE_HMAC_SECRET": "x" * 32,
            "VOICE_RAG_API_URL": "https://example.test/api/rag",
            "VOICE_RAG_SERVICE_TOKEN": "token",
            "VOICE_TTS_MODE": "clone",
        })
        self.assertTrue(any("VOICE_TTS_VOICE_REF" in e for e in clone.validation_errors()))
        self.assertFalse(any("x" * 32 in e for e in clone.validation_errors()))

    def test_widget_auth_defaults_and_validation(self):
        self.assertEqual(Settings(env={}).widget_auth_mode, "anonymous")
        base = {
            "VOICE_HMAC_SECRET": "x" * 32,
            "VOICE_RAG_API_URL": "https://example.test/api/rag",
            "VOICE_RAG_SERVICE_TOKEN": "token",
        }
        required = Settings(env={**base, "VOICE_WIDGET_AUTH_MODE": "required"})
        self.assertTrue(any("VOICE_WIDGET_TOKEN_SECRET" in e for e in required.validation_errors()))
        ok = Settings(env={
            **base,
            "VOICE_WIDGET_AUTH_MODE": "required",
            "VOICE_WIDGET_TOKEN_SECRET": "s" * 32,
        })
        self.assertEqual(ok.validation_errors(), [])
        short = Settings(env={
            **base,
            "VOICE_WIDGET_TOKEN_SECRET": "short",
        })
        self.assertTrue(any("at least 32" in e for e in short.validation_errors()))
        invalid = Settings(env={**base, "VOICE_WIDGET_AUTH_MODE": "nope"})
        self.assertTrue(any("must be 'anonymous' or 'required'" in e for e in invalid.validation_errors()))


if __name__ == "__main__":
    unittest.main()
