"""Regression tests: whole-utterance VAD windowing and model source pinning."""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.vad import SileroVadBackend, max_window_score, window_pad


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


class WindowPadTest(unittest.TestCase):
    """Regression: Silero (TorchScript) raises 'Input audio chunk is too
    short' on a final window < 512 samples, which failed every turn whose
    length wasn't a multiple of 512. The adapter now pads the tail."""

    def test_pads_to_next_full_window(self):
        self.assertEqual(window_pad(0), 0)
        self.assertEqual(window_pad(512), 0)
        self.assertEqual(window_pad(1024), 0)
        self.assertEqual(window_pad(1), 511)
        self.assertEqual(window_pad(367), 145)
        self.assertEqual(window_pad(513), 511)

    def test_padded_length_is_always_a_whole_number_of_windows(self):
        for n in (1, 367, 512, 513, 36207, 40000):
            self.assertEqual((n + window_pad(n)) % 512, 0)

    def test_non_positive_inputs_are_safe(self):
        self.assertEqual(window_pad(-5), 0)
        self.assertEqual(window_pad(100, window=0), 0)


if __name__ == "__main__":
    unittest.main()
