"""Automatic call termination: completion detection + lifecycle safety.

Pure, deterministic — no models, network, or framework. Covers the acceptance
scenarios at the decision/lifecycle layer the orchestrator enforces.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.conversation_end import (
    CallLifecycle,
    classify_completion,
    ending_wait_seconds,
)


class ClassifyCompletionTest(unittest.TestCase):
    def test_1_explicit_goodbye_ends(self):
        for text in ["Goodbye!", "bye", "okay, bye bye", "See you later.", "talk later", "Take care!"]:
            d = classify_completion(text)
            self.assertTrue(d.should_end_call, text)
            self.assertEqual(d.reason, "goodbye")
            self.assertGreaterEqual(d.confidence, 0.9)

    def test_2_no_further_questions_ends(self):
        for text in [
            "No, that's all. Thanks.",
            "that's everything",
            "I have no further questions",
            "nothing else, thanks",
            "no more questions",
            "I'm good now",
        ]:
            d = classify_completion(text)
            self.assertTrue(d.should_end_call, text)
            self.assertEqual(d.reason, "no_more_questions")

    def test_3_ordinary_question_does_not_end(self):
        for text in [
            "Have you got React experience?",
            "What projects have you built?",
            "Tell me more about MyTreep",
            "How does the RAG work?",
        ]:
            self.assertFalse(classify_completion(text).should_end_call, text)

    def test_4_empty_or_silence_does_not_end(self):
        for text in ["", "   ", "\n"]:
            d = classify_completion(text)
            self.assertFalse(d.should_end_call)
            self.assertEqual(d.reason, "empty")

    def test_5_ambiguous_does_not_end(self):
        # "thanks" alone, thinking aloud, and hedges must not end the call.
        for text in ["thanks", "maybe", "I might have one more", "hold on", "hmm let me think"]:
            self.assertFalse(classify_completion(text).should_end_call, text)

    def test_case_and_punctuation_insensitive(self):
        self.assertTrue(classify_completion("GOODBYE!!!").should_end_call)

    def test_word_boundaries_no_substring_false_positive(self):
        # "goodbyes"/"byes" inside other words must not fire.
        self.assertFalse(classify_completion("the goodbyes were hard").should_end_call)

    def test_threshold_is_enforced(self):
        # A stricter threshold rejects even a clear goodbye (used to disable on
        # a deployment that wants manual-only termination).
        self.assertFalse(classify_completion("goodbye", threshold=0.99).should_end_call)
        self.assertTrue(classify_completion("goodbye", threshold=0.9).should_end_call)


class CallLifecycleTest(unittest.TestCase):
    def test_6_ending_is_reversible_back_to_active(self):
        c = CallLifecycle()
        self.assertTrue(c.begin_ending())   # ACTIVE -> ENDING
        self.assertTrue(c.reversible)
        self.assertTrue(c.cancel_ending())  # user resumed
        self.assertEqual(c.state, CallLifecycle.ACTIVE)
        self.assertFalse(c.reversible)

    def test_7_terminate_is_idempotent_from_any_state(self):
        c = CallLifecycle()
        c.begin_ending()
        self.assertTrue(c.terminate())      # ENDING -> TERMINATED
        self.assertFalse(c.terminate())     # repeated signal is a no-op
        self.assertFalse(c.begin_ending())  # cannot re-enter after termination
        self.assertFalse(c.cancel_ending()) # cannot un-terminate
        self.assertEqual(c.state, CallLifecycle.TERMINATED)

    def test_8_double_end_signal_is_single_transition(self):
        c = CallLifecycle()
        self.assertTrue(c.begin_ending())
        self.assertFalse(c.begin_ending())  # second signal ignored
        self.assertEqual(c.state, CallLifecycle.ENDING)

    def test_9_terminate_directly_from_active(self):
        c = CallLifecycle()
        self.assertTrue(c.terminate())
        self.assertFalse(c.begin_ending())
        self.assertFalse(c.cancel_ending())

    def test_10_new_turn_cannot_inherit_previous_ending_decision(self):
        # A fresh lifecycle per call: ending state never leaks across sessions.
        a = CallLifecycle()
        a.begin_ending()
        a.terminate()
        b = CallLifecycle()
        self.assertEqual(b.state, CallLifecycle.ACTIVE)
        self.assertTrue(b.begin_ending())


class EndingWaitTest(unittest.TestCase):
    def test_bounded_below_and_above(self):
        self.assertGreaterEqual(ending_wait_seconds(0), 4.0)
        self.assertLessEqual(ending_wait_seconds(10_000), 20.0)

    def test_scales_with_audio_length(self):
        short = ending_wait_seconds(10)    # ~0.8s of audio
        long = ending_wait_seconds(100)    # ~8s of audio
        self.assertLess(short, long)

    def test_never_hangs_indefinitely(self):
        # Even a pathological frame count is clamped.
        self.assertLessEqual(ending_wait_seconds(10**6), 20.0)


if __name__ == "__main__":
    unittest.main()
