# P2U — Latency, Accuracy, and Responsiveness Audit: Report

## 1. Root causes, ranked (evidence-backed)

1. **Remote RAG latency dominates every non-greeting turn** (upstream, not a
   voice defect): first token 6.9–28 s across runs (OpenAI path + retrieval);
   RAG tail now measured up to ~85 s trickling under host load. Nothing
   voice-side creates it; the portfolio streams correctly.
2. **Host contention inflates all local stages**: STT 1.1→3.1 s, TTS RTF
   ~0.5–0.6 under load (vs realtime-plus unloaded). Environmental.
3. **Degraded mic input mistranscribes** (acoustic, proven on reporter audio);
   the STT guard is calibrated at the demonstrated separability limit —
   `Alo.`(-0.43)/`Home.`(-0.27) are statistically adjacent to genuine short
   speech and cannot be gated without eating legit utterances.
4. No defect found in: endpointing, turn submission, RAG/TTS invocation,
   chunk ordering, playback scheduling, cancellation, stale filtering, UI
   states, or WS lifecycle (each verified below). No fix applied where none
   was indicated.

## 2. Before/after latency by stage (load-labeled)

q-aws turn, current load (my builds/probes churning the same CPU):

| stage | measured |
|---|---|
| endpointing (client silence) | ~0.9 s by design |
| eou → transcript (STT) | 2.5–3.1 s (1.1–1.9 s unloaded) |
| RAG first token | 6.9–7.4 s (2.6–28 s across loads) |
| **RAG tail (NEW `rag_total_ms`)** | **up to ~85 s trickling under load** |
| TTS first chunk | 0.9–4.2 s server-side |
| eou → first audible | 10.9–14.1 s (2.15 s greeting / ~8 s substantive unloaded) |
| TTS total (499 frames) | 65–107 s under load (RTF ~0.5–0.6) |
| server→browser delivery | ~35 ms (localhost: client 14.11 s vs server 14.075 s) |
| browser receive→play | 8 ms; scheduling correct and ordered |
| full turn | 69–110 s loaded (fast-path greeting ~7–20 s) |

`rag_total_ms` (first→last RAG token, or wait time when none arrives) is the
one instrumentation addition — the tail was previously invisible next to
first-token `rag_ms`. Fast-path turns now correctly report ~0.

## 3. Transcription accuracy (reference set, exact-match)

File audio 8/8: clean/quiet-0.25x/slowed/leading-silence/repeated hello,
mid-fragment, AWS technical question, `Chidera`+`Alimosho` names — all exact,
including proper nouns. Real reporter mic: clean→correct; degraded→rejected
or marginal (separability limit, §1.3). Origins: repo fixtures are
synthetic/clean; `nimoh.wav` origin unverified — **no accent-validation claim
made**; reporter recordings are real consented mic audio with stated "Hello".

## 4. Playback findings (which of the 4 cases)

1. Generated audio cracked? **No** — TTS direct output healthy (RMS 4861,
   98% nonzero, correct 24 kHz).
2. Clean audio but browser stutters? **Yes under load** — 161 ms median
   inter-source gaps vs 80 ms chunks (server emission cadence, not browser
   scheduling: 8 ms overhead, ordered, bounded queue, interruptible).
3. Starts too late? **Yes** — RAG-bound (see §2), not buffering (incremental
   path proven by test + first-audio-before-stream-end).
4. Inherently low quality? Unmeasured (no listening panel); clone verified active.

## 5. Files changed and why

- `james-voice-service/app/pipeline.py` — `TurnTimings.rag_total_ms` + capture
  at last-token/wait (the missing bottleneck metric); comment updates.
- `james-voice-service/app/main.py` — emit `rag_total_ms` in `turn_timing`.
- `james-voice-service/tests/test_pipeline_streaming.py` — advancing-clock
  test that RAG duration covers the stream (`rag_total ≥ rag_ms`).
- Nothing else changed (portfolio untouched this round).

## 6. Tests executed, exact outcomes

- Voice suite **163/163** (was 162; +1 new). Portfolio 607 pass + 2
  pre-existing unrelated (`cv-detailed` missing `/tmp` fixture; `agent-sdr`
  #8). `tsc` clean. `next build` blocked by sandbox Google-Fonts fetch
  (environmental, pre-existing; succeeded repeatedly with warm font cache).
- Live: hello + q-aws complete with full stage logs; browser gap run
  (recv→play 8 ms, p50 gap 161 ms loaded); UI speaking+interrupt re-verified;
  memory 1.28 GiB container / 1.38 GiB VmRSS warm idle (bounded, consistent).

## 7. Upstream/hardware limits (unchanged, still out of scope)

RAG first-token/tail latency (portfolio/LLM side); TTS RTF under single-CPU
contention; 2 GB host NOT approved (DEPLOY gate); ARM unmeasured; mic quality
dominates accuracy; no duplicate transcription/RAG calls exist (single
`run_turn` per utterance, verified by code + tests).

## 8. Safe for controlled testing vs production

Safe for controlled local testing (all green paths verified end to end).
**Not production**: 2 GB gate unmet, ARM unmeasured, RAG latency unloved.

## 9. Unresolved / next highest-value

1. RAG tail latency (portfolio side; voice already streams incrementally).
2. TTS throughput under contention (host capacity, not code).
3. Real-mic confirmation of audibility by the reporter (final acceptance).
4. Google-Fonts build fetch in offline sandboxes (environmental).
