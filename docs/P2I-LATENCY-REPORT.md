# P2I — Talk to James Latency Report

Scope: measured performance engineering in `james-voice-service/`. The portfolio
was not changed (it remains the sole knowledge source and answer LLM). Pocket TTS
clone mode preserved. No deploy, no commits, no infrastructure changes.

Status: **PASS** for the three optimizations (streaming TTS, storage stall-flush,
startup warm-up). The provisional end-of-speech→first-audio targets are **NOT
MET** on this host; the dominant remaining cost is STT + the remote RAG, not TTS.

## 1. Headline numbers (warm, fixed 5-question suite, n=10 turns)

Host: macOS Intel x86_64, Docker `cpus: 2`, RAM 10g; host load ~6–9 during runs.
Voice: Pocket TTS **clone** mode.

| Stage (ms) | baseline p50 | optimised p50 | optimised p95 | optimised max |
|---|---|---|---|---|
| STT (end→transcript) | 10,422 | **3,704** | 20,020 | 20,020 |
| RAG first token | 3,070 | 2,608 | 4,502 | 4,502 |
| TTS first chunk | 17,834 | **336** | 4,364 | 4,364 |
| **End of speech → first audio** | **37,497** | **5,749** | 30,448 | 30,448 |
| TTS total (whole answer) | 80,540 | 34,884 | 108,678 | 108,678 |
| Turn total | 90,302 | 37,461 | 111,409 | 111,409 |

Baseline = the pre-P2I path (each segment fully synthesised before the first
frame). Optimised = streamed TTS (below). Both measured on the same host; the
two runs differed in ambient load, so treat the delta as directional.

**Targets:** p50 ≤ 2.5 s, p95 ≤ 4 s → **not met** (5.7 s / 30.4 s). The floor is
STT (3.7 s) + remote RAG first token (2.6 s); TTS first audio is now 0.34 s.

## 2. Stage-by-stage attribution (optimised)

`first_audio ≈ stt + rag_first_token + tts_first_chunk`:
`3.7 + 2.6 + 0.34 ≈ 6.6 s` (measured p50 5.7 s; the small gap is STT/RAG overlap
and clock rounding).

**Portfolio RAG internal stages** (uncached, from its own SSE `done.metrics`;
retrieval includes the embedding HTTP call + pgvector, rerank is local):

| question | retrievalMs | rerankMs | LLM first token | generation | RAG total |
|---|---|---|---|---|---|
| cloud services | 6,169 | 10 | 2,961 | 5,283 | 15,369 |
| databases | 1,756 | 0 | 1,189 | 7,473 | 12,508 |
| recent role | 3,477 | 1 | 1,143 | 4,396 | 9,287 |
| distributed exports | 2,050 | 1 | 1,254 | 3,990 | 9,852 |

So the remote RAG cost splits roughly **retrieval ~2–6 s** + **LLM first token
~1.1–3 s**; cached questions return in 0.5–3 s. This is external to the voice
service and cannot be reduced without changing the portfolio's contract (out of
scope).

**STT comparison** (warm, fixed suite; WER is a crude token-error metric, used
only for relative comparison):

| config | p50 s | max s | mean WER |
|---|---|---|---|
| small.en / beam 5 (current default) | 7.41 | 8.15 | 0.173 |
| small.en / beam 1 | 5.99 | 6.62 | 0.173 |
| base.en / beam 5 | 2.17 | 2.48 | 0.196 |
| base.en / beam 1 | 2.00 | 4.80 | 0.196 |

`base.en` is ~3× faster at ~+2 pp WER (transcripts nearly identical on this set);
`beam 1` is ~20 % faster at equal accuracy. Both are now env-configurable; the
default is unchanged (small.en, beam 5) to protect answer quality.

**WebSocket end-to-end (real path, browser-equivalent client):** first audio
**14.5 s** on the first turn after a fresh session under contention (490 streamed
frames, 39.5 s of synthesized audio), `turn_end=complete`. This is the first-turn
cost; steady-state warm turns are the 5.7 s figure above.

**Cold vs warm:** model load measured at startup 27–92 s depending on contention
(`models_warm`: vad 4.6–7.9 s, stt 7.9–29.4 s, tts 14.8–55.7 s). This is now paid
**off the request path**; before P2I it was paid on the first turn (the source of
the historical cold-first-turn and the 279 s outlier).

## 3. Root causes of slow turns and outliers

1. **Whole-segment TTS synthesis (dominant, fixed).** `speak` used
   `synthesize_full`, so no audio was emitted until an entire 100–400-char
   segment (~10–30 s of speech) finished synthesising. At RTF ≈ 1.2–3.6 under
   load that is 15–50 s before the first frame. Fix: stream TTS chunks.
2. **Indefinite wait on a quiet RAG stream (fixed).** The segmentation
   `max_wait` was only evaluated when a new token arrived; a stalled upstream
   produced no flush and no timeout — an unbounded wait. Fix: a timer-driven
   poll that flushes pending text at the max-wait while the stream is quiet.
3. **Cold model load on the first turn (fixed).** STT+TTS+VAD weigh 27–92 s.
   Fix: background warm-up at startup.
4. **STT is multi-second** on 2 vCPU (3.7–7.4 s). Partly configurable (beam,
   model, threads); the residual is CPU-bound.
