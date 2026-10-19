#!/usr/bin/env python3
"""Measure incremental (rolling-window) STT against a real recording.

Runs ON THE VOICE HOST (needs faster-whisper; numpy optional). Never part of
the stdlib test suite. It replays a WAV as if it arrived as 100 ms frames in
real time and schedules partials with the PRODUCTION debounce/increment rules
and single-flight semantics (``app.partial_stt.should_start_partial``): a decode
already in flight skips new starts instead of queueing, which is exactly the
process-wide gate the server uses. That makes the contention measurements
meaningful, not projected.

Usage:
  # On the host, in a Python env that has faster-whisper:
  python3 scripts/bench-streaming-stt.py /path/to/utterance.wav [--model base.en]
  python3 scripts/bench-streaming-stt.py /path/to/x.wav --speed 4   # 4x replay

  # Or inside the running container (scripts/ is NOT baked into the image):
  docker cp scripts/bench-streaming-stt.py voice-service:/tmp/bench-stt.py
  docker cp /path/to/x.wav voice-service:/tmp/x.wav
  docker exec -w /srv/voice voice-service python3 /tmp/bench-stt.py /tmp/x.wav --model base.en

Reports (all vs the audio-arrival clock, so it answers "how soon does text
appear while the user is still speaking?"):
  * time to first partial and whether it completed before end-of-audio
  * every partial's decode duration and covered window
  * starts skipped while a decode was in flight (backpressure)
  * end-of-audio -> final transcript (full decode vs exact reuse)
  * peak RSS and CPU time / utilization for the whole run
  * streaming final == the single whole-file decode (no window drift)

Expensive and CPU/GPU-bound: do not run alongside real traffic.
"""
from __future__ import annotations

import argparse
import resource
import struct
import sys
import threading
import time
import wave

sys.path.insert(0, ".")

from app.partial_stt import should_start_partial  # noqa: E402


