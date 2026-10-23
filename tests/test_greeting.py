"""Deterministic social/greeting fast path: precision, repair, confidence gate."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.greeting import greeting_reply, is_greeting, normalize  # noqa: I001


SOCIAL_PHRASES = [
    "Hello.", "hello", "Hi!", "Hey", "hello there", "Hey there, James",
    "Good morning", "GOOD EVENING", "How are you?", "how are you doing?",
    "Hello, how are you doing?", "Hi there, how's it going?",
    "How you doing?", "How're you doing?", "How have you been?",
    "How's it going?", "What's up?", "Nice to meet you.",
    "Thank you.", "Thanks so much.", "Good afternoon, how are you?",
    "I said, how are you doing?", "I said, how you doing?", "I'm good, thanks.", "Not bad.",
    "Doing well, thank you.",
]


class GreetingMatchTest(unittest.TestCase):
    def test_common_greetings_match(self):
        for text in SOCIAL_PHRASES:
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
            "How you doing today and what do you do?",
            "I said, can you explain the architecture?",
            "I asked what projects has James built?",
            "That's not what I asked, what does James do?",
            "Thank you, can I book a call?",
            "Good morning, can we book a call?",
            "That's great, can we book a call?",
        ]:
            self.assertIsNone(greeting_reply(text), text)
            self.assertFalse(is_greeting(text), text)

    def test_social_replies_are_natural_and_booking_free(self):
        for text in ["hello", "good morning", "how are you?", "thank you",
                     "nice to meet you", "good evening, how are you?",
                     "I'm good, thanks", "not bad", "doing well"]:
            reply = greeting_reply(text)
            self.assertIsInstance(reply, str)
            self.assertLessEqual(len(reply), 120)
            self.assertNotIn("RAG", reply)
            self.assertNotIn("book", reply.lower())
            self.assertNotIn("calendar", reply.lower())

    def test_repair_acknowledges_and_answers(self):
        reply = greeting_reply("I said, how are you doing?")
        self.assertTrue(reply.startswith("You're right, I missed that."))
        self.assertIn("doing well", reply)
        self.assertEqual(greeting_reply("thank you"), "You're welcome!")
        self.assertEqual(
            greeting_reply("I said, thank you"),
            "You're right, I missed that. You're welcome!",
        )
        self.assertEqual(greeting_reply("I'm good, thanks"), "Glad to hear it. How can I help?")

    def test_low_confidence_transcript_is_not_social(self):
        self.assertIsNotNone(greeting_reply("hello", avg_logprob=-0.3))
        self.assertIsNone(greeting_reply("hello", avg_logprob=-1.5))
        self.assertIsNone(greeting_reply("I said hello", avg_logprob=-1.5))

    def test_empty_and_whitespace(self):
        self.assertIsNone(greeting_reply(""))
        self.assertIsNone(greeting_reply("   "))
        self.assertIsNone(greeting_reply("..."))
        self.assertIsNone(greeting_reply("I said"))

    def test_normalize(self):
        self.assertEqual(normalize("Hello,   there!?"), "hello there")
        self.assertEqual(normalize("How's it going?"), "hows it going")
        self.assertEqual(normalize("I said, how are you?"), "i said how are you")


if __name__ == "__main__":
    unittest.main()