5. **Remote RAG/LLM** ~5–9 s (retrieval ~2–6 s + LLM ttft ~1–3 s). External.
6. **Host CPU contention** inflates every stage and explains the p95/max spikes
   (e.g. first-audio max 30 s = a 20 s STT spike + a 4.4 s TTS-first spike). No
   stall or hang was observed; every turn completed (`error: null`).

The old **279 s outlier** was cold STT+TTS load (plus contention) on the first
turn, not an LLM stall — the warm-up and streaming changes remove that regime.

## 4. Files and functions changed

- `app/tts.py` — `TtsBackend.synthesize_stream` (default: one full clip) and a
  real streamed implementation in `PocketTtsBackend` via
  `TTSModel.generate_audio_stream(..., stop=…)`; `supports_incremental()` now true.
- `app/pipeline.py` — `TurnTimings` adds `first_audio_ms`/`total_ms`; the
  incremental loop now polls the RAG stream with `asyncio.ensure_future` +
  `asyncio.wait(timeout)` and flushes `SegmentationBuffer.poll()` while quiet;
  `speak()` streams TTS through a worker thread + queue with a `threading.Event`
  stop for barge-in; `segmentation` config accepted.
- `app/stt.py` — `SttConfig.beam_size`, `cpu_threads`; used in `_model`/`transcribe`.
- `app/config.py` — `VOICE_STT_BEAM_SIZE`, `VOICE_STT_CPU_THREADS`,
  `VOICE_TTS_{MAX_WAIT_MS,CLAUSE_TRIGGER_CHARS,MAX_CHARS,MIN_CHARS}`.
- `app/main.py` — `warm_models()` background task in `lifespan`; per-turn
  `turn_timing` structured log; passes segmentation config to `run_turn`.
- `.env.example` — documents the new knobs.
- Tests: `tests/test_pipeline_streaming.py` (stalled-stream flush, streamed
  multi-chunk ordering, timing capture), `tests/test_tts.py` (streamed chunks).

Preserved: turn ordering, quotas, auth, stale-audio prevention, barge-in,
cancellation, SSE parsing, per-segment backpressure (the loop awaits each
`speak` before pulling more text), and the public RAG contract.

## 5. Trade-offs

- **Accuracy/voice quality:** unchanged. Streaming uses the same Pocket TTS clone
  model and voice state and the same text; only *when* audio is emitted changes.
  STT defaults are unchanged (base.en/beam1 are opt-in, with measured accuracy).
- **CPU/memory:** the streaming path adds a worker thread + small per-segment
  queue; process RSS was ~1.1–2.2 GB (model-resident), unchanged. Frame count per
  answer rises (~470 vs ~5) — more, smaller WebSocket messages; measured cost is
  negligible relative to inference.
- **Inter-chunk gaps:** if synthesis runs slower than real time under contention
  (RTF > 1), streamed playback can gap; on a quiet host RTF < 1 (P2A: ~0.7) so
  playback is smooth. This is a contention artifact, not a logic change.

## 6. Regression tests & results

- Voice suite: **133/133 PASS** (`python3 -m unittest discover -s tests`), +2
  new streaming tests, +1 stall-flush test; `py_compile` clean.
- Stall flush: a stream that yields text then sleeps past max-wait emits audio
  **before** it resumes (asserted).
- Streamed ordering: 2 segments × 2 chunks → 4 ordered audio frames + 1 final.
- Timings: `first_audio_ms > 0` and `total_ms ≥ first_audio_ms`.
- WS barge-in (real path): interrupt stop **0.06 s**, **0 stale audio frames**.
- WS clone turn: transcript correct, `turn_end=complete`, audio delivered.
- Portfolio: not modified by P2I (its uncommitted files are from prior phases).

## 7. Remaining bottlenecks and next steps

Dominant, in order: (1) **remote RAG** retrieval + LLM (~5–9 s uncached),
(2) **STT** (3.7–7.4 s), (3) host CPU contention (p95 spikes). TTS is no longer a
first-audio bottleneck (0.34 s).

Recommended next steps (each measured before adoption):
1. **STT:** if a modest accuracy cost is acceptable, set `VOICE_STT_MODEL=base.en`
   (~3 s saved) and/or `VOICE_STT_BEAM_SIZE=1`; re-validate name/technical-term
   accuracy on a larger Nigerian-English set.
2. **RAG:** the retrieval leg (embedding round-trip + pgvector) is 2–6 s; caching
   common questions (already present) and pre-warming the LLM connection help.
   Any change must respect the portfolio as sole knowledge source.
3. **Hardware:** the targets (p50 ≤ 2.5 s) are unreachable on 2 contended vCPUs
   with a remote LLM; re-measure on a quiet/dedicated CPU and the ARM target.
4. **First-turn:** warm-up removes the cold hit; confirm health/readiness gating
   so a turn never races an in-progress warm-up on very small instances.

## 8. Limitations

- Two runs (baseline/optimised) were taken under different ambient load; the
  direction is robust, the multiplier is approximate.
- No physical-microphone or human-listening validation was performed; the WS
  client is browser-equivalent but synthetic.
- All figures are x86_64, not the ARM VPS.