def read_wav_pcm16_mono(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as fh:
        if fh.getsampwidth() != 2:
            raise SystemExit("expected 16-bit PCM WAV")
        channels = fh.getnchannels()
        rate = fh.getframerate()
        frames = fh.readframes(fh.getnframes())
    if channels > 1:  # downmix to mono by averaging
        count = len(frames) // 2 // channels
        out = bytearray()
        for i in range(count):
            total = 0
            for c in range(channels):
                (v,) = struct.unpack_from("<h", frames, (i * channels + c) * 2)
                total += v
            out += struct.pack("<h", total // channels)
        frames = bytes(out)
    return frames, rate


def peak_rss_mb() -> float:
    """Peak resident set size for this process, in MB (portable macOS/Linux)."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports kilobytes.
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--beam", type=int, default=1)
    ap.add_argument("--interval-ms", type=int, default=900)
    ap.add_argument("--min-audio-ms", type=int, default=700)
    ap.add_argument("--max-audio-seconds", type=int, default=15)
    ap.add_argument("--speed", type=float, default=1.0,
                    help="replay speed vs real time (1.0 = as spoken; >1 = faster)")
    args = ap.parse_args()

    from app.stt import FasterWhisperBackend, SttConfig

    pcm, rate = read_wav_pcm16_mono(args.wav)
    if rate != 16000:
        raise SystemExit(f"expected 16 kHz mono, got {rate} Hz")
    cfg = SttConfig(model=args.model, beam_size=args.beam, temperature=0.0,
                    condition_on_previous_text=False)
    stt = FasterWhisperBackend()
    stt.transcribe(bytes(16000 * 2), cfg)  # warm the model first

    frame_bytes = int(rate * 100 / 1000) * 2  # 100 ms of PCM16
    frame_secs = frame_bytes / (rate * 2)
    min_bytes = int(args.min_audio_ms * rate / 1000) * 2
    max_bytes = int(args.max_audio_seconds * rate) * 2

    buf = bytearray()
    lock = threading.Lock()
    in_flight = threading.Event()
    partials: list[dict] = []
    skipped_busy = 0
    last_start = -10**12
    last_considered = 0

    cpu0 = time.process_time()
    t_start = time.monotonic()

    def worker(snapshot: bytes, covered: int, start_offset: float) -> None:
        try:
            t0 = time.monotonic()
            tr = stt.transcribe(snapshot, cfg)
            dt = time.monotonic() - t0
            emit_offset = time.monotonic() - t_start
            text = (tr.text or "").strip()
            with lock:
                partials.append({
                    "start": start_offset,
                    "emit": emit_offset,
                    "decode": dt,
                    "covered_bytes": covered,
                    "text": text,
                })
        finally:
            in_flight.clear()

    offsets = range(0, len(pcm), frame_bytes)
    for i, offset in enumerate(offsets):
        buf.extend(pcm[offset:offset + frame_bytes])
        # Pace arrival to (scaled) real time so the audio clock is honest even
        # when a decode is slow — exactly how frames arrive over the wire.
        deadline = t_start + ((i + 1) * frame_secs) / max(args.speed, 0.01)
        sleep = deadline - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        now = time.monotonic()
        now_ms = int((now - t_start) * 1000)
        busy = in_flight.is_set()
        can_start = should_start_partial(
            enabled=True, stopped=False, in_flight=False, gate_locked=False,
            audio_bytes=len(buf), last_considered_bytes=last_considered,
            min_bytes=min_bytes, max_bytes=max_bytes, now_ms=now_ms,
            last_start_ms=last_start, interval_ms=args.interval_ms,
        )
        if busy and can_start:
            skipped_busy += 1  # a start was due but the single decoder was busy
        if can_start and not busy:
            last_start = now_ms
            last_considered = len(buf)
            in_flight.set()
            threading.Thread(
                target=worker, args=(bytes(buf), len(buf), now - t_start), daemon=True
            ).start()

    # Let any in-flight decode finish before finalizing (production settles too).
    deadline = time.monotonic() + 30.0
    while in_flight.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    t_end = time.monotonic() - t_start
    cpu = time.process_time() - cpu0
    audio_secs = len(pcm) / (rate * 2)

    print("\n=== partials ===")
    with lock:
        for p in partials:
            print(f"  emit @{p['emit']:5.2f}s wall ({p['covered_bytes']/(rate*2):4.1f}s window, "
                  f"decode {p['decode']*1000:6.0f} ms): {p['text']!r}")

    print("\n=== summary ===")
    print(f"audio: {audio_secs:.2f}s; replayed in {t_end:.2f}s wall (speed {args.speed}x)")
    if partials:
        first = partials[0]
        before_end = "YES" if first["emit"] < audio_secs else "NO"
        print(f"first partial completed after {first['emit']:.2f}s wall "
              f"({first['emit']*1000:.0f} ms); before end-of-audio: {before_end}")
        print(f"partials: {len(partials)}; starts skipped (decoder busy): {skipped_busy}")
    else:
        print(f"partials: 0 (first start needs >= {args.min_audio_ms} ms of audio); "
              f"starts skipped: {skipped_busy}")
    print(f"peak RSS: {peak_rss_mb():.0f} MB; CPU time: {cpu:.2f}s; "
          f"CPU utilization: {cpu/max(t_end,1e-9)*100:.0f}%")

    # Final: exact reuse is possible only when a partial covered the whole buffer.
    covered = max((p["covered_bytes"] for p in partials), default=0)
    print("\n=== final ===")
    if partials and max(p["covered_bytes"] for p in partials) == len(buf):
        print("exact reuse available (final needs no new decode)")
    else:
        t0 = time.monotonic()
        final = stt.transcribe(bytes(buf), cfg)
        print(f"full decode {(time.monotonic()-t0)*1000:.0f} ms -> {final.text.strip()!r}")

    # Correctness: streaming must not bias the final decode.
    ref = stt.transcribe(bytes(buf), cfg).text.strip()
    print(f"reference whole-file decode: {ref!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
