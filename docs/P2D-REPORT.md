# P2D — Hugging Face Access + Real Pocket TTS Voice Cloning: Report

Scope: local work in `james-voice-service/`. No Oracle deploy, no DNS/proxy/
secret changes, no public access, no commits. Portfolio untouched. The token is
never printed, logged, or committed.

Status labels: **PASS** · **FAIL** · **BLOCKED** · **NOT TESTED**.

## Bottom line

Real voice cloning now works. The gated model loaded, the reference recording
was initialized, and cloned speech was generated. **Perceptual similarity is
NOT TESTED** — that requires you to listen to the two outputs and compare with
the original; this report makes no claim that the clone "sounds like you".

## 1. Verification checklist

| Item | Status | Evidence |
|---|---|---|
| Token delivery to the service process | **PASS** | `.env` has `HF_TOKEN` (37 chars); container recreated; `present=yes len=37` (value never shown) |
| Hugging Face authentication | **PASS** | `whoami` → user `crystalwebpro` |
| Gated repo authorization (`kyutai/pocket-tts`) | **PASS** | `model_info` returns gated:auto with 643 files visible |
| Cloning-capable model loaded | **PASS** | `has_voice_cloning = True` |
| Reference initialization | **PASS** | `get_state_for_audio_prompt(voice-ref.wav)` succeeded (14.2 s) |
| Real cloned synthesis | **PASS** | `cloned-voice.wav` — 32.80 s of speech |
| Preset baseline synthesis | **PASS** | `preset-voice.wav` — 26.64 s of speech |
| Clone path genuinely used (no preset fallback) | **PASS** | `describe()` = `cloning_active: True`; integration log `mode=clone cloning_active=True` |
| Streaming / segmentation | **PASS** | one WS clone turn emitted 6 ordered audio frames |
| Cancellation / interruption, no stale audio | **PASS** | clone-mode barge-in: `interrupted`, stop 0.00 s, stale frames 0 |
| Concurrent inference thread-safety | **PASS** | unit test asserts serialized synthesis (max concurrency 1) |
| Missing access fails explicitly (no silent fallback) | **PASS** | `VoiceCloningUnavailable` when cloning is unavailable |
| Reference audio valid for the installed version | **PASS** | probe: 30.0 s, 24 kHz, mono, PCM16, mean −18.4 dB, peak −1.5 dB, no issues |
| Private audio Git-ignored + kept out of the image | **PASS** | `git check-ignore` on all files; `.dockerignore` excludes it; `tests/test_privacy.py` |
| Perceptual voice similarity | **NOT TESTED** | cannot audition audio here |

## 2. Exact commands that proved it

```bash
# 1. recreate so the process picks up HF_TOKEN + clone config from .env
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --no-build voice

# 2. direct clone + preset inference via the existing adapter (real model)
docker exec james-voice env HF_HOME=/srv/voice/models PYTHONPATH=/srv/voice \
  python /tmp/p2d_clone.py         # wrote /tmp/{cloned,preset}-voice.wav

# 3. integrated turn (Settings -> adapter -> run_turn: STT -> RAG -> clone TTS)
docker exec james-voice env HF_HOME=/srv/voice/models PYTHONPATH=/srv/voice \
  python /tmp/p2d_integrated.py

# 4. live WebSocket clone turn + barge-in (headless; real STT/RAG/TTS)
cd ../Personal\ website/pwtest-local && node p2d-ws-clone.mjs && node p2d-ws-interrupt.mjs
```

## 3. Output paths (both Git-ignored)

```
voice-reference/output/cloned-voice.wav   32.80 s  24 kHz mono PCM16  1,574,444 bytes
voice-reference/output/preset-voice.wav   26.64 s  24 kHz mono PCM16  1,278,764 bytes
```

Same passage in both modes. Objective content check: transcribing each output
with the service's STT recovers the passage text in both, so both are real
speech of identical text (not a similarity judgement).

## 4. Pocket TTS version and capability

- Installed **`pocket-tts 3.3.0`** (Kyutai), MIT.
- Gated English weights `kyutai/pocket-tts/languages/english/model.safetensors`
  — **219 MB**, downloaded once (now cached in the `voice-models` volume).
- Resulting model reports **`has_voice_cloning = True`**.

## 5. Performance (host: Intel x86_64, 8 cores / 16 GB; Docker `cpus: 2`;
host load 8–30 during runs — contended, so treat as an upper bound)

| Metric | Cloned | Preset |
|---|---|---|
| Model load (uncached, incl. 219 MB download) | 115.4 s | — |
| Model load thereafter (cached) | seconds | seconds |
| Reference-state init | 14.2 s | n/a |
| Synthesis time | 37.5 s | 31.5 s |
| Audio duration | 32.8 s | 26.6 s |
| RTF (synth ÷ audio) | **1.15** | **1.18** |
| First generated audio (streaming) | **0.37 s** | — |
| Peak RSS | ~1.09–1.13 GB | ~1.0 GB |

Live WS clone turn (cold service process): first audible audio **33.0 s**,
total **64.4 s**, answer audio **39.6 s**, 6 frames. Sub-second first *chunk* is
the model-side figure; the 33 s includes one-time model load + 14 s reference
init + remote RAG + first segment under load. Barge-in stop: **0.00 s**.

## 6. Corrections made in this task

Two small diagnostic defects found while verifying observability (no cloning
logic changed):

- `app/main.py` — `create_app()` never passed `lifespan`, so the startup
  diagnostic (`voice_startup`, incl. `tts_mode`) never ran. Now wired.
- `app/tts.py` — the `tts_voice_mode` log used a logger that wasn't configured,
  so it was invisible. It now uses the service's `voice` logger.

Result (safe, path-free):
```
{"event":"voice_startup", ..., "tts_mode":"clone", "tts_ref_configured":true}
tts_voice_mode mode=clone cloning_available=True cloning_active=True ref_configured=True
```

No new tests were added: the cloning gating, state reuse, concurrency, missing
reference, and packaging behaviors were already covered; the full suite remains
**129/129 PASS**.

## 7. Configuration in use (local, Git-ignored `.env`)

```
HF_TOKEN=<redacted>                                   # server-side only
VOICE_TTS_MODE=clone
VOICE_TTS_VOICE_REF=/srv/voice/voice-reference/processed/voice-ref.wav
```

The reference directory is mounted **read-only** by `docker-compose.local.yml`;
the recording is never copied into the image and never served over HTTP.

## 8. Blockers / limitations

- **Perceptual similarity: human listening required.** I cannot audition audio.
  Play `voice-reference/output/cloned-voice.wav` next to the original
  `voice-reference/original/Alimosho.m4a` and `preset-voice.wav`.
- **Latency**: RTF > 1 on this contended 2-vCPU Intel host — not real-time here
  (unchanged from P2A/P2B). Re-measure on the target CPU.
- The cloned voice will sound best if the 30 s reference is clean, continuous
  speech; the current clip is a 30 s excerpt of a 99 s memo (mean −18 dB after
  normalization). If similarity is weak, re-prep (a quieter/cleaner 5–15 s
  segment may help — but that is a quality judgement only you can make).

## 9. Remaining next step

**Human listening.** Compare the two outputs against the original recording:

```bash
open voice-reference/output/cloned-voice.wav
open voice-reference/output/preset-voice.wav
open voice-reference/original/Alimosho.m4a
```

If the cloned output is perceptually close, the cloning integration is complete
for local use; no code change is required to proceed to a deployment decision.
