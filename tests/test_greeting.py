"""Deterministic greeting fast path: precision, false positives, confidence gate."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.greeting import greeting_reply, is_greeting, normalize


class GreetingMatchTest(unittest.TestCase):
    def test_common_greetings_match(self):
        for text in ["Hello.", "hello", "Hi!", "Hey", "hello there", "Hey there, James",
                     "Good morning", "GOOD EVENING", "How are you?", "how are you doing?",
                     "How's it going?", "What's up?", "Nice to meet you."]:
            self.assertIsNotNone(greeting_reply(text), text)

    def test_greeting_with_extra_content_does_not_match(self):
        # The critical false-positive guards.
        for text in [
            "Hello, can you explain my RAG architecture?",
            "Hi, what has James built with AWS?",
            "Hey, how can I contact James?",
            "Hello, tell me about TaxBloc",
            "Good morning, what projects has he shipped?",
            "Hello how are you doing today and what do you do",
        ]:
            self.assertIsNone(greeting_reply(text), text)
            self.assertFalse(is_greeting(text), text)

    def test_low_confidence_transcript_is_not_a_greeting(self):
        self.assertIsNotNone(greeting_reply("hello", avg_logprob=-0.3))
        self.assertIsNone(greeting_reply("hello", avg_logprob=-1.5))

    def test_empty_and_whitespace(self):
        self.assertIsNone(greeting_reply(""))
        self.assertIsNone(greeting_reply("   "))
        self.assertIsNone(greeting_reply("..."))

    def test_normalize(self):
        self.assertEqual(normalize("Hello,   there!?"), "hello there")
        self.assertEqual(normalize("How's it going?"), "hows it going")

    def test_reply_text_is_short_and_natural(self):
        for text in ["hello", "good morning", "how are you?"]:
            reply = greeting_reply(text)
            self.assertIsInstance(reply, str)
            self.assertLessEqual(len(reply), 120)
            self.assertNotIn("RAG", reply)


if __name__ == "__main__":
    unittest.main()
