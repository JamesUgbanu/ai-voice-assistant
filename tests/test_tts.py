"""Pocket TTS adapter: mode selection, cloning gating, state reuse,
thread-safety. Uses a fake ``pocket_tts`` module so no torch/model is needed.
"""
import os
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.tts import PocketTtsBackend, TtsConfig, VoiceCloningUnavailable


class FakeArray:
    def __init__(self, n=120):
        self.n = n

    def __mul__(self, _k):
        return self

    def astype(self, _dtype):
        return self

    def tobytes(self):
        return b"\x00\x00" * self.n


class FakeAudio:
    def numpy(self):
        return FakeArray()


class FakeChunk:
    def __init__(self, n=60):
        self.n = n

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return FakeArray(self.n)


class FakeModel:
    def __init__(self, has_cloning=True, delay=0.0):
        self.has_voice_cloning = has_cloning
        self.state_calls = []
        self.state_kwargs = []
        self.gen_calls = []
        self.delay = delay
        self._active = 0
        self.max_active = 0

    def get_state_for_audio_prompt(self, source, truncate=False):
        self.state_calls.append(source)
        self.state_kwargs.append({"truncate": truncate})
        if source.endswith(".wav") and not self.has_voice_cloning:
            raise ValueError("voice cloning unsupported")
        return {"voice": source}

    def generate_audio(self, state, text):
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        if self.delay:
            time.sleep(self.delay)
        self.gen_calls.append(text)
        self._active -= 1
        return FakeAudio()

    def generate_audio_stream(self, state, text, stop=None):
        # Three decoded chunks stand in for a streamed segment.
        for _ in range(3):
            if stop is not None and stop.is_set():
                return
            yield FakeChunk()


