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

from .model_manifest import ModelIntegrityError

logger = logging.getLogger("voice")


class VoiceCloningUnavailable(RuntimeError):
    """Clone mode was requested but cannot run. Message is user-actionable and
    contains no secrets or private paths."""


class TtsBusy(RuntimeError):
    """A prior stream has not released the model after cancellation."""


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


def _decoded_values(audio):
    """Flatten model audio to finite floats, or None for legacy fake arrays."""
    import math

    raw = audio.detach().cpu() if hasattr(audio, "detach") else audio
    if hasattr(raw, "numpy"):
        raw = raw.numpy()
    if not hasattr(raw, "tolist") and not hasattr(raw, "__iter__"):
        return None
    values = raw.tolist() if hasattr(raw, "tolist") else list(raw)

    def _flat(items):
        for item in items:
            if isinstance(item, (list, tuple)):
                yield from _flat(item)
            else:
                yield item

    return [
        float(v)
        for v in _flat(values)
        if isinstance(v, (int, float)) and math.isfinite(float(v))
    ]


def _pcm16(audio) -> bytes:
    """Convert model float audio to PCM16 safely.

    The previous direct ``audio * 32767`` cast wrapped samples outside [-1, 1]
    and was a source of audible crackle. Remove DC, apply bounded peak gain to
    a safe target, and clip before conversion. The streaming path uses
    ``Pcm16StreamEncoder`` so gain changes are continuous across chunks.
    """
    from array import array

    values = _decoded_values(audio)
    if values is None:
        # Compatibility for the unit-suite fake array (no numeric export).
        raw = audio.detach().cpu() if hasattr(audio, "detach") else audio
        if hasattr(raw, "numpy"):
            raw = raw.numpy()
        return (raw * 32767.0).astype("<i2").tobytes()
    if not values:
        return b""
    mean = sum(values) / len(values)
    values = [v - mean for v in values]
    peak = max(abs(v) for v in values)
    gain = min(0.95 / peak, 10.0) if peak > 0 else 1.0
    out = array("h")
    for v in values:
        sample = max(-1.0, min(1.0, v * gain))
        out.append(int(round(sample * 32767.0)))
    return out.tobytes()


