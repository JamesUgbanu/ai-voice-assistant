"""Hallucination-guard contract: decoder garbage must never reach RAG/UI.

Thresholds are anchored in measured base.en stats (avg_logprob / mean
no_speech_prob) on file AND real browser captures: genuine utterances score
(-0.12..-0.49 / 0.00..0.05) — including repeated words ("Hello. Hello."
-0.25/0.04), prompted proper nouns ("…Chidera and Alimosho." -0.32/0.00) and
quiet complete speech (-0.12/0.05) — while degraded browser fragments score
("Alo." -0.93/0.08, "Blo." -0.89/0.16, "Welcome home." -0.87/0.25, "So."
-1.73/0.59, "Alimosho, Alimosho," -0.77/0.30) far worse. No model needed.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.stt import is_hallucinated_transcript


class HallucinationGuardTest(unittest.TestCase):
    def test_genuine_utterances_pass(self):
        self.assertFalse(is_hallucinated_transcript("Hello.", -0.18, [0.029]))
        self.assertFalse(is_hallucinated_transcript("What has James built with AWS?", -0.39, [0.002]))
        self.assertFalse(is_hallucinated_transcript("Tell me about Chidera and Alimosho.", -0.32, [0.002]))
        # Quiet but complete speech stays well clear of the guard.
        self.assertFalse(is_hallucinated_transcript("Hello.", -0.12, [0.047]))
        self.assertFalse(is_hallucinated_transcript("Hello.", -0.49, [0.03]))

    def test_genuine_repetition_passes(self):
        # "Hello. Hello." (-0.25/0.04): the user really said it twice.
        self.assertFalse(is_hallucinated_transcript("Hello. Hello.", -0.25, [0.038, 0.038]))

    def test_low_confidence_speech_like_garbage_rejected(self):
        self.assertTrue(is_hallucinated_transcript("HAPPY.", -1.31, [0.289]))
        self.assertTrue(is_hallucinated_transcript("So.", -1.73, [0.594]))

    def test_degraded_browser_fragments_rejected(self):
        # Real browser captures of spoken "Hello" that decoded wrong. Each has
        # the model's own elevated no-speech estimate plus poor confidence;
        # genuine speech never scores like this.
        self.assertTrue(is_hallucinated_transcript("Alo.", -0.93, [0.075]))
        self.assertTrue(is_hallucinated_transcript("Blo.", -0.89, [0.164]))
        self.assertTrue(is_hallucinated_transcript("Welcome home.", -0.87, [0.249]))

    def test_high_no_speech_rejected(self):
        self.assertTrue(is_hallucinated_transcript("anything", -0.2, [0.75]))

    def test_repetition_loop_rejected(self):
        # The reported class ("Alimosho, Alimosho", "No. No. No. No. No."):
        # one word on a loop with poor confidence. The browser capture below
        # carries the decoder's runaway timestamps ([0.00-28.24s] for 0.5 s of
        # audio) and is rejected by both the confidence and repetition rules.
        self.assertTrue(is_hallucinated_transcript("No. No. No. No. No.", -1.01, [0.4] * 5))
        self.assertTrue(is_hallucinated_transcript("Alimosho, Alimosho", -1.2, [0.45, 0.45]))
        self.assertTrue(is_hallucinated_transcript("Alimosho, Alimosho,", -0.77, [0.304]))

    def test_missing_stats_never_rejects(self):
        # Stub/test backends provide no stats: pass through, never block.
        self.assertFalse(is_hallucinated_transcript("Hello.", None, []))
        self.assertFalse(is_hallucinated_transcript("Hello. Hello.", None, []))

    def test_single_word_uses_confidence_rules_not_repetition(self):
        # One word is never a repetition loop; the confidence rules decide.
        self.assertFalse(is_hallucinated_transcript("Hello.", -0.5, [0.1]))
        self.assertTrue(is_hallucinated_transcript("Hello.", -2.0, [0.4]))


if __name__ == "__main__":
    unittest.main()