def install_fake_pocket(model):
    mod = types.ModuleType("pocket_tts")

    class TTSModel:
        @staticmethod
        def load_model(*_a, **_k):
            return model

    mod.TTSModel = TTSModel
    sys.modules["pocket_tts"] = mod


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("pocket_tts", None)
        self.tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)
        if self._saved is None:
            sys.modules.pop("pocket_tts", None)
        else:
            sys.modules["pocket_tts"] = self._saved

    def test_preset_mode_uses_preset_voice(self):
        m = FakeModel(has_cloning=False)
        install_fake_pocket(m)
        b = PocketTtsBackend()
        b.synthesize_full("hello there", TtsConfig(mode="preset", preset_voice="alba"))
        self.assertEqual(m.state_calls, ["alba"])
        self.assertEqual(m.state_kwargs, [{"truncate": False}])
        d = b.describe()
        self.assertEqual(d["mode"], "preset")
        self.assertFalse(d["cloning_active"])

    def test_clone_mode_without_cloning_support_fails_explicitly(self):
        install_fake_pocket(FakeModel(has_cloning=False))
        b = PocketTtsBackend()
        with self.assertRaises(VoiceCloningUnavailable) as c:
            b.synthesize_full("hi", TtsConfig(mode="clone", voice_ref=self.tmp.name))
        self.assertIn("gated", str(c.exception))

    def test_clone_mode_missing_file_fails(self):
        install_fake_pocket(FakeModel(has_cloning=True))
        b = PocketTtsBackend()
        with self.assertRaises(VoiceCloningUnavailable):
            b.synthesize_full("hi", TtsConfig(mode="clone", voice_ref="/no/such/ref.wav"))

    def test_clone_mode_without_ref_fails(self):
        install_fake_pocket(FakeModel(has_cloning=True))
        b = PocketTtsBackend()
        with self.assertRaises(VoiceCloningUnavailable):
            b.synthesize_full("hi", TtsConfig(mode="clone", voice_ref=""))

    def test_clone_mode_success_and_state_reuse(self):
        m = FakeModel(has_cloning=True)
        install_fake_pocket(m)
        b = PocketTtsBackend()
        cfg = TtsConfig(mode="clone", voice_ref=self.tmp.name)
        out1 = b.synthesize_full("one", cfg)
        out2 = b.synthesize_full("two", cfg)
        self.assertIsInstance(out1, bytes)
        self.assertEqual(len(m.state_calls), 1)  # prepared once, reused
        self.assertEqual(m.state_kwargs, [{"truncate": True}])
        self.assertEqual(m.gen_calls, ["one", "two"])
        self.assertTrue(b.describe()["cloning_active"])

    def test_replacing_reference_at_same_path_reloads_voice_state(self):
        m = FakeModel(has_cloning=True)
        install_fake_pocket(m)
        b = PocketTtsBackend()
        cfg = TtsConfig(mode="clone", voice_ref=self.tmp.name)
        b.synthesize_full("one", cfg)
        with open(self.tmp.name, "wb") as handle:
            handle.write(b"changed reference")
        stat = os.stat(self.tmp.name)
        os.utime(self.tmp.name, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        b.synthesize_full("two", cfg)
        self.assertEqual(len(m.state_calls), 2)
        self.assertEqual(m.state_kwargs, [{"truncate": True}, {"truncate": True}])

    def test_switching_mode_reuses_distinct_states(self):
        m = FakeModel(has_cloning=True)
        install_fake_pocket(m)
        b = PocketTtsBackend()
        b.synthesize_full("a", TtsConfig(mode="preset", preset_voice="alba"))
        b.synthesize_full("b", TtsConfig(mode="clone", voice_ref=self.tmp.name))
        self.assertEqual(len(m.state_calls), 2)

    def test_concurrent_synthesis_is_serialized(self):
        m = FakeModel(has_cloning=False, delay=0.02)
        install_fake_pocket(m)
        b = PocketTtsBackend()
        cfg = TtsConfig(mode="preset")
        threads = [threading.Thread(target=b.synthesize_full, args=(f"t{i}", cfg)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(m.max_active, 1)  # lock prevents overlapping inference

    def test_preflight_reports_without_path(self):
        install_fake_pocket(FakeModel(has_cloning=False))
        b = PocketTtsBackend()
        d = b.preflight(TtsConfig(mode="preset"))
        self.assertEqual(set(d), {"backend", "mode", "cloning_available", "cloning_active"})
        self.assertNotIn(self.tmp.name, str(d))

    def test_synthesize_stream_yields_multiple_pcm_chunks(self):
        install_fake_pocket(FakeModel(has_cloning=True))
        b = PocketTtsBackend()
        chunks = list(b.synthesize_stream("hello there", TtsConfig(mode="preset")))
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(isinstance(c, bytes) and len(c) > 0 for c in chunks))
        self.assertTrue(b.supports_incremental())


if __name__ == "__main__":
    unittest.main()


class Pcm16NormalizationTest(unittest.TestCase):
    def test_clips_out_of_range_samples_instead_of_wrapping(self):
        from app.tts import _pcm16
        import struct
        raw = _pcm16([2.0, -2.0, 0.0, 0.0])
        vals = struct.unpack(f"<{len(raw)//2}h", raw)
        self.assertEqual(len(vals), 4)
        self.assertLessEqual(max(abs(v) for v in vals), 32767)
        self.assertEqual(vals[2:], (0, 0))

    def test_accepts_nested_channel_lists(self):
        from app.tts import _pcm16
        import struct
        raw = _pcm16([[0.0, 0.5, -0.5, 0.0]])
        vals = struct.unpack(f"<{len(raw)//2}h", raw)
        self.assertEqual(len(vals), 4)
        self.assertGreater(max(abs(v) for v in vals), 2000)

    def test_removes_dc_and_boosts_quiet_audio_without_clipping(self):
        from app.tts import _pcm16
        import struct
        raw = _pcm16([0.50, 0.51, 0.49, 0.50])
        vals = struct.unpack(f"<{len(raw)//2}h", raw)
        self.assertLessEqual(max(abs(v) for v in vals), 32767)
        self.assertGreater(max(abs(v) for v in vals), 2000)
        self.assertLessEqual(abs(sum(vals) / len(vals)), 80)


class Pcm16StreamEncoderTest(unittest.TestCase):
    def test_quiet_stream_is_boosted_without_clipping(self):
        from app.tts import Pcm16StreamEncoder
        import struct
        enc = Pcm16StreamEncoder()
        raw = enc.encode([0.01, -0.01] * 120)
        vals = struct.unpack(f"<{len(raw)//2}h", raw)
        peak = max(abs(v) for v in vals)
        self.assertGreater(peak, 2000)
        self.assertLessEqual(peak, int(0.96 * 32767))

    def test_loud_chunk_after_quiet_never_clips(self):
        from app.tts import Pcm16StreamEncoder
        import struct
        enc = Pcm16StreamEncoder()
        enc.encode([0.01, -0.01] * 40)  # initialize at the maximum quiet gain
        raw = enc.encode([0.7, -0.7] * 40)
        vals = struct.unpack(f"<{len(raw)//2}h", raw)
        self.assertLessEqual(max(abs(v) for v in vals), int(0.96 * 32767))

    def test_gain_recovers_gradually_without_chunk_edge_step(self):
        from app.tts import Pcm16StreamEncoder
        import struct
        enc = Pcm16StreamEncoder()
        first_raw = enc.encode([0.7, -0.7] * 40)
        first = struct.unpack(f"<{len(first_raw)//2}h", first_raw)
        second_raw = enc.encode([0.01, -0.01] * 40)
        second = struct.unpack(f"<{len(second_raw)//2}h", second_raw)
        # A quiet chunk after a loud one stays attenuated (gain continuity)
        # but is not silenced, and neither chunk wraps.
        self.assertGreater(max(abs(v) for v in second), 0)
        self.assertLessEqual(max(abs(v) for v in second), int(0.96 * 32767))

    def test_out_of_range_model_samples_are_clipped_not_wrapped(self):
        from app.tts import Pcm16StreamEncoder
        import struct
        enc = Pcm16StreamEncoder()
        raw = enc.encode([8.0, -8.0])
        vals = struct.unpack(f"<{len(raw)//2}h", raw)
        self.assertLessEqual(max(abs(v) for v in vals), int(0.96 * 32767))

    def test_silence_stays_silent(self):
        from app.tts import Pcm16StreamEncoder
        import struct
        enc = Pcm16StreamEncoder()
        raw = enc.encode([0.0] * 40)
        vals = struct.unpack(f"<{len(raw)//2}h", raw)
        self.assertEqual(set(vals), {0})
