"""Composer-dictation audio validation and rate limiting (stdlib only)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.transcribe import (
    DICTATION_SUBJECT_PREFIX,
    TranscribeError,
    WindowLimiter,
    validate_transcribe_audio,
)


class ValidateAudioTest(unittest.TestCase):
    def _ok(self, n=3200):
        return bytes([1, 0]) * (n // 2)

    def test_accepts_valid_pcm16(self):
        audio = self._ok()
        self.assertEqual(validate_transcribe_audio(audio, max_bytes=100_000), audio)

    def test_rejects_too_large(self):
        with self.assertRaises(TranscribeError) as c:
            validate_transcribe_audio(bytes([1, 0]) * 10, max_bytes=8)
        self.assertEqual(c.exception.code, "too_large")

    def test_rejects_empty_and_short(self):
        for body in (b"", bytes([1, 0]) * 10):
            with self.assertRaises(TranscribeError) as c:
                validate_transcribe_audio(body, max_bytes=100_000)
            self.assertEqual(c.exception.code, "empty")

    def test_rejects_odd_byte_count(self):
        with self.assertRaises(TranscribeError) as c:
            validate_transcribe_audio(bytes([1]) * 3201, max_bytes=100_000)
        self.assertEqual(c.exception.code, "bad_audio")

    def test_rejects_pure_silence(self):
        with self.assertRaises(TranscribeError) as c:
            validate_transcribe_audio(b"\x00" * 3200, max_bytes=100_000)
        self.assertEqual(c.exception.code, "empty")


class WindowLimiterTest(unittest.TestCase):
    def test_allows_up_to_limit_then_blocks(self):
        lim = WindowLimiter()
        self.assertTrue(lim.allow("k", 2, 60_000, 1000)[0])
        self.assertTrue(lim.allow("k", 2, 60_000, 2000)[0])
        ok, retry = lim.allow("k", 2, 60_000, 3000)
        self.assertFalse(ok)
        self.assertGreater(retry, 0)

    def test_window_slides(self):
        lim = WindowLimiter()
        self.assertTrue(lim.allow("k", 1, 1000, 0)[0])
        self.assertFalse(lim.allow("k", 1, 1000, 500)[0])
        self.assertTrue(lim.allow("k", 1, 1000, 1500)[0])

    def test_keys_are_independent(self):
        lim = WindowLimiter()
        self.assertTrue(lim.allow("a", 1, 1000, 0)[0])
        self.assertTrue(lim.allow("b", 1, 1000, 0)[0])


class PrefixTest(unittest.TestCase):
    def test_prefix_distinguishes_dictation_tickets(self):
        self.assertEqual(DICTATION_SUBJECT_PREFIX, "dict:")


if __name__ == "__main__":
    unittest.main()
