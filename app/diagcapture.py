"""Temporary diagnostic capture of the exact Whisper input (P2Y follow-up).

When enabled, `run_turn` persists the precise PCM16 bytes handed to
`stt.transcribe` as a playable WAV plus a JSON sidecar (config, signal
stats, VAD decision, transcript association, timings). Everything here is
stdlib-only so it unit-tests without models.

Safety rules (do not relax without review):
- Opt-in only: `diag_dir=None` (the default) writes nothing and allocates
  nothing beyond the constants below.
- Local diagnostics only: never point this at production; the sidecar holds
  the transcript text (required to associate audio with results) and must
  stay on the operator's machine.
- No audio bytes, transcripts, or paths ever go through the event logger.
"""
from __future__ import annotations

import json
import math
import os
import struct
import time
import uuid
import wave

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2


def audio_stats(pcm16_mono_16k: bytes) -> dict:
    """Signal facts for one utterance: sizes, level, clipping, emptiness."""
    n = len(pcm16_mono_16k) // 2
    if n <= 0:
        return {
            "bytes": len(pcm16_mono_16k), "samples": 0, "secs": 0.0,
            "rms": 0, "peak": 0, "clipped_fraction": 0.0, "empty": True,
        }
    samples = struct.unpack("<%dh" % n, pcm16_mono_16k)
    peak = 0
    clipped = 0
    energy = 0
    for s in samples:
        a = abs(s)
        if a > peak:
            peak = a
        if a >= 32767:
            clipped += 1
        energy += s * s
    return {
        "bytes": len(pcm16_mono_16k),
        "samples": n,
        "secs": round(n / SAMPLE_RATE, 2),
        "rms": int(math.sqrt(energy / n)),
        "peak": peak,
        "clipped_fraction": round(clipped / n, 6),
        "empty": not pcm16_mono_16k.strip(b"\x00"),
    }


def write_diag_capture(
    directory: str,
    *,
    audio: bytes,
    turn_id: str,
    model_config: dict,
    vad_prob: float,
    vad_threshold: float,
    transcript: str,
    avg_logprob: float | None,
    no_speech: bool,
    stt_ms: int,
    extra: dict | None = None,
) -> str:
    """Write `<diag_id>.wav` + `<diag_id>.json`; return the diag id.

    Best-effort: any filesystem failure raises (the caller decides whether
    a diagnostic failure may fail the turn — it must not).
    """
    os.makedirs(directory, exist_ok=True)
    diag_id = f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"
    wav_path = os.path.join(directory, f"{diag_id}.wav")
    with wave.open(wav_path, "wb") as fh:
        fh.setnchannels(CHANNELS)
        fh.setsampwidth(SAMPLE_WIDTH_BYTES)
        fh.setframerate(SAMPLE_RATE)
        fh.writeframes(audio)
    sidecar = {
        "diag_id": diag_id,
        "turn_id": turn_id,
        "created_unix_ms": int(time.time() * 1000),
        "audio": {**audio_stats(audio), "sample_rate": SAMPLE_RATE, "channels": CHANNELS},
        "model": model_config,
        "vad": {"speech_probability": round(float(vad_prob), 4), "threshold": vad_threshold},
        "result": {
            "chars": len(transcript),
            "avg_logprob": avg_logprob,
            "no_speech": bool(no_speech),
            "stt_ms": stt_ms,
            "transcript": transcript,
        },
        "extra": extra or {},
    }
    with open(os.path.join(directory, f"{diag_id}.json"), "w") as fh:
        json.dump(sidecar, fh, indent=2)
    return diag_id
