#!/usr/bin/env python3
"""Fixed voice-clone listening test: synthesize one passage with the ACTIVE
voice and compare it against your reference recording.

Runs ON THE VOICE HOST (needs `pocket-tts` + torch; numpy optional for the
pitch stats). Never part of the stdlib test suite. It uses the SAME reference
recording configured for the service — it does not substitute a generic sample —
and it states plainly which voice mode was active.

Usage:
  # On a host with pocket-tts + torch installed:
  python3 scripts/clone-smoke.py
  python3 scripts/clone-smoke.py --mode clone --ref /path/to/voice-ref.wav
  python3 scripts/clone-smoke.py --mode preset              # non-cloned baseline

  # Or inside the running container (scripts/ is NOT baked into the image; the
  # reference mount is read-only, so write the output to /tmp and copy it out):
  docker cp scripts/clone-smoke.py voice-service:/tmp/clone-smoke.py
  docker exec -w /srv/voice voice-service python3 /tmp/clone-smoke.py \
      --out /tmp/clone-smoke.wav
  docker cp voice-service:/tmp/clone-smoke.wav ./clone-smoke.wav
  open ./clone-smoke.wav

Outputs a WAV under voice-reference/output/ (Git-ignored) and prints the exact
`open` command. F0 (pitch-range) stats are a cheap objective correlate of the
"flat vs expressive" impression — not a speaker-similarity score. Judge the
clone by listening; the numbers only explain what you hear.
"""
from __future__ import annotations

import argparse
import os
import sys
import wave

sys.path.insert(0, ".")

PASSAGE = (
    "Hello, my name is James Ugbanu, and it's a pleasure to meet you. "
    "I grew up in Alimosho, Lagos, and I still call Nigeria home. "
    "Would you like to hear about the platforms I have been building? "
    "I work every day with PostgreSQL, pgvector, TypeScript, Terraform, and AWS. "
    "Let me walk you through how I think about software architecture, from the "
    "first sketch all the way to production."
)


def write_wav_pcm16(path: str, pcm: bytes, rate: int) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(pcm)


def read_wav_pcm16(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as fh:
        if fh.getsampwidth() != 2 or fh.getnchannels() != 1:
            raise SystemExit("expected mono 16-bit PCM WAV")
        return fh.readframes(fh.getnframes()), fh.getframerate()


def f0_percentiles(pcm: bytes, rate: int) -> dict | None:
    """Rough autocorrelation pitch stats — objective only, not a similarity
    score. Returns None when numpy is unavailable."""
    try:
        import numpy as np
    except Exception:
        return None
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    win = int(rate * 0.04)
    hop = int(rate * 0.02)
    lo, hi = int(rate / 300), int(rate / 60)  # 60–300 Hz search
    values = []
    for start in range(0, max(0, len(x) - win), hop):
        seg = x[start:start + win]
        if float(np.sqrt(np.mean(seg * seg))) < 0.01:
            continue
        seg = seg - float(np.mean(seg))
        corr = np.correlate(seg, seg, mode="full")[win - 1:]
        if corr[0] <= 0:
            continue
        peak = lo + int(np.argmax(corr[lo:hi]))
        values.append(rate / peak)
    if len(values) < 10:
        return None
    a = np.array(values)
    return {
        "median": float(np.median(a)),
        "p10": float(np.percentile(a, 10)),
        "p90": float(np.percentile(a, 90)),
        "range": float(np.percentile(a, 90) - np.percentile(a, 10)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=os.environ.get("VOICE_TTS_MODE", "preset"))
    ap.add_argument("--ref", default=os.environ.get("VOICE_TTS_VOICE_REF", ""))
    ap.add_argument("--preset-voice", default=os.environ.get("VOICE_TTS_PRESET_VOICE", "alba"))
    ap.add_argument("--text", default=PASSAGE)
    ap.add_argument("--out", default="voice-reference/output/clone-smoke.wav")
    args = ap.parse_args()

    from app.tts import PocketTtsBackend, TtsConfig

    cfg = TtsConfig(mode=args.mode, voice_ref=args.ref, preset_voice=args.preset_voice)
    backend = PocketTtsBackend()
    status = backend.preflight(cfg)  # raises VoiceCloningUnavailable if unusable
    print(f"active voice: {status}")

    pcm = backend.synthesize_full(args.text, cfg)
    rate = backend.sample_rate
    write_wav_pcm16(args.out, pcm, rate)
    secs = len(pcm) / (rate * 2)
    print(f"wrote {args.out} ({secs:.1f}s @ {rate} Hz)")

    out_f0 = f0_percentiles(pcm, rate)
    ref_f0 = None
    if cfg.mode == "clone" and cfg.voice_ref and os.path.isfile(cfg.voice_ref):
        ref_pcm, ref_rate = read_wav_pcm16(cfg.voice_ref)
        ref_f0 = f0_percentiles(ref_pcm, ref_rate)
    if out_f0 and ref_f0:
        print("pitch (F0, Hz) — objective correlate, NOT a similarity score:")
        print(f"  reference: median {ref_f0['median']:.1f}  p90-p10 range {ref_f0['range']:.1f}")
        print(f"  output:    median {out_f0['median']:.1f}  p90-p10 range {out_f0['range']:.1f}")
        if ref_f0["range"] > 0:
            print(f"  output/ref pitch-range ratio: {out_f0['range']/ref_f0['range']*100:.0f}%")
    elif out_f0:
        print(f"output pitch: median {out_f0['median']:.1f} Hz, "
              f"p90-p10 range {out_f0['range']:.1f} Hz")
    else:
        print("pitch stats skipped (numpy unavailable)")

    print("\nLISTEN (this is the only real test):")
    print(f"  open {args.out}")
    if cfg.mode == "clone" and cfg.voice_ref:
        print(f"  open {cfg.voice_ref}   # your reference")
    print("If the output does not sound like you, check `.tts.cloning_active` on /healthz:")
    print("  preset mode or a missing reference reads as cloning_active=false.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
