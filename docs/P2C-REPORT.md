# P2C — Pocket TTS Voice Cloning + Reference Integration: Report

Scope: local work in `james-voice-service/` only. No Oracle deploy, no DNS/
proxy/secret changes, no public access, no commits. Portfolio untouched.

Status labels: **PASS** · **FAIL** · **BLOCKED** · **NOT TESTED**.

## Bottom line

- Reference recording validated and normalized: **PASS**.
- Voice-reference directory, config, adapter integration, error handling,
  privacy guards: **PASS** (unit-tested).
- Real voice cloning with your recording: **BLOCKED — MODEL ACCESS REQUIRED**
  (gated `kyutai/pocket-tts` weights; no Hugging Face token/terms accepted).
- Voice similarity (perceptual): **NOT TESTED** and **not possible** with the
  current model — no cloned audio was produced, so nothing is claimed about
  similarity. The generic voice is not, and is never called, a clone.
- Preset (non-cloning) real inference: **PASS** (baseline only).

## 1. Files changed (all under `james-voice-service/`)

- `app/tts.py` — preset/clone modes, cloning-capability check, actionable
  `VoiceCloningUnavailable`, cached voice state, per-process lock (Pocket TTS is
  not thread-safe), path-free `describe()`/`preflight()`, one safe `tts_voice_mode` log.
- `app/voice_reference.py` — **new**: pure validation policy + ffmpeg/ffprobe
  probe + mono-24 kHz loudness-normalized conversion capped at the documented 30 s.
- `scripts/prepare-voice-reference.py` — **new**: explicit-path reference prep CLI.
- `app/config.py` — `VOICE_TTS_MODE`, `VOICE_TTS_PRESET_VOICE`.
- `app/main.py` — TTS config wiring + startup log of mode (no paths).
- `docker-compose.local.yml` — read-only mount of `voice-reference/processed`.
- `.env.example` — mode/preset/HF_TOKEN/reference docs.
- `.gitignore` — ignore `voice-reference/{original,processed,output}/`.
- `.dockerignore` — **new**: keep private audio out of the image build context.
- `voice-reference/README.md` — **new** (only tracked file in that dir).
- `README.md` — voice-modes + reference section.
- Tests: `tests/test_voice_reference.py`, `tests/test_tts.py`,
  `tests/test_privacy.py` (**new**).

P2A/P2B uncommitted work preserved unchanged (segmentation, streaming RAG
client, VAD fix, `turn_end_reason`, reports).

## 2. Voice-reference directory and configuration

Directory (Git-ignored except its README):

```
voice-reference/
  original/    Alimosho.m4a            (the untouched iPad/Voice Memos file)
  processed/   voice-ref.wav           (30.00 s, 24 kHz, mono, PCM16, normalized)
  output/      preset-voice.wav        (local comparison output, disposable)
  README.md    (tracked)
```

Prepare:

```bash
python3 scripts/prepare-voice-reference.py \
  --input "/Users/user/Downloads/Alimosho.m4a" \
  --out-dir voice-reference/processed --keep-original
```

Enable clone (once weights are authorized):

```
VOICE_TTS_MODE=clone
VOICE_TTS_VOICE_REF=/srv/voice/voice-reference/processed/voice-ref.wav
HF_TOKEN=<your HF token>          # gated weights; server-side only
```

The container mounts `./voice-reference/processed` **read-only**; the recording
is never copied into the image, never served over HTTP, never uploaded.

## 3. Git-ignore status

`original/`, `processed/`, and `output/` are ignored
(`git check-ignore` confirms each path). Only `voice-reference/README.md` is
tracked. `.dockerignore` additionally excludes `voice-reference/`, `voices/`,
`*.wav`, `*.mp3`, `.env`. `tests/test_privacy.py` asserts these rules and that
the Dockerfile never `COPY`s the reference.

## 4. Pocket TTS version and real model capability

