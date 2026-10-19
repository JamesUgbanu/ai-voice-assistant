"""The reference echo backend is stdlib-only and must stay that way."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from examples.echo_backend import build_reply


class EchoBackendTest(unittest.TestCase):
    def test_answers_without_a_backend(self):
        reply = build_reply("What do you do?", [])
        self.assertIn("What do you do?", reply)
        self.assertIn("example echo backend", reply)

    def test_uses_the_previous_question_as_context(self):
        reply = build_reply("And that?", [{"question": "What is RAG?", "answer": "..."}])
        self.assertIn("Earlier you asked", reply)
        self.assertIn("What is RAG?", reply)

    def test_empty_input_is_safe(self):
        self.assertIsInstance(build_reply("", []), str)


if __name__ == "__main__":
    unittest.main()
