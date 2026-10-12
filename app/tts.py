"""Text-to-speech adapter interface.

Production candidate is Kyutai Pocket TTS (MIT, 100M params, CPU
real-time, streaming, voice cloning from a ~5s wav) — subject to Phase 2
measurement on the actual target CPU. Lazy imports keep the app importable
without torch. A deterministic stub serves tests.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TtsConfig:
    language: str = "english"
    # Pre-exported voice embedding (safetensors) or wav path. None until the
    # authorized reference recording is supplied and evaluated (Phase 2+).
    voice_ref: str = ""
    sample_rate: int = 24000


class TtsBackend:
    name = "base"

    @property
    def sample_rate(self) -> int:
        return 24000

    def synthesize_full(self, text: str, config: TtsConfig) -> bytes:
        """Return complete PCM16 mono audio for text. Raises on failure."""
        raise NotImplementedError

    def supports_incremental(self) -> bool:
        """True only if verified to emit playable prefixes mid-generation."""
        return False


class PocketTtsBackend(TtsBackend):
    """Kyutai Pocket TTS. Model + voice state load once per process."""

    name = "pocket-tts"

    def __init__(self) -> None:
        self._model = None
        self._voice_state = None
        self._voice_key = ""

    def _ensure(self, config: TtsConfig):
        from pocket_tts import TTSModel

        if self._model is None:
            self._model = TTSModel.load_model()
        key = config.voice_ref or "<default>"
        if self._voice_state is None or self._voice_key != key:
            if config.voice_ref:
                self._voice_state = self._model.get_state_for_audio_prompt(config.voice_ref)
            else:
                self._voice_state = self._model.get_state_for_audio_prompt("alba")
            self._voice_key = key
        return self._model, self._voice_state

    def synthesize_full(self, text: str, config: TtsConfig) -> bytes:
        model, voice_state = self._ensure(config)
        audio = model.generate_audio(voice_state, text)
        pcm = (audio.numpy() * 32767.0).astype("<i2")
        return pcm.tobytes()

    @property
    def sample_rate(self) -> int:
        return 24000


class StubTtsBackend(TtsBackend):
    """Deterministic stand-in: encodes text length as tone frames. Tests only."""

    name = "stub"

    def __init__(self, sample_rate: int = 24000) -> None:
        self._rate = sample_rate
        self.calls: list = []

    @property
    def sample_rate(self) -> int:
        return self._rate

    def synthesize_full(self, text: str, config: TtsConfig) -> bytes:
        self.calls.append(text)
        import struct

        n = max(1, min(len(text), 200)) * 240  # deterministic dummy frames
        return struct.pack(f"<{n}h", *([1000] * n))
