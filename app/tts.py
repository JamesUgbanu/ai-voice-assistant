"""Text-to-speech adapter interface.

Production candidate is Kyutai Pocket TTS (MIT, 100M params, CPU real-time,
streaming). Voice cloning needs the **gated** ``kyutai/pocket-tts`` weights;
without them the model loads a non-cloning fallback and custom references are
rejected with an actionable error — never a silent generic-voice substitution.
Lazy imports keep the app importable without torch. A deterministic stub serves
tests.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

logger = logging.getLogger("voice")


class VoiceCloningUnavailable(RuntimeError):
    """Clone mode was requested but cannot run. Message is user-actionable and
    contains no secrets or private paths."""


@dataclass
class TtsConfig:
    language: str = "english"
    # "preset" (built-in voice) or "clone" (authorized reference recording).
    mode: str = "preset"
    # Path to the authorized reference WAV/audio. Required in clone mode.
    voice_ref: str = ""
    # Predefined voice used in preset mode.
    preset_voice: str = "alba"
    sample_rate: int = 24000


class TtsBackend:
    name = "base"

    @property
    def sample_rate(self) -> int:
        return 24000

    def synthesize_full(self, text: str, config: TtsConfig) -> bytes:
        """Return complete PCM16 mono audio for text. Raises on failure."""
        raise NotImplementedError

    def synthesize_stream(self, text: str, config: TtsConfig, stop=None):
        """Yield PCM16 mono audio chunks as they are generated.

        Backends that can stream override this; the default yields the whole
        clip once, so callers always get a consistent iterator. `stop` is a
        threading.Event a backend may honour to abort mid-generation."""
        yield self.synthesize_full(text, config)

    def supports_incremental(self) -> bool:
        """True only if verified to emit playable prefixes mid-generation."""
        return False


class PocketTtsBackend(TtsBackend):
    """Kyutai Pocket TTS. Model + voice state load once per process.

    Pocket TTS is not thread-safe, so a single lock serializes model load and
    every synthesis. The prepared voice state is reused across utterances and
    only re-derived when the requested voice key changes.
    """

    name = "pocket-tts"

    def __init__(self) -> None:
        self._model = None
        self._voice_state = None
        self._voice_key = ""
        self._has_cloning: bool | None = None
        self._cloning_active = False
        self._mode = "preset"
        self._logged = False
        self._lock = threading.RLock()

    def _ensure_locked(self, config: TtsConfig):
        from pocket_tts import TTSModel

        if self._model is None:
            self._model = TTSModel.load_model()
            self._has_cloning = bool(getattr(self._model, "has_voice_cloning", False))
        key = f"{config.mode}:{config.voice_ref if config.mode == 'clone' else config.preset_voice}"
        if self._voice_state is None or self._voice_key != key:
            if config.mode == "clone":
                if not config.voice_ref:
                    raise VoiceCloningUnavailable(
                        "clone mode requires VOICE_TTS_VOICE_REF to point at the prepared reference"
                    )
                if not os.path.isfile(config.voice_ref):
                    raise VoiceCloningUnavailable(
                        "VOICE_TTS_VOICE_REF does not point at a readable file"
                    )
                if not self._has_cloning:
                    raise VoiceCloningUnavailable(
                        "voice cloning needs the gated 'kyutai/pocket-tts' weights: accept the "
                        "model terms on Hugging Face and authenticate (HF_TOKEN). The loaded "
                        "model has no cloning support, so a custom voice cannot be used."
                    )
                self._voice_state = self._model.get_state_for_audio_prompt(config.voice_ref)
                self._cloning_active = True
            else:
                self._voice_state = self._model.get_state_for_audio_prompt(config.preset_voice)
                self._cloning_active = False
            self._voice_key = key
        self._mode = config.mode
        if not self._logged:
            # Safe, path-free observability of the active voice mode.
            logger.info(
                "tts_voice_mode mode=%s cloning_available=%s cloning_active=%s ref_configured=%s",
                self._mode, self._has_cloning, self._cloning_active, bool(config.voice_ref),
            )
            self._logged = True
        return self._model, self._voice_state

    def preflight(self, config: TtsConfig) -> dict:
        """Resolve and validate the configured voice without synthesizing.
        Raises VoiceCloningUnavailable (clone) or the underlying model error.
        Returns a safe summary (no paths)."""
        with self._lock:
            self._ensure_locked(config)
            return self.describe()

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "mode": self._mode,
            "cloning_available": bool(self._has_cloning),
            "cloning_active": self._cloning_active,
        }

    def synthesize_full(self, text: str, config: TtsConfig) -> bytes:
        # Serialize ensure + inference: the model is single-instance and not
        # thread-safe across overlapping turns/sessions.
        with self._lock:
            model, voice_state = self._ensure_locked(config)
            audio = model.generate_audio(voice_state, text)
            pcm = (audio.numpy() * 32767.0).astype("<i2")
            return pcm.tobytes()

    def synthesize_stream(self, text: str, config: TtsConfig, stop=None):
        """Stream decoded chunks as Pocket TTS produces them. First audio
        arrives in a fraction of a second instead of after the whole segment
        is synthesized (the dominant first-audio latency at segment sizes of
        100–400 chars). `stop` aborts mid-generation for barge-in."""
        with self._lock:
            model, voice_state = self._ensure_locked(config)
            for chunk in model.generate_audio_stream(voice_state, text, stop=stop):
                arr = chunk.detach().cpu().numpy()
                yield (arr * 32767.0).astype("<i2").tobytes()

    def supports_incremental(self) -> bool:
        return True

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
