"""Regression tests: whole-utterance VAD windowing and model source pinning."""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.vad import SileroVadBackend, max_window_score


class MaxWindowScoreTest(unittest.TestCase):
    def test_empty_or_degenerate_input_scores_zero(self):
        self.assertEqual(max_window_score(0, 512, lambda s, e: 1.0), 0.0)
        self.assertEqual(max_window_score(100, 0, lambda s, e: 1.0), 0.0)

    def test_takes_max_over_windows(self):
        calls = []

        def score(start, end):
            calls.append((start, end))
            return 0.9 if start == 0 else 0.1

        self.assertEqual(max_window_score(1600, 512, score), 0.9)
        self.assertEqual([c[0] for c in calls], [0, 512, 1024, 1536])

    def test_trailing_silence_cannot_hide_speech(self):
        # Speech in the first window, silence after: whole-utterance max
        # stays high. (The old tail-only behavior scored ~0 here and
        # dropped real utterances.)
        scores = {(0, 512): 0.95, (512, 1024): 0.02, (1024, 1536): 0.01}
        self.assertEqual(max_window_score(1536, 512, lambda s, e: scores[(s, e)]), 0.95)

    def test_trailing_noise_cannot_dominate(self):
        scores = {(0, 512): 0.05, (512, 1024): 0.04, (1024, 1536): 0.9}
        self.assertEqual(max_window_score(1536, 512, lambda s, e: scores[(s, e)]), 0.9)


class SileroSourceTest(unittest.TestCase):
    def test_default_upstream_and_explicit_override(self):
        self.assertEqual(SileroVadBackend()._source, "snakers4/silero-vad")
        self.assertEqual(SileroVadBackend(source="local:/srv/voice/vendor/silero")._source,
                         "local:/srv/voice/vendor/silero")


if __name__ == "__main__":
    unittest.main()
