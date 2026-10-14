"""Voice-reference validation and normalization.

Pure helpers run everywhere; the ffmpeg-backed paths skip when ffmpeg is
absent. No real private recording is used — audio is generated in a temp dir.
"""
import array
import math
import os
import shutil
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.voice_reference import (
    NORMALIZED_SAMPLE_RATE,
    AudioInfo,
    VoiceReferenceError,
    assess_levels,
    clamp_reference_seconds,
    normalize,
    probe,
    validate_info,
)

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_wav(path, seconds=2.0, rate=16000, freq=180.0, amp=0.3):
    n = int(seconds * rate)
    frames = array.array("h", (int(32767 * amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())
    return path


class PurePolicyTest(unittest.TestCase):
    def test_assess_levels_flags_clipping_and_quiet(self):
        self.assertIn("clipped", assess_levels(-6.0, -0.1))
        self.assertIn("excessively quiet", assess_levels(-52.0, -20.0))
        self.assertEqual(assess_levels(-20.0, -6.0), [])

    def test_clamp_never_exceeds_documented_limit(self):
        self.assertEqual(clamp_reference_seconds(9.0, 30.0), 9.0)
        self.assertEqual(clamp_reference_seconds(99.0, 30.0), 30.0)
        self.assertEqual(clamp_reference_seconds(0.0), 0.0)
        self.assertEqual(clamp_reference_seconds(5.0, 30.0), 5.0)

    def test_validate_raises_missing_empty_too_short(self):
        with self.assertRaises(VoiceReferenceError) as c:
            validate_info(AudioInfo(exists=False))
        self.assertEqual(c.exception.code, "missing")
        with self.assertRaises(VoiceReferenceError) as c:
            validate_info(AudioInfo(exists=True, duration_s=0.0))
        self.assertEqual(c.exception.code, "empty")
        with self.assertRaises(VoiceReferenceError) as c:
            validate_info(AudioInfo(exists=True, duration_s=0.3))
        self.assertEqual(c.exception.code, "too_short")

    def test_validate_ok_populates_issues(self):
        info = validate_info(AudioInfo(exists=True, duration_s=5.0, mean_db=-12.0, peak_db=-3.0))
        self.assertEqual(info.issues, [])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not installed")
class FfmpegTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="voiceref-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_probe_missing_file(self):
        info = probe(os.path.join(self.dir, "nope.wav"))
        self.assertFalse(info.exists)

    def test_probe_valid_wav_reports_format(self):
        p = make_wav(os.path.join(self.dir, "a.wav"), seconds=2.0, rate=16000)
        info = probe(p)
        self.assertTrue(info.exists)
        self.assertAlmostEqual(info.duration_s, 2.0, places=1)
        self.assertEqual(info.sample_rate, 16000)
        self.assertEqual(info.channels, 1)
        self.assertEqual(info.codec, "pcm_s16le")
        self.assertIsNotNone(info.peak_db)

    def test_probe_corrupt_file_raises(self):
        bad = os.path.join(self.dir, "bad.wav")
        with open(bad, "w") as fh:
            fh.write("this is not audio")
        with self.assertRaises(VoiceReferenceError) as c:
            probe(bad)
        self.assertEqual(c.exception.code, "decode_failed")

    def test_normalize_mono_24k_and_caps_duration(self):
        src = make_wav(os.path.join(self.dir, "long.wav"), seconds=40.0, rate=16000)
        dst = os.path.join(self.dir, "out", "ref.wav")
        out = normalize(src, dst, sample_rate=NORMALIZED_SAMPLE_RATE, max_seconds=30.0)
        self.assertTrue(os.path.isfile(dst))
        self.assertEqual(out.channels, 1)
        self.assertEqual(out.sample_rate, NORMALIZED_SAMPLE_RATE)
        self.assertLessEqual(out.duration_s, 30.5)
        # Original untouched.
        self.assertAlmostEqual(probe(src).duration_s, 40.0, places=1)

    def test_normalize_short_file_kept_whole(self):
        src = make_wav(os.path.join(self.dir, "short.wav"), seconds=2.0, rate=16000)
        dst = os.path.join(self.dir, "ref.wav")
        out = normalize(src, dst, max_seconds=30.0)
        self.assertAlmostEqual(out.duration_s, 2.0, places=1)


if __name__ == "__main__":
    unittest.main()
