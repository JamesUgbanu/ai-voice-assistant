"""Speech-to-text adapter interface.

Production backend is faster-whisper (CTranslate2, MIT weights) selected in
Phase 2; whisper.cpp remains an evaluated alternative. Both are imported
lazily so unit tests and the app module graph never require model
dependencies. Deterministic stub backend exists for tests only.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Transcript:
    text: str
    language: str = "en"
    # Approximate confidence when the backend provides one, else None.
    confidence: float | None = None
    # True when the backend believes the audio held no speech.
    no_speech: bool = False


@dataclass
class SttConfig:
    model: str = "small.en"
    device: str = "cpu"
    compute_type: str = "int8"
    num_workers: int = 1


class SttBackend:
    name = "base"

    def transcribe(self, pcm16_mono_16k: bytes, config: SttConfig) -> Transcript:
        raise NotImplementedError


class FasterWhisperBackend(SttBackend):
    """faster-whisper, loaded once per process. Import inside methods so a
    missing/failing install raises a clear error at first use, not import."""

    name = "faster-whisper"

    def __init__(self) -> None:
        self._models: dict = {}

    def _model(self, config: SttConfig):
        from faster_whisper import WhisperModel

        key = (config.model, config.device, config.compute_type)
        model = self._models.get(key)
        if model is None:
            model = WhisperModel(config.model, device=config.device, compute_type=config.compute_type)
            self._models[key] = model
        return model

    def transcribe(self, pcm16_mono_16k: bytes, config: SttConfig) -> Transcript:
        import numpy as np  # noqa: F401  (required by faster-whisper)

        model = self._model(config)
        samples = np.frombuffer(pcm16_mono_16k, dtype=np.int16).astype("float32") / 32768.0
        segments, info = model.transcribe(samples, language="en", beam_size=5, vad_filter=False)
        text = "".join(s.text for s in segments).strip()
        prob = getattr(info, "language_probability", None)
        if not text:
            return Transcript(text="", no_speech=True)
        return Transcript(text=text, confidence=float(prob) if prob is not None else None)


class StubSttBackend(SttBackend):
    """Deterministic stand-in for unit/integration tests. Never production."""

    name = "stub"

    def __init__(self, text: str = "hello james") -> None:
        self.text = text
        self.calls: list = []

    def transcribe(self, pcm16_mono_16k: bytes, config: SttConfig) -> Transcript:
        self.calls.append((len(pcm16_mono_16k), config.model))
        if not pcm16_mono_16k.strip(b"\x00"):
            return Transcript(text="", no_speech=True)
        return Transcript(text=self.text, confidence=1.0)


def describe_backend(name: str) -> str:
    return {"faster-whisper": "faster-whisper (CTranslate2)", "stub": "deterministic test stub"}.get(
        name, f"unknown backend: {name}"
    )
