"""Voice activity detection interface.

Production: Silero VAD (MIT, tiny, CPU-trivial). A fixed energy gate is
deliberately NOT the strategy — it fails on quiet speech, laptop fans, and
music. Lazy import; deterministic stub for tests.
"""
from __future__ import annotations


class VadBackend:
    name = "base"

    def speech_probability(self, pcm16_mono_16k: bytes) -> float:
        """Probability [0,1] that the frame contains speech."""
        raise NotImplementedError


class SileroVadBackend(VadBackend):
    name = "silero"

    def __init__(self) -> None:
        self._model = None
        self._state = None

    def _ensure(self):
        import torch

        if self._model is None:
            self._model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad", model="silero_vad", force_reload=False, trust_repo=True
            )
            self._reset = utils[0]
            self._state = None
        return self._model

    def speech_probability(self, pcm16_mono_16k: bytes) -> float:
        import torch

        model = self._ensure()
        import numpy as np

        samples = np.frombuffer(pcm16_mono_16k, dtype=np.int16).astype("float32") / 32768.0
        audio = torch.from_numpy(samples)
        # Silero VAD expects 512-sample windows at 16kHz; use the tail.
        window = audio[-512:] if audio.numel() >= 512 else audio
        with torch.no_grad():
            prob = model(window, 16000).item()
        return float(prob)


class StubVadBackend(VadBackend):
    """Energy heuristic stand-in. Tests only — never production VAD."""

    name = "stub"

    def __init__(self, threshold: float = 0.02) -> None:
        self.threshold = threshold

    def speech_probability(self, pcm16_mono_16k: bytes) -> float:
        import struct

        if not pcm16_mono_16k:
            return 0.0
        n = len(pcm16_mono_16k) // 2
        samples = struct.unpack(f"<{n}h", pcm16_mono_16k)
        rms = (sum(s * s for s in samples) / max(1, n)) ** 0.5 / 32768.0
        return 1.0 if rms >= self.threshold else 0.0
