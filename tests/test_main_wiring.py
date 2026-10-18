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
