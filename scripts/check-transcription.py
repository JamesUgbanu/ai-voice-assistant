#!/usr/bin/env python3
"""Run one WAV file through the production STT stack and print the result.

Defaults mirror the live voice path exactly (model, beam size, prompt,
temperature, previous-text conditioning from Settings). Use it to compare a
diagnostic capture against what the app produced, and --model to run a
separate comparison experiment against another locally installed checkpoint
without touching production configuration.

Needs model packages (faster-whisper, torch, numpy); run where they are
installed, e.g. inside the voice container (models are cached there):

    docker cp BECAUSE.wav voice-service:/tmp/x.wav
    docker exec voice-service python3 scripts/check-transcription.py /tmp/x.wav
    docker exec voice-service python3 scripts/check-transcription.py /tmp/x.wav --model small.en

Prints: transcript, no_speech flag, avg logprob, per-segment details,
audio stats, config, and wall-clock transcription time.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import wave

here = os.path.dirname(os.path.abspath(__file__))
for candidate in (os.path.join(here, ".."), "/srv/voice", os.getcwd()):
    if os.path.isdir(os.path.join(candidate, "app")):
        sys.path.insert(0, candidate)
        break

from app.config import Settings
from app.stt import FasterWhisperBackend, SttConfig


def load_wav_16k_mono(path: str) -> bytes:
    with wave.open(path, "rb") as fh:
        if fh.getnchannels() != 1 or fh.getsampwidth() != 2 or fh.getframerate() != 16000:
            raise SystemExit(
                f"need 16-bit mono 16 kHz WAV, got {fh.getnchannels()}ch/"
                f"{fh.getsampwidth() * 8}bit/{fh.getframerate()}Hz"
            )
        return fh.readframes(fh.getnframes())


def describe(raw: bytes) -> dict:
    n = len(raw) // 2
    if not n:
        return {"secs": 0.0, "rms": 0, "peak": 0}
    import math

    samples = struct.unpack("<%dh" % n, raw)
    peak = max(abs(s) for s in samples)
    rms = int(math.sqrt(sum(s * s for s in samples) / n))
    return {"secs": round(n / 16000, 2), "rms": rms, "peak": peak}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wav", help="16-bit mono 16 kHz WAV (e.g. a diagnostic capture)")
    ap.add_argument("--model", default=None, help="comparison checkpoint (default: production model)")
    args = ap.parse_args()

    settings = Settings()
    model = args.model or settings.stt_model
    cfg = SttConfig(
        model=model,
        beam_size=settings.stt_beam_size,
        cpu_threads=settings.stt_cpu_threads,
        initial_prompt=settings.stt_initial_prompt,
        hotwords=settings.stt_hotwords,
        temperature=settings.stt_temperature,
        condition_on_previous_text=settings.stt_condition_previous,
    )
    raw = load_wav_16k_mono(args.wav)
    info = describe(raw)
    backend = FasterWhisperBackend()
    t0 = time.time()
    transcript = backend.transcribe(raw, cfg)
    dt = time.time() - t0
    print(f"file: {args.wav}")
    print(f"audio: {info['secs']}s 16kHz mono, rms={info['rms']} peak={info['peak']}")
    print(f"model: {model} beam={cfg.beam_size} temp={cfg.temperature} "
          f"cond_prev={cfg.condition_on_previous_text}")
    print(f"prompt: {settings.stt_initial_prompt[:72]}...")
    print(f"text: {transcript.text!r}")
    print(f"no_speech={transcript.no_speech} avg_logprob={transcript.avg_logprob} "
          f"seconds={dt:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