class Pcm16StreamEncoder:
    """Stateful float-audio → PCM16 encoder for chunked synthesis.

    Two chunk-boundary defects caused audible clicks/pumping:
    subtracting each chunk's own mean reset the DC level to zero at every
    boundary, and switching gain at the first sample of a chunk created a step.
    This encoder keeps both filters continuous across chunks:

    * one stateful DC blocker (no per-chunk mean removal), and
    * smooth gain attack/release, with an immediate emergency attack only
      when a sample would otherwise hit the limiter.

    Samples are clamped before conversion, so model overshoot cannot wrap.
    """

    def __init__(
        self,
        target_peak: float = 0.95,
        max_gain: float = 10.0,
        sample_rate: int = 24000,
        attack_ms: float = 3.0,
        release_ms: float = 60.0,
        dc_r: float = 0.995,
    ) -> None:
        import math
        from array import array

        self._array = array
        self._target_peak = max(0.05, min(1.0, target_peak))
        self._max_gain = max(1.0, max_gain)
        self._dc_r = max(0.0, min(0.99999, dc_r))
        self._x_prev: float | None = None
        self._y_prev = 0.0
        self._gain: float | None = None
        attack_s = max(0.0005, attack_ms / 1000.0)
        release_s = max(0.0005, release_ms / 1000.0)
        self._attack = 1.0 - math.exp(-1.0 / (attack_s * sample_rate))
        self._release = 1.0 - math.exp(-1.0 / (release_s * sample_rate))

    def _dc_block(self, values: list[float]) -> list[float]:
        if self._x_prev is None:
            # Start from the first sample so a non-zero first chunk cannot
            # produce an initial DC thump.
            self._x_prev = values[0]
        out: list[float] = []
        for value in values:
            filtered = value - self._x_prev + self._dc_r * self._y_prev
            self._x_prev = value
            self._y_prev = filtered
            out.append(filtered)
        return out

    def encode(self, audio) -> bytes:
        values = _decoded_values(audio)
        if values is None:
            raw = audio.detach().cpu() if hasattr(audio, "detach") else audio
            if hasattr(raw, "numpy"):
                raw = raw.numpy()
            return (raw * 32767.0).astype("<i2").tobytes()
        if not values:
            return b""

        filtered = self._dc_block(values)
        peak = max(abs(v) for v in filtered)
        if peak <= 0:
            return bytes(len(filtered) * 2)
        desired = min(self._target_peak / peak, self._max_gain)
        if self._gain is None:
            self._gain = desired

        out = self._array("h")
        gain = self._gain
        attack, release = self._attack, self._release
        limit = self._target_peak
        for v in filtered:
            if abs(v) * gain > limit:
                # A smoothed attack would stay above the limiter and
                # flat-top the waveform. Drop straight to the chunk target.
                gain = desired
            elif desired < gain:
                gain += attack * (desired - gain)
            else:
                gain += release * (desired - gain)
            sample = max(-limit, min(limit, v * gain))
            out.append(int(round(sample * 32767.0)))
        self._gain = gain
        return out.tobytes()


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

    def __init__(self, verify_models: bool = True, manifest_path: str | None = None) -> None:
        self._model = None
        self._voice_state = None
        self._voice_key: tuple | None = None
        self._has_cloning: bool | None = None
        self._cloning_active = False
        self._mode = "preset"
        self._logged = False
        self._lock = threading.RLock()
        # Verification is on by default for the real backend. Adapter tests
        # inject a fake `pocket_tts` and disable it; the verifier itself has
        # separate deterministic tests with synthetic assets.
        self._verify_models = verify_models
        self._manifest_path = manifest_path
        self._integrity_error: str | None = None
        # Set by the pipeline when a producer ignores `stop` past the bounded
        # shutdown wait. This is a fail-closed flag for every entry point
        # (streaming, sentence fallback, and /v1/tts), not only the pipeline.
        self._producer_stalled = threading.Event()

    def _verify_model_assets(self, has_cloning: bool, model_config=None) -> dict:
        """Verify the checkpoint actually selected by Pocket TTS.

        The loader may fall back to the public non-cloning checkpoint for
        preset mode. Select the manifest entry from that resolved state, then
        require the config's exact HF revision and the local file digest to
        agree with it before the model is assigned for use.
        """
        from .model_manifest import verify_runtime_manifest

        if model_config is None:
            from pocket_tts.models.tts_model import CONFIGS_DIR, load_config

            model_config = load_config(CONFIGS_DIR / "english.yaml")
        if has_cloning:
            model_source = model_config.weights_path
        else:
            model_source = model_config.weights_path_without_voice_cloning
        if not model_source:
            raise ModelIntegrityError("model config has no checkpoint source")
        tokenizer_source = model_config.flow_lm.lookup_table.tokenizer_path
        if not tokenizer_source:
            raise ModelIntegrityError("model config has no tokenizer source")
        return verify_runtime_manifest(
            manifest_path=self._manifest_path,
            cloning_available=has_cloning,
            model_source=model_source,
            tokenizer_source=tokenizer_source,
        )

    def report_producer_stalled(self) -> None:
        """Fail closed after a producer ignores the stop timeout."""
        self._producer_stalled.set()

    def report_producer_recovered(self) -> None:
        """The stalled producer exited; new synthesis may be admitted."""
        self._producer_stalled.clear()

    def _ensure_locked(self, config: TtsConfig):
        from pocket_tts import TTSModel

        if self._integrity_error is not None:
            raise ModelIntegrityError(self._integrity_error)
        if self._model is None:
            model = TTSModel.load_model()
            has_cloning = bool(getattr(model, "has_voice_cloning", False))
            if self._verify_models:
                try:
                    self._verify_model_assets(has_cloning)
                except ModelIntegrityError as exc:
                    # Cache the failure: do not reload/download 200+ MB on every
                    # subsequent request when the pinned asset is wrong.
                    self._integrity_error = str(exc)
                    raise
            self._model = model
            self._has_cloning = has_cloning
        if config.mode == "clone":
            if not config.voice_ref:
                raise VoiceCloningUnavailable(
                    "clone mode requires VOICE_TTS_VOICE_REF to point at the prepared reference"
                )
            if not os.path.isfile(config.voice_ref):
                raise VoiceCloningUnavailable(
                    "VOICE_TTS_VOICE_REF does not point at a readable file"
                )
            try:
                stat = os.stat(config.voice_ref)
            except OSError as exc:
                raise VoiceCloningUnavailable(
                    "VOICE_TTS_VOICE_REF does not point at a readable file"
                ) from exc
            # Re-derive the voice state when the reference is replaced at the
            # same path. The model caches conditioning by path, so a path-only
            # key would keep speaking the old recording until a restart.
            key = (
                "clone",
                config.voice_ref,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_size,
                stat.st_ino,
            )
        else:
            key = ("preset", config.preset_voice)
        if self._voice_state is None or self._voice_key != key:
            if config.mode == "clone":
                if not self._has_cloning:
                    raise VoiceCloningUnavailable(
                        "voice cloning needs the gated 'kyutai/pocket-tts' weights: accept the "
                        "model terms on Hugging Face and authenticate (HF_TOKEN). The loaded "
                        "model has no cloning support, so a custom voice cannot be used."
                    )
                # Pocket TTS caps conditioning at 30 s. Our prepared reference
                # already is, but a raw/baked reference may be longer; truncate
                # explicitly so the model never processes unbounded audio.
                self._voice_state = self._model.get_state_for_audio_prompt(
                    config.voice_ref, truncate=True
                )
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

    def warm(self, config: TtsConfig) -> None:
        """Preflight and run one short synthesis.

        The first Pocket TTS decode is much slower and emits much larger
        chunks than subsequent decodes (observed 0.42x real-time cold vs
        1.1-1.2x warm). Without this, the first live segment can underrun.
        Startup-only; output is discarded.
        """
        self.preflight(config)
        for _ in self.synthesize_stream("Ready.", config):
            pass

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "mode": self._mode,
            "cloning_available": bool(self._has_cloning),
            "cloning_active": self._cloning_active,
        }

    def synthesize_full(self, text: str, config: TtsConfig) -> bytes:
        if self._producer_stalled.is_set():
            raise TtsBusy("tts producer is still shutting down")
        # Serialize ensure + inference: the model is single-instance and not
        # thread-safe across overlapping turns/sessions.
        with self._lock:
            model, voice_state = self._ensure_locked(config)
            audio = model.generate_audio(voice_state, text)
            return _pcm16(audio)

    def synthesize_stream(self, text: str, config: TtsConfig, stop=None):
        """Stream decoded chunks as Pocket TTS produces them. First audio
        arrives in a fraction of a second instead of after the whole segment
        is synthesized (the dominant first-audio latency at segment sizes of
        100–400 chars). `stop` aborts mid-generation for barge-in."""
        if self._producer_stalled.is_set():
            raise TtsBusy("tts producer is still shutting down")
        with self._lock:
            model, voice_state = self._ensure_locked(config)
            encoder = Pcm16StreamEncoder(sample_rate=self.sample_rate)
            for chunk in model.generate_audio_stream(voice_state, text, stop=stop):
                pcm = encoder.encode(chunk)
                if pcm:
                    yield pcm

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
