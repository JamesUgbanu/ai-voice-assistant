#!/usr/bin/env python3
"""Prepare an authorized recording into a Pocket TTS reference.

Validates and normalizes a supplied audio file (Apple Voice Memos .m4a works
via ffmpeg) into a mono PCM WAV at the model sample rate, capped at the
installed Pocket TTS documented reference limit. The input path is always
explicit — this script never guesses a filename.

    python3 scripts/prepare-voice-reference.py \
        --input "/path/to/Voice Memo.m4a" \
        --out-dir voice-reference/processed \
        --keep-original

The original recording is never modified. Output goes to an ignored directory.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.voice_reference import (  # noqa: E402
    NORMALIZED_SAMPLE_RATE,
    POCKET_TTS_REF_MAX_SECONDS,
    VoiceReferenceError,
    clamp_reference_seconds,
    normalize,
    probe,
    validate_info,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Prepare a Pocket TTS voice reference.")
    p.add_argument("--input", required=True, help="path to the authorized recording (m4a/wav/mp3/...)")
    p.add_argument("--out-dir", default="voice-reference/processed", help="output directory (git-ignored)")
    p.add_argument("--output-name", default="voice-ref.wav", help="normalized file name")
    p.add_argument("--max-seconds", type=float, default=POCKET_TTS_REF_MAX_SECONDS,
                   help=f"max reference seconds (Pocket TTS limit: {POCKET_TTS_REF_MAX_SECONDS:.0f}s)")
    p.add_argument("--sample-rate", type=int, default=NORMALIZED_SAMPLE_RATE)
    p.add_argument("--keep-original", action="store_true",
                   help="also copy the source into voice-reference/original/ (git-ignored)")
    args = p.parse_args()

    if not os.path.isfile(args.input):
        print(f"ERROR missing: no readable file at the supplied --input path ({os.path.basename(args.input)})")
        print("Place the recording anywhere readable and pass its path, e.g.:")
        print('  python3 scripts/prepare-voice-reference.py --input "/Users/you/Downloads/Alimosho.m4a"')
        return 2

    try:
        info = validate_info(probe(args.input))
    except VoiceReferenceError as exc:
        print(f"ERROR {exc.code}: {exc.message}")
        return 2

    used = clamp_reference_seconds(info.duration_s, args.max_seconds)
    print("Reference audio report")
    print(f"  source           : {os.path.basename(args.input)}")
    print(f"  container/codec  : {info.container} / {info.codec}")
    print(f"  duration         : {info.duration_s:.2f}s (will use first {used:.2f}s)")
    print(f"  sample rate      : {info.sample_rate} Hz")
    print(f"  channels         : {info.channels}")
    print(f"  level            : mean {info.mean_db} dB / peak {info.peak_db} dB")
    print(f"  warnings         : {', '.join(info.issues) if info.issues else 'none'}")

    dst = os.path.join(args.out_dir, args.output_name)
    try:
        out = normalize(args.input, dst, sample_rate=args.sample_rate, max_seconds=args.max_seconds)
    except VoiceReferenceError as exc:
        print(f"ERROR {exc.code}: {exc.message}")
        return 2

    if args.keep_original:
        orig_dir = "voice-reference/original"
        os.makedirs(orig_dir, exist_ok=True)
        shutil.copy2(args.input, os.path.join(orig_dir, os.path.basename(args.input)))

    print("\nWrote normalized reference:")
    print(f"  {dst}")
    print(f"  {out.duration_s:.2f}s  {out.sample_rate} Hz  {out.channels}ch  {out.codec}")
    print("\nConfigure the service (host path inside the container):")
    print("  VOICE_TTS_MODE=clone")
    print(f"  VOICE_TTS_VOICE_REF=/srv/voice/{dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
