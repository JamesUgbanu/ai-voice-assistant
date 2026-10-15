"""Temporary diagnostic capture: exact-Whisper-input WAV+JSON, no models."""
import os
import struct
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.diagcapture import audio_stats, write_diag_capture

import json


def tone(seconds=0.5, freq=440.0, amp=8000, rate=16000):
    import math

    n = int(seconds * rate)
    return struct.pack(
        "<%dh" % n,
        *[int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)],
    )


class AudioStatsTest(unittest.TestCase):
    def test_tone_stats(self):
        st = audio_stats(tone())
        self.assertEqual(st["samples"], 8000)
        self.assertEqual(st["secs"], 0.5)
        self.assertGreater(st["rms"], 4000)
        self.assertLessEqual(st["peak"], 8000)
        self.assertEqual(st["clipped_fraction"], 0.0)
        self.assertFalse(st["empty"])

    def test_silence_and_empty(self):
        st = audio_stats(b"\x00" * 3200)
        self.assertTrue(st["empty"])
        self.assertEqual(st["rms"], 0)
        self.assertEqual(audio_stats(b"")["samples"], 0)

    def test_clipping_flagged(self):
        st = audio_stats(struct.pack("<4h", 32767, -32768, 100, 0))
        self.assertEqual(st["peak"], 32768)
        self.assertEqual(st["clipped_fraction"], 0.5)


class WriteCaptureTest(unittest.TestCase):
    def test_wav_plus_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            diag_id = write_diag_capture(
                tmp,
                audio=tone(),
                turn_id="t7",
                model_config={"model": "base.en", "beam_size": 1},
                vad_prob=0.99,
                vad_threshold=0.5,
                transcript="Hello.",
                avg_logprob=-0.2,
                no_speech=False,
                stt_ms=1234,
            )
            wav_path = os.path.join(tmp, diag_id + ".wav")
            with wave.open(wav_path, "rb") as fh:
                self.assertEqual(fh.getnchannels(), 1)
                self.assertEqual(fh.getsampwidth(), 2)
                self.assertEqual(fh.getframerate(), 16000)
                self.assertEqual(fh.getnframes(), 8000)
            with open(os.path.join(tmp, diag_id + ".json")) as fh:
                sidecar = json.load(fh)
            self.assertEqual(sidecar["turn_id"], "t7")
            self.assertEqual(sidecar["result"]["transcript"], "Hello.")
            self.assertEqual(sidecar["audio"]["sample_rate"], 16000)
            self.assertEqual(sidecar["model"]["model"], "base.en")

    def test_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "nested", "caps")
            write_diag_capture(
                target, audio=tone(0.1), turn_id="t1", model_config={},
                vad_prob=1.0, vad_threshold=0.5, transcript="x",
                avg_logprob=None, no_speech=False, stt_ms=0,
            )
            self.assertTrue(os.path.isdir(target))


if __name__ == "__main__":
    unittest.main()
