# Voice Latency Optimization: Report

## 1. Baseline measurements

Browser-measured (headless Chromium, fake mic, `pwtest-local/e2e-timing.mjs`)
plus server `turn_timing`, same hello fast-path turn:

| stage | measured |
|---|---|
| last speech → `end_utterance` (endpointing) | **950 ms** (900 ms silence window + frame cadence, by design) |
| eou → transcript displayed | 4.6 s (server STT 4.2–4.6 s small.en under load; 1.2 s unloaded base era) |
| eou → first audible (greeting fast path) | 4.9 s (transcript + TTS first chunk ~0.3–0.6 s) |
| UI render/transport | negligible (WS localhost; state transitions observed same-task) |
| Full RAG turn (q-aws, separate runs) | eou→transcript 1.3–4.2 s; RAG first token 2.8–7.4 s; RAG tail to ~85 s loaded; TTS total paced by stream; complete 44–110 s |

## 2. Confirmed source of each delay

- **Endpointing 950 ms**: the 900 ms silence rule working exactly as specified.
  Shortening it risks truncating trailing words and mid-sentence pauses;
  rejected without a safe alternative — kept.
- **STT 1–5 s**: faster-whisper small.en single inference on 1 CPU; model loads
  once (singleton, verified), runs once per turn (new regression assertion:
  exactly one `stt.calls` entry per turn). Threading sweep (`cpu_threads`
  0/1/2/4 ×3 trials): transcripts identical everywhere; timings drowned in
  host-load variance (10→18 s drift within one setting) — no setting justified
  a change; default kept.
- **VAD ~40 ms/turn** (first-ever call pays one-time model load, amortized in
  warm-up); runs once per turn; no redundancy found.
- **RAG first token 3–7 s, tail to ~85 s**: external OpenAI path (retrieval +
  rerank measured ~0 ms locally); bounded retries; genuine streaming. The
  dominant cost, not locally fixable.
- **TTS**: first chunk 0.3–0.6 s typical; total paced by RAG stream (already
  incremental — first audio well before stream end, proven by test and live
  runs). 80 ms uniform chunks; bounded queue (64); ordered; interruptible.
- **Browser**: 8 ms receive→play, correct scheduling; playback gaps track
  server emission cadence under load, not browser buffering.

## 3. Experiments and methodology

- `e2e-timing.mjs` (new, untracked dev harness): worklet-level last-active,
  `end_utterance` send, transcript/first-audio receipt timestamps.
- Threading matrix: 4 configs × 3 trials, identical transcripts required
  (accuracy-neutral gate for any threading claim).
- VAD timing via direct backend calls (cold vs warm separated).
- Transcription matrix re-verified unchanged (8/8 file cases exact).
- All runs labeled with host load (3–10); no p95 claims (n too small).

## 4. Before-and-after latency and accuracy results

No production-behavior change was justified, so there is no before/after
delta to report — this is the honest outcome of the evidence rule. Accuracy
suites pass unchanged (reference matrix exact; guard boundary test intact).

## 5. Changes implemented and rationale

- `tests/test_pipeline.py`: assert exactly one STT call per turn (locks the
  no-duplicate-transcription invariant found during the audit).
- `pwtest-local/e2e-timing.mjs`: endpoint-to-transcript measurement harness
  (untracked dev tool).
- Nothing else: endpointing, VAD, threading, segmentation, RAG, TTS,
  thresholds, models, prompts, and contracts all deliberately unchanged.

## 6. Regression test results

- Voice service: **171/171 pass** (5 pre-existing env skips: no ffmpeg on host).
- Portfolio voice suites + `tsc`/`eslint`: green (unchanged files this round).
- Live: hello fast-path complete with full stage logs; barge-in lifecycle
  re-verified earlier in this state.

## 7. Remaining bottlenecks and next steps

1. Remote RAG tail (provider-bound; a voice-appropriate length hint needs an
   owner-approved contract change — not made).
2. Single-CPU STT/TTS throughput under contention (capacity, not code).
3. `next build` in this sandbox (Google-Fonts fetch blocked; environmental).
4. Real-mic acceptance of audibility remains the reporter's step.