- Installed: **`pocket-tts 3.3.0`** (Kyutai), MIT; Python `>=3.10,<3.15`.
- `TTSModel.load_model()` → **`has_voice_cloning = False`** in this environment.
- `get_state_for_audio_prompt(<custom wav>)` → `ValueError` ("could not download
  the weights for the model with voice cloning…").
- Hugging Face access check (from the container):
  - `kyutai/pocket-tts` → **gated: auto**; unauthenticated weight GET → **401**.
  - `kyutai/pocket-tts-without-voice-cloning` → not gated (the fallback in use).
- No `HF_TOKEN` in the container or host cache. Therefore the cloning-capable
  weights are **not available**, and the model's documented reference limit is
  the **first 30 s** (the normalization cap).

## 5. Test results

Unit / integration (stdlib, no models): `python3 -m unittest discover -s tests`
→ **PASS 129/129** (was 108; +21 new), `py_compile` clean.

New coverage: valid/missing/corrupt/too-short/clipped/quiet audio; duration cap;
mono-24 kHz normalization; clone-unsupported; missing/invalid reference; preset
fallback; prepared-state reuse; concurrent-synthesis serialization; path-free
`describe()`/`preflight()`; ignore/Docker packaging guards.
(Cancellation, interruption, turn IDs, streaming are exercised by the existing
P2A/P2B pipeline and segmentation suites — unchanged and still passing.)

Real inference (recording supplied, real model, in Docker):

| Check | Result |
|---|---|
| Process the real recording (`Alimosho.m4a` → normalized ref) | **PASS** |
| Clone mode preflight with the real reference | **BLOCKED** — `VoiceCloningUnavailable` (gated weights) |
| Preset-mode real synthesis of the shared passage | **PASS** |
| Voice-clone synthesis producing audio | **NOT TESTED** (blocked) |
| Perceptual comparison (human listening) | **NOT TESTED** |

## 6. Technical success?

**No — BLOCKED.** The cloning-capable model did not and cannot load here, so no
cloned speech was generated. The adapter fails explicitly and actionably rather
than substituting a preset voice.

## 7. Perceptual verification?

**NOT TESTED.** No cloned audio exists to evaluate, and this environment cannot
audition audio. Nothing about voice similarity is claimed.

## 8. Latency and resource measurements (host: Intel x86_64, 8 cores/16 GB;
Docker `cpus: 2`; **host load 18–30** during runs — contended)

Preset-mode real synthesis, shared passage (236 chars):

| Metric | Value |
|---|---|
| Model load (cached) | 0.01 s |
| Synthesis | 18.53 s |
| Audio produced | 14.56 s (698,880 bytes, 24 kHz mono) |
| RTF (synth ÷ audio) | **1.27** (slower than real-time under this load) |
| RSS after load | 871 MB |

Clone-mode preflight (before the expected failure): 13.1 s (model load).
Streaming first-chunk and interruption behavior are unchanged from P2A/P2B
(~0.2–0.6 s first chunk on a quiet machine; barge-in stop ~0.02 s).

## 9. Blockers

1. **Model authorization (primary).** `kyutai/pocket-tts` is gated; cloning and
   `export-voice` require accepting the model terms and authenticating. Without
   this, cloning is impossible regardless of code.
2. Hardware/latency: RTF > 1 on this contended 2-vCPU container — not suitable
   for real-time on this machine (unchanged from P2B).

## 10. Exact next steps

1. On Hugging Face, open `https://huggingface.co/kyutai/pocket-tts`, accept the
   terms, and create a **read** token.
2. Provide the token to the voice service only (server-side, never
   `NEXT_PUBLIC_*`): add `HF_TOKEN=<token>` to `james-voice-service/.env`, then
   `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d`.
3. Re-run the clone test:
   ```bash
   docker exec james-voice env HF_HOME=/srv/voice/models PYTHONPATH=/srv/voice \
     python -c "from app.tts import PocketTtsBackend, TtsConfig as C; \
     print(PocketTtsBackend().preflight(C(mode='clone', voice_ref='/srv/voice/voice-reference/processed/voice-ref.wav')))"
   ```
   Expect `cloning_available=True, cloning_active=True` instead of the error.
4. Then generate cloned vs preset comparison audio from the **same** passage and
   have a human listen (perceptual verification) — that step cannot be automated
   here.
5. Only after a clean perceptual pass: expose clone mode in the deployed config
   (still no public enablement in this task).
