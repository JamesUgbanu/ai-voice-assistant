# P2A — Pocket TTS + Real-Time Voice Integration: Local Validation Report

Status: **PARTIAL PASS (non-cloning), with a hard blocker on voice cloning.**
Nothing here is production-accepted; no Oracle VPS deploy and no public voice
access were performed.

## 1. Verified facts (source-level, pocket-tts 3.3.0)

Verified against the installed PyPI sdist/wheel and the running Docker image,
not from memory.

| Fact | Finding |
|---|---|
| Package / version | `pocket-tts==3.3.0` on PyPI ("Kyutai's pocket-sized text-to-speech!") |
| License | **MIT** (bundled `LICENSE`); PyPI exposes no license metadata |
| Python | `>=3.10,<3.15` |
| Model size | ~100M params, CPU-first (README) |
| Public API | `TTSModel.load_model`, `get_state_for_audio_prompt`, `generate_audio`, `generate_audio_stream`, `.sample_rate`, `.device`, `export_model_state` |
| Streaming | **Real** — `generate_audio_stream(...)` yields `torch.Tensor` `[samples]` at 24 kHz as decoded; supports a `stop: threading.Event`; **not thread-safe** (one instance per concurrent generation) |
| Chunk semantics | Each yielded tensor is decoded Mimi audio (24 kHz mono) for a token window; `MAX_TOKEN_PER_CHUNK = 50`; `frame_rate = 12.5`, sample rate **24000** |
| Voice reference | `get_state_for_audio_prompt(path|url|safetensors|tensor)`; resamples to 24 kHz; `truncate=True` caps at 30 s; README example uses a ~5 s clip |
| Precomputed embeddings | **Yes** — `export-voice` CLI and `export_model_state()` convert audio → safetensors voice state (fast to load) |
| Voice cloning access | **GATED.** Cloning weights are `hf://kyutai/pocket-tts/...` and require accepting terms at `huggingface.co/kyutai/pocket-tts` + `hf auth login`. Without them, `load_model()` falls back to `kyutai/pocket-tts-without-voice-cloning` and sets `has_voice_cloning=False`; any custom wav or `export-voice` then raises `VOICE_CLONING_UNSUPPORTED`. |
| Predefined voices | Non-gated (`kyutai/tts-voices/...`), e.g. `alba`; usable **without** cloning weights |
| ARM64/Linux | Pure Python + torch/safetensors/tokenizers/sentencepiece (all ship aarch64 wheels); community `sherpa-onnx` port targets Linux/ARM boards. No measured ARM numbers here. |
| Native deps | `ffmpeg`, `libsndfile1` (Dockerfile); `soundfile` is an optional extra (not installed in the image) |

`export-voice` **does** exist (CLI command + docs), contrary to the caution —
but it is unusable without the gated cloning weights.

## 2. Environment

- Host: macOS 13.7.8, **x86_64** (Intel), 8 CPU, 16 GiB RAM. **Not ARM64.**
- Docker: 24.0.7, x86_64 engine, 8 GiB VM.
- Voice service run via its own Dockerfile on `python:3.12-slim-bookworm`,
  CPU-only torch, `--cpus=2 --memory=6g` per run (compose plans `cpus: 2.0`,
  `mem_limit: 10g`).
- **Host cannot resolve `huggingface.co`; the Docker container can.** All model
  runs therefore happened inside the container. This is an environment quirk,
  not a deployable assumption.

## 3. Measured results (x86_64 Linux CPU, 2 vCPU, torch 2.14.1+cpu, threads=1)

First (cold) `load_model()`: **105.5 s** incl. weight download. Cached load
(model + predefined voice state): **2.27 s**; `alba` voice state encode ~0.004 s
(precomputed state).

| Text | chars | audio s | full RTF | first chunk s | stream RTF | peak RSS |
|---|---|---|---|---|---|---|
| short | 40 | 3.04 | 0.80 | **0.249** | 0.71 | 1.28 GB |
| names | 86 | 5.04 | 0.66 | **0.209** | 0.75 | 1.31 GB |
| tech | 100 | 6.88 | 0.74 | **0.227** | 0.68 | 1.34 GB |
| long | 213 | 13.20 | 0.81 | **0.633** | 1.12 | 1.37 GB |

Per-segment synthesis (simulating the segmentation buffer, one short phrase at a
time — no model re-init between calls):

| Segment | audio s | full RTF | first chunk s | stream RTF |
|---|---|---|---|---|
| "Yes, I have." | 1.68 | 1.46 | 0.245 | 1.10 |
| "I've worked extensively with AWS and Terraform…" | 4.16 | 1.39 | 0.308 | 1.26 |
| "The closest match in my background…" | 3.36 | 1.21 | 0.320 | 1.26 |
| "Would you like me to go deeper…?" | 2.40 | 1.32 | 0.297 | 1.32 |

**First-chunk target (<500 ms): MET** for short/medium utterances (~210–320 ms);
the long single utterance hit 633 ms. With sentence segmentation, first audio is
~210–320 ms.

**RTF target (<1.0): MET for continuous synthesis** (0.66–0.81) but **NOT for
very short segments** (1.2–1.5). There is a ~2 s **fixed per-`generate_audio`
call overhead** (EOS/padding tail), so splitting into tiny phrases is
*inefficient*. Implication: prefer synthesizing a whole sentence stream rather
than clause-sized calls; the buffer's `min_chars`/`clause_trigger_chars`
defaults are deliberately conservative for this reason.

**Memory: ~1.2–1.4 GB RSS**, well under the 6–10 GB budget.

