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


def max_window_score(num_samples: int, window: int, score_fn) -> float:
    """Score non-overlapping windows with `score_fn(start, end)` and return
    the max. A whole utterance scores positive if ANY window holds speech,
    so trailing silence can neither hide speech nor let trailing noise
    dominate. Pure function — the unit under test for windowing policy."""
    if num_samples <= 0 or window <= 0:
        return 0.0
    best = 0.0
    start = 0
    while start < num_samples:
        best = max(best, float(score_fn(start, min(start + window, num_samples))))
        start += window
    return best


class SileroVadBackend(VadBackend):
    name = "silero"

    def __init__(self, source: str = "snakers4/silero-vad") -> None:
        self._source = source
        self._model = None
        self._state = None

    def _ensure(self):
        import os
        import torch

        # VOICE_VAD_SOURCE pins the model origin: "local:/path/to/silero"
        # for a vendored checkout (recommended for reproducible deploys),
        # otherwise the maintained upstream repo. torch.hub caches under
        # ~/.cache/torch (override with TORCH_HOME); nothing is fetched at
        # import time — only on first inference, and failures surface as
        # explicit errors, never silent fallbacks. Constructor default keeps
        # direct instantiation working; Settings is the source of truth.
        source = os.environ.get("VOICE_VAD_SOURCE", self._source)
        if self._model is None:
            if source.startswith("local:"):
                self._model, utils = torch.hub.load(
                    repo_or_dir=source[len("local:"):], model="silero_vad",
                    source="local", force_reload=False, trust_repo=False,
                )
            else:
                self._model, utils = torch.hub.load(
                    repo_or_dir=source, model="silero_vad", force_reload=False, trust_repo=True
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
        # Score the whole utterance as non-overlapping 512-sample windows
        # and take the max: scoring only the tail would miss speech followed
        # by trailing silence (false negatives) and overreact to trailing
        # noise (false positives).
        with torch.no_grad():
            return max_window_score(
                audio.numel(), 512, lambda s, e: model(audio[s:e], 16000).item()
            )


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
