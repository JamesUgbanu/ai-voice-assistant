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
        self.gen_calls = []
        self.delay = delay
        self._active = 0
        self.max_active = 0

    def get_state_for_audio_prompt(self, source):
        self.state_calls.append(source)
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
        self.assertEqual(m.gen_calls, ["one", "two"])
        self.assertTrue(b.describe()["cloning_active"])

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
