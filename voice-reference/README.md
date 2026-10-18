# Voice reference (private)

This directory holds the authorized speaker recording used for Pocket TTS
voice cloning and the normalized working copy derived from it.

**Everything here except this README is Git-ignored** (see `.gitignore`).
Never commit a personal recording, an exported voice embedding, or generated
audio. Never copy the source/processed recordings into the portfolio
repository, public/static assets, or an image. The separate production image
intentionally bakes only the normalized authorized reference from
`deploy/reference/` (Option A); do not mount it over the image or publish it.
Never upload any of these files to a third party.

## Layout

```
voice-reference/
  original/    # the untouched iPad / Voice Memos recording (private)
  processed/   # normalized mono PCM WAV used for inference (private)
  output/      # locally generated comparison audio (private, disposable)
```

`original/` and `processed/` are created on demand by the preparation script.

## Prepare a recording

The script accepts the input path explicitly — it never guesses a filename:

```bash
python3 scripts/prepare-voice-reference.py \
  --input "/path/to/Voice Memo.m4a" \
  --out-dir voice-reference/processed \
  --keep-original            # optional: copy the source into original/
```

It validates the audio (existence, decodability, duration, level, clipping),
converts it to mono PCM WAV at 24 kHz with loudness normalization, caps the
reference at the installed Pocket TTS documented limit (the first 30 s), and
prints a report. The original file is never modified.

The processed file is then referenced from configuration:

```
VOICE_TTS_VOICE_REF=/srv/voice/voice-reference/processed/voice-ref.wav
```

## Cloning availability

Cloning requires the **gated** `kyutai/pocket-tts` weights (accept the model
terms and authenticate Hugging Face). Without them the model loads its
non-cloning fallback and any custom reference is rejected with an actionable
error — the service never substitutes a generic voice and calls it cloned.
See `VOICE_TTS_MODE` in `.env.example` and `../docs/P2C-REPORT.md`.
