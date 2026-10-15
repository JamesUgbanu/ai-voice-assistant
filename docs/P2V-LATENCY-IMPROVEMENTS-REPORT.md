# P2V — Latency and Responsiveness: Report

## Root causes (evidence-ranked)

1. **Remote RAG is the dominant bottleneck (external provider).** Fresh
   uncached structured query, measured inside the portfolio's own
   `done.metrics`: retrieval 0 ms (pgvector), rerank 0 ms (local IDF),
   LLM TTFT **1.9 s**, generation **4.4 s** for 301 chars. Voice-turn tails
   (33–85 s) are long-answer LLM generation at provider speed, not local
   staging: the portfolio streams genuinely (`onToken`→SSE immediately, no
   buffering), retries are bounded (2 + backoff), telemetry/cache writes sit
   off the first-token path. Nothing to optimize without changing provider,
   answer length, or contract — all out of mandate.
2. **Host contention scales every local stage.** Same turns measured STT
   1.3 s (moderate load) vs 3.1 s (builds churning), TTS RTF ~0.5 vs
   realtime-plus, first-audio 5 s vs 14 s. Single-CPU reality, not a defect.
3. **Cache behavior confirmed healthy**: repeat query 3200 ms → 705 ms →
   474 ms (cold compile → warm, retrieval/rerank 0, instant streamText).
4. **No defect found** in endpointing, turn submission, RAG/TTS invocation
   order, chunking, scheduling, cancellation, stale filtering, UI states, or
   WS lifecycle — each re-verified; no fix applied where none was indicated.

## Before/after latency by stage

| stage | moderate load | heavy load | notes |
|---|---|---|---|
| eou → transcript (STT) | 1.3 s | 2.5–3.1 s | base.en greedy, model-bound |
| RAG first token | 3.1 s | 6.9–7.4 s | upstream TTFT + embedding |
| RAG tail (**new `rag_total_ms`**) | 33.7 s | ~85 s trickling | answer length × provider speed |
| TTS first chunk | 0.7 s | 0.9–4.2 s | server-side |
| eou → first audible | 5.1 s | 10.9–14.1 s | greeting ~2–3 s (fast path) |
| TTS total (499 frames) | 43 s | 65–107 s | RTF 0.5–0.6 loaded |
| server → browser | ~35 ms | same | localhost, negligible |
| browser recv → play | 8 ms | same | correct + ordered |
| playback gaps | continuous unloaded | p50 161 ms vs 80 ms chunks loaded | server emission cadence, not browser |

Sample counts are small (n≈2–4 per condition, stated, not p95-grade).

## RAG / audio / transcription findings

- RAG first-token/total: above. Base64+envelope ≈ 5.3 KB per 80 ms chunk
  (~60 KB/s) — negligible by design; queue stays bounded (64); 80 ms uniform
  chunks; TTS output verified healthy (no crackle/distortion in signal).
- Playback cases: generated audio clean; stutter under load is emission
  cadence (CPU), not scheduling; starts late only via RAG; voice quality
  unmeasured (no panel) — clone verified active.
- Transcription: file matrix 8/8 exact (incl. names/technical); real-mic
  clean→correct, degraded→rejected-or-marginal at the documented
  separability limit. Added boundary regression test (`Come on.` passes by
  design). Thresholds, VAD, model, decoding **unchanged** — no comparative
  test justified a move. Accent validation: not claimed (nimoh origin
  unverified; reporter captures are the only real-mic data).

## Changes (uncommitted, per constraints)

- `app/pipeline.py`, `app/main.py`: `TurnTimings.rag_total_ms` (first→last
  RAG token, else wait time) + `turn_timing` emission.
- `tests/test_pipeline_streaming.py`, `tests/test_stt_guard.py`: advancing-clock
  RAG-duration test + separability-boundary test.
- Voice suite **164/164**; portfolio 607 + 2 pre-existing unrelated; `tsc`
  clean; `next build` blocked only by sandbox font-fetch (environmental).
- No portfolio, dependency, model, threshold, or limit changes.

## Remaining constraints / next highest-value

External LLM latency + answer length (a voice-appropriate length hint would
need owner-approved contract change); single-CPU TTS throughput (capacity,
not code); 2 GB host NOT approved; ARM unmeasured; reporter mic confirmation
still the final acceptance.
