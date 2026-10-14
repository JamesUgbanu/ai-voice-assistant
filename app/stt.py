"""Speech-to-text adapter interface.

Production backend is faster-whisper (CTranslate2, MIT weights) selected in
Phase 2; whisper.cpp remains an evaluated alternative. Both are imported
lazily so unit tests and the app module graph never require model
dependencies. Deterministic stub backend exists for tests only.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field


@dataclass
class Transcript:
    text: str
    language: str = "en"
    # Approximate confidence when the backend provides one, else None.
    confidence: float | None = None
    # True when the backend believes the audio held no speech.
    no_speech: bool = False
    # Mean segment log-probability (Whisper). Higher (closer to 0) = more
    # confident; used to gate the deterministic greeting fast path.
    avg_logprob: float | None = None


@dataclass
class SttConfig:
    model: str = "small.en"
    device: str = "cpu"
    compute_type: str = "int8"
    num_workers: int = 1
    # Decoding: lower beam = faster, slightly less accurate. 1 is greedy.
    beam_size: int = 5
    # CTranslate2 threads. 0 lets the library pick.
    cpu_threads: int = 0
    # Domain biasing: Whisper conditions on this text, which markedly improves
    # proper nouns and technical vocabulary (PostgreSQL, Chidera, Alimosho…).
    initial_prompt: str = ""
    hotwords: str = ""
    # 0.0 = deterministic greedy fallback (best for short utterances); the
    # library default (1.0) can hallucinate on near-silence.
    temperature: float = 0.0
    # Independent short utterances should not condition on each other.
    condition_on_previous_text: bool = False


class SttBackend:
    name = "base"

    def transcribe(self, pcm16_mono_16k: bytes, config: SttConfig) -> Transcript:
        raise NotImplementedError


_WORD_RE = re.compile(r"[a-z0-9']+")

#: Hallucinated decoder output is rejected below these floors. Genuine
#: utterances — including repeated words ("Hello. Hello.", -0.25/0.04),
#: prompted proper nouns ("…Chidera and Alimosho.", -0.32/0.00) and quiet but
#: complete speech ("Hello.", -0.12/0.05) — score far better, while real
#: browser captures of degraded fragments ("Alo." -0.93/0.08, "Blo."
#: -0.89/0.16, "Welcome home." -0.87/0.25, "So." -1.73/0.59,
#: "Alimosho, Alimosho," -0.77/0.30) score far worse. (stat = avg_logprob /
#: mean no_speech_prob; base.en, beam 1, measured on file and browser audio.)
_NO_SPEECH_PROB_CEILING = 0.5
_GUARD_NSP_FLOOR = 0.06
_GUARD_LOGPROB_FLOOR = -0.75
_REPEAT_LOGPROB_FLOOR = -0.7
_REPEAT_NSP_FLOOR = 0.15


def is_hallucinated_transcript(
    text: str,
    avg_logprob: float | None,
    no_speech_probs: list,
) -> bool:
    """True when decoder output looks hallucinated rather than spoken.

    The decoder can latch onto a token and repeat it ("No. No. No…") or emit
    a confident-looking word for an ambiguous fragment ("HAPPY." for 0.1 s of
    audio). Such outputs carry poor segment log-probs and/or elevated
    no-speech probability. Pure function — unit-tested without a model.
    """
    mean_nsp = (
        sum(float(p) for p in no_speech_probs) / len(no_speech_probs)
        if no_speech_probs
        else 0.0
    )
    if mean_nsp > _NO_SPEECH_PROB_CEILING:
        return True
    if (
        avg_logprob is not None
        and avg_logprob < _GUARD_LOGPROB_FLOOR
        and mean_nsp > _GUARD_NSP_FLOOR
    ):
        return True
    words = _WORD_RE.findall((text or "").lower())
    if (
        len(words) >= 2
        and len(set(words)) == 1
        and avg_logprob is not None
        and avg_logprob < _REPEAT_LOGPROB_FLOOR
        and mean_nsp > _REPEAT_NSP_FLOOR
    ):
        return True
    return False


class FasterWhisperBackend(SttBackend):
    """faster-whisper, loaded once per process. Import inside methods so a
    missing/failing install raises a clear error at first use, not import."""

    name = "faster-whisper"

    def __init__(self) -> None:
        self._models: dict = {}
        self._lock = threading.Lock()
        # Serializes inference across callers (voice turns and composer
        # dictation): one CPU core and one CTranslate2 model, so overlapping
        # transcriptions must queue rather than compete or corrupt state.
        self._infer_lock = threading.Lock()

    def _model(self, config: SttConfig):
        from faster_whisper import WhisperModel

        key = (config.model, config.device, config.compute_type, config.cpu_threads)
        model = self._models.get(key)
        if model is None:
            # Serialize construction: startup warm-up and a first turn must
            # share ONE loaded model, never load twice under contention.
            with self._lock:
                model = self._models.get(key)
                if model is None:
                    kwargs = {}
                    if config.cpu_threads and config.cpu_threads > 0:
                        kwargs["cpu_threads"] = config.cpu_threads
                    model = WhisperModel(
                        config.model, device=config.device, compute_type=config.compute_type, **kwargs
                    )
                    self._models[key] = model
        return model

    def transcribe(self, pcm16_mono_16k: bytes, config: SttConfig) -> Transcript:
        import numpy as np  # noqa: F401  (required by faster-whisper)

        model = self._model(config)
        samples = np.frombuffer(pcm16_mono_16k, dtype=np.int16).astype("float32") / 32768.0
        kwargs = dict(
            language="en",
            beam_size=config.beam_size,
            vad_filter=False,
            temperature=config.temperature,
            condition_on_previous_text=config.condition_on_previous_text,
        )
        if config.initial_prompt:
            kwargs["initial_prompt"] = config.initial_prompt
        if config.hotwords:
            kwargs["hotwords"] = config.hotwords
        # One model, one CPU core: serialize every caller (voice turns and
        # composer dictation) so transcriptions queue instead of competing.
        with self._infer_lock:
            try:
                segments, info = model.transcribe(samples, **kwargs)
            except TypeError:
                # Older faster-whisper lacks `hotwords`; retry without it.
                kwargs.pop("hotwords", None)
                segments, info = model.transcribe(samples, **kwargs)
            seg_list = list(segments)
            text = "".join(s.text for s in seg_list).strip()
            prob = getattr(info, "language_probability", None)
            logprobs = [s.avg_logprob for s in seg_list if getattr(s, "avg_logprob", None) is not None]
            avg_logprob = float(sum(logprobs) / len(logprobs)) if logprobs else None
            nsps = [s.no_speech_prob for s in seg_list if getattr(s, "no_speech_prob", None) is not None]
        if not text:
            return Transcript(text="", no_speech=True, avg_logprob=avg_logprob)
        if is_hallucinated_transcript(text, avg_logprob, nsps):
            # Never send decoder hallucinations ("Alimosho, Alimosho",
            # "No. No. No…") to RAG or the UI; the caller treats no_speech as
            # an empty turn and the client returns to listening.
            return Transcript(text="", no_speech=True, avg_logprob=avg_logprob)
        return Transcript(
            text=text,
            confidence=float(prob) if prob is not None else None,
            avg_logprob=avg_logprob,
        )


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