**Cloning / `export-voice`: BLOCKED.** Probe returned
`ValueError: We could not download the weights for the model with voice
cloning…` — the gated `kyutai/pocket-tts` weights were not (and must not be)
bypassed. No authorized-voice clone test was run.

## 4. STT round-trip (real models, headless)

Pocket TTS → 16 kHz PCM → `app.stt.FasterWhisperBackend` (`small.en`, int8, 2
vCPU):

| Spoken | Transcript | note |
|---|---|---|
| "Hello, I am James's portfolio assistant." | "Hello, I am James's Portfolio Assistant." | exact |
| "…Obinna and Chidera on backend systems." | "…Obinna and **Chedera** on back-end systems." | Nigerian name misspelled |
| "…PostgreSQL and pgvector." | "…PostgreSQL and **Protector**." | technical term error |

Steady-state STT after warm-up: ~5.2–5.6 s per 3.2–4.4 s clip → **RTF ≈ 1.2–1.6**
on 2 vCPU (slower than realtime). First call included ~270 s model download/load.
STT is a latency risk; `base.en` or more cores should be benchmarked.

## 5. Incremental TTS buffering (implemented)

- `app/segmentation.py` — bounded `SegmentationBuffer`: sentence-first, clause
  fallback, configurable `max_chars`/`max_wait_ms`/`min_chars`, hard cap,
  no mid-token cuts, abbreviation/decimal/acronym protection, Unicode and
  trailing-whitespace handling, ordered flush, `reset()` on cancel.
- `app/rag_client.py` — `SseStreamParser` (splits across arbitrary chunk
  boundaries, `close()` drain) and `iter_answer()` async generator with
  per-token `should_stop`, max-chars cap, and clean transport/HTTP/rag/empty
  error mapping that never misclassifies a consumer-side error.
- `app/pipeline.py` — `run_turn(..., rag_stream=...)` streams the answer,
  segments it, and synthesizes each segment as it completes; checks `alive()`
  before every synthesis, resets the buffer on cancel, finalizes partial
  answers on mid-stream error, and never emits stale-turn audio. The existing
  full-answer path is preserved.
- `app/main.py` — the real route now wires `rag_stream` into `run_turn`.

**Tests: 38 new, all passing** (101 total in the service, up from 63) —
`tests/test_segmentation.py` (17), `tests/test_rag_streaming.py` (12),
`tests/test_pipeline_streaming.py` (9): boundaries, max-wait, flush, ordering,
cancellation discards queued audio, mid-stream error recovery, stale turns,
split-frame SSE, `should_stop`, error mapping.

Because Pocket TTS has ~2 s fixed per-call overhead (§3), **per-segment
synthesis degrades RTF**; the honest conclusion is that the buffer should emit
whole sentences (fewer, longer segments), which its defaults do.

## 6. Voice quality (§5) — NOT evaluated

Not performed: this environment has no way to audition audio. The TTS adapter
produced valid 24 kHz PCM and the STT round-trip transcribed it, but
intelligibility, accent preservation, voice similarity, prosody, and chunk
discontinuities require a **human listening pass** with an authorized 5–15 s
reference clip. One indicator: Whisper transcribed the TTS output almost
exactly, which is weak evidence of intelligibility.

## 7. Browser integration & barge-in (§3/§6) — PARTIAL

- Cancellation is verified at the orchestration level (unit/integration):
  mid-stream `should_stop`, `state.cancel()`, buffer reset, no final frame, no
  stale audio. `app/main.py` already cancels the turn task and drops the audio
  buffer on interrupt/disconnect.
- **Not performed:** a real browser microphone turn, end-of-speech →
  first-audible-audio end-to-end timing, in-browser barge-in during STT/RAG/TTS
  playback, and a repeated-session memory/leak soak. These need a browser and
  audio device.

## 8. Decision

- Pocket TTS **passes at the component level for non-cloning, self-hosted CPU
  streaming**: MIT, installs on CPU Linux, real streaming, first chunk
  ~0.21–0.32 s, RTF < 1.0 for continuous speech, ~1.4 GB RAM.
- It **fails the "real voice clone" acceptance here**: the cloning weights are
  gated and unavailable, so neither custom-voice synthesis nor `export-voice`
  for the intended speaker could be validated.
- Performance is **not** transferable to the Oracle VPS: all numbers are
  **x86_64 Intel**, not the target ARM. ARM Neoverse throughput, int8 behavior,
  and accent fidelity remain **unmeasured**.
- STT latency (~1.2–1.6 RTF on 2 vCPU) is a risk to tune (`base.en`, more cores).

**Recommendation:** treat Pocket TTS as *provisionally viable* for a non-cloning
launch on x86; do **not** deploy to Oracle until (a) the gated cloning terms are
accepted for the intended voice, and (b) the same measurements are repeated on
the actual ARM target. Do not evaluate "OmniVoice" as a fallback yet — Pocket
TTS has not failed on a verified ARM CPU, only remained unmeasured; the cloning
gate is a licensing/access decision, not a runtime failure.

## 9. Reproduce

```bash
# image (validated: builds on CPU Linux, installs torch + pocket-tts 3.3.0)
docker build -t james-voice-service:0.1.0 .

# component measurements (container can reach HF; host here could not)
docker run --rm --cpus=2 --memory=6g -v voice-models:/srv/voice/models \
  -e HF_HOME=/srv/voice/models --entrypoint python james-voice-service:0.1.0 <script>

# unit/integration suite (no models, no network)
python3 -m unittest discover -s tests -q   # 101 tests
```
