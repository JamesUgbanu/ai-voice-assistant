"""Static wiring checks for main.py, which cannot be imported without FastAPI.

The pure unit suite runs on stdlib only; these checks catch accidental removal
of imports/helpers that the WebSocket handshake and typed-turn path require.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


class MainWiringTest(unittest.TestCase):
    def setUp(self):
        self.source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
        self.tree = ast.parse(self.source)
        self.imported: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                self.imported.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                self.imported.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def test_handshake_history_and_booking_helpers_are_imported(self):
        self.assertIn("clean_history", self.imported)
        self.assertIn("booking_flow", self.imported)
        self.assertIn("clean_history(payload.get(\"history\"))", self.source)

    def test_each_turn_forwards_history_and_voice_mode(self):
        # Both the streaming and non-streaming RAG calls carry the session's
        # bounded history; the current utterance is not pre-appended.
        self.assertIn("history=list(history)", self.source)
        self.assertIn('mode="voice"', self.source)
        self.assertIn("append_turn(history, result.transcript, result.answer_text)", self.source)

    def test_uncertain_booking_retains_idempotency(self):
        # Never confirm an uncertain outcome; keep the key for a safe retry.
        self.assertIn("booking_flow.parse_booking_response", self.source)
        self.assertIn('result.get("status") == "uncertain"', self.source)
        self.assertIn("booking.mark_uncertain()", self.source)

    def test_generic_responder_is_opt_in_and_does_not_replace_the_portfolio_path(self):
        self.assertIn("HttpResponder", self.source)
        self.assertIn('settings.responder == "http"', self.source)
        self.assertIn("http_responder.stream(", self.source)
        # The portfolio RAG/booking path remains the default branch.
        self.assertIn("rc.iter_answer(", self.source)
        self.assertIn("booking.handle(", self.source)
        # Startup validation must run before serving.
        self.assertIn("settings.validation_errors()", self.source)
        self.assertIn("Invalid voice-service configuration", self.source)

    def test_booking_only_reads_availability_directly(self):
        self.assertIn("call_availability_api", self.source)
        self.assertIn("settings.booking_availability_url", self.source)
        self.assertIn("booking_flow.parse_availability_response", self.source)
        self.assertIn("booking_flow.prepare_offer", self.source)
        # Containment: booking-only never falls through to portfolio Q&A.
        self.assertIn("settings.booking_only", self.source)
        self.assertIn("booking_flow.booking_guidance_message()", self.source)

    def test_playback_metrics_are_validated_before_logging(self):
        self.assertIn("validate_playback_metrics", self.source)
        self.assertIn('etype == "playback_metrics"', self.source)
        self.assertIn("metric(\"playback_metrics_received\")", self.source)

    def test_widget_tokens_are_optional_and_project_scoped(self):
        self.assertIn("verify_widget_token", self.source)
        self.assertIn('request.headers.get("x-widget-token", "")', self.source)
        self.assertIn("session_visitor_id", self.source)
        self.assertIn("f\"{scope}{visitor_id}\"", self.source)
        self.assertIn("settings.widget_auth_mode", self.source)
        # The browser session POST carries the widget token, so the CORS
        # preflight must allow that header.
        self.assertIn('"X-Widget-Token"', self.source)

    def test_startup_tts_warmup_synthesizes_not_just_preflights(self):
        self.assertIn('warm = getattr(tts, "warm", None)', self.source)
        self.assertIn("warm(tts_cfg)", self.source)

    def test_typed_turn_uses_the_same_pipeline_without_stt(self):
        self.assertIn('etype == "text_message"', self.source)
        self.assertIn("text_input=text_", self.source)
        self.assertIn("run_utterance(tid_, b\"\", None", self.source)

    def test_listen_to_response_is_ticket_authed_and_bounded(self):
        self.assertIn('@app.post("/v1/tts/ticket")', self.source)
        self.assertIn('@app.post("/v1/tts")', self.source)
        self.assertIn("TTS_SUBJECT_PREFIX", self.source)
        self.assertIn("X-Voice-Ticket", self.source)
        self.assertIn("tts_listen_max_chars", self.source)
        self.assertIn("synthesize_full", self.source)
        self.assertIn("Cache-Control", self.source)


if __name__ == "__main__":
    unittest.main()
