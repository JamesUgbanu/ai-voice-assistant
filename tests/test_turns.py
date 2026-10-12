"""Contract tests: turn state machine, sentence chunking, cancellation."""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.turns import TurnState, split_sentences


class TurnStateTest(unittest.TestCase):
    def test_happy_path_transitions(self):
        s = TurnState()
        tid = s.begin_turn()
        s.add_audio(320, 1_000_000)
        s.transition("processing")
        s.transition("speaking")
        self.assertTrue(s.is_current(tid))
        s.transition("idle")

    def test_illegal_transitions_rejected(self):
        s = TurnState()
        with self.assertRaises(ValueError):
            s.transition("speaking")
        with self.assertRaises(ValueError):
            s.add_audio(10, 1000)

    def test_turn_cap_enforced(self):
        s = TurnState()
        s.begin_turn()
        with self.assertRaises(OverflowError):
            s.add_audio(2000, 1000)

    def test_cancel_invalidates_and_reports(self):
        s = TurnState()
        tid = s.begin_turn()
        s.transition("processing")
        self.assertEqual(s.cancel(), tid)
        self.assertFalse(s.is_current(tid))
        self.assertIsNone(s.cancel())

    def test_begin_supersedes_previous_turn(self):
        s = TurnState()
        old = s.begin_turn()
        s.transition("processing")
        new = s.begin_turn()
        self.assertNotEqual(old, new)
        self.assertFalse(s.is_current(old))
        self.assertEqual(s.state, "collecting")


class SplitSentencesTest(unittest.TestCase):
    def test_splits_on_sentence_boundaries(self):
        out = split_sentences("Hello there. How are you? I am fine!")
        self.assertEqual(out, ["Hello there.", "How are you?", "I am fine!"])

    def test_breaks_pathological_sentences(self):
        out = split_sentences("x" * 1000, max_chars=400)
        self.assertTrue(all(len(c) <= 400 for c in out))
        self.assertEqual("".join(out).replace(" ", ""), "x" * 1000)

    def test_empty_and_whitespace(self):
        self.assertEqual(split_sentences(""), [])
        self.assertEqual(split_sentences("   "), [])


if __name__ == "__main__":
    unittest.main()
