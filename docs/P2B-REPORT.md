# P2B — Real-Time Voice Integration: Local Verification Report

Scope: local integration/validation only. No Oracle deploy, no DNS/proxy/public
changes, no commits. Two repos kept separate:
`Personal website` (Next.js portfolio) and `james-voice-service` (Docker).

Status labels: **PASS** = directly verified · **FAIL** = tested and failed ·
**NOT TESTED** = not executed / insufficient evidence.

---

## 1. Integration status — what existed, what was missing

Existing and working: the voice service skeleton (FastAPI WS + REST sessions,
HMAC tickets, SQLite quotas, protocol, STT/TTS/VAD adapters, RAG client), the
portfolio text chat + authenticated `/api/rag` SSE, and a `TalkToJames`
component behind `NEXT_PUBLIC_VOICE_ENABLED`.

Missing / defective (found by running it, not by reading tests):

| # | Defect | Where | Symptom |
|---|---|---|---|
| 1 | Browser never sent `end_utterance` — no end-of-speech signal | `components/TalkToJames.tsx` | Turn never started; assistant never replied |
| 2 | Mic source never connected to the AudioWorklet, and the worklet never connected to a destination | `TalkToJames.tsx` | `process()` never ran → zero audio frames |
| 3 | Worklet used `btoa`, which is **undefined in `AudioWorkletGlobalScope`** | `TalkToJames.tsx` | Worklet threw on first frame and stopped |
| 4 | Silero VAD crashed on the trailing partial 512-sample window | `app/vad.py` | Every utterance whose length wasn't a multiple of 512 → `stt_failed` |
| 5 | `turn_end` swallowed for **both errors and successes** (state reset to idle before the liveness guard) | `app/main.py` | Client hung forever / stuck "speaking" |
| 6 | Local allowed origin was `:3001` but the browser runs on `:3000` | `docker-compose.local.yml` | CORS would block the browser session call |

All six are fixed.

## 2. UI status

Component: `components/TalkToJames.tsx`, rendered from `app/layout.tsx` only
when `process.env.NEXT_PUBLIC_VOICE_ENABLED === "1"` (default off). Base URL
from `NEXT_PUBLIC_VOICE_BASE_URL`; the WebSocket URL always comes from the
session response. Verified in a real server-rendered page that the control is
present when enabled. There is no dedicated route — the control renders in the
site body (bottom of the page) on every page via the root layout.

Enable locally: start the portfolio with
`NEXT_PUBLIC_VOICE_ENABLED=1 NEXT_PUBLIC_VOICE_BASE_URL=http://localhost:8000`.

## 3. Runtime architecture (as verified)

```
Chromium (getUserMedia + AudioWorklet capture, 16 kHz PCM16)
  ──ws://localhost:8000/v1/voice──▶  voice service (Docker, :8000)
      hello(ticket) → STT (faster-whisper small.en)
      audio_frame… → end_utterance → rag_stream
  voice service ──https POST host.docker.internal:3000/api/rag──▶ portfolio RAG
      (Bearer RAG_SERVICE_TOKEN) ← SSE token/sources/done streams back
  voice service: bounded SegmentationBuffer → Pocket TTS → audio_frame PCM16 24 kHz
  ◀──ws── browser WebAudio queue plays in order; interrupt cancels the turn
```

CLIENTS → server messages verified: `hello`, `audio_frame`, `end_utterance`,
`interrupt`, `end_session`. Server → browser: `session_created`, `listening`,
`transcript_final`, `rag_started`, `audio_frame`, `turn_end`, `session_ended`.

## 4. Files changed

Portfolio (`Personal website`):
- `components/TalkToJames.tsx` — client endpointing + send `end_utterance`;
  connect mic→worklet→sink→destination; transfer raw PCM (no `btoa` in worklet);
  `AudioContext.resume()`; reset endpoint state on turn boundaries.
- `lib/rag/voice.ts` — pure `shouldFinalizeUtterance()` endpointing rule + `ENDPOINT_SILENCE_MS`.
- `tests/voice.test.ts` — 7 endpointing regression tests.
- `pwtest-local/p2b-e2e.mjs`, `browser-voice.mjs`, `browser-conversations.mjs` — probes (untracked scratch).
- (pre-existing, not authored here: `app/api/rag/route.ts` service auth, `lib/env.ts`, `lib/ratelimit.ts`, `app/layout.tsx`, `package.json`.)

Voice service (`james-voice-service`):
- `app/vad.py` — `window_pad()` + pad Silero input to whole 512-sample windows.
- `app/main.py` — `turn_end_reason()` drives `turn_end` from the result, never liveness.
- `app/pipeline.py` — `turn_end_reason()` helper; pure first-segment TTS timing.
- `tests/test_vad.py`, `tests/test_pipeline.py` — regression tests for both.
- `docker-compose.local.yml` — correct local allowed origin (`:3000`,`:3001`).
- (pre-existing from P2A: `app/segmentation.py`, `app/rag_client.py` streaming.)

## 5. Tests executed

| Command | Result |
|---|---|
| `james-voice-service`: `python3 -m unittest discover -s tests` | **PASS** 108/108 |
| voice `py_compile app/*.py tests/*.py` | **PASS** |
| portfolio `npx tsc --noEmit` | **PASS** |
| portfolio `npx eslint` (changed files) | **PASS** |
| portfolio `npx vitest run` | 574 passed, 1 failed, 2 files failed |
| `pwtest-local/p2b-e2e.mjs` (real STT/RAG/TTS over WS, 3 turns + barge-in) | **PASS** 0 failures |
| `pwtest-local/browser-voice.mjs` (Chromium, synthetic mic, 1 turn) | **PASS** 0 failures |
| `pwtest-local/browser-conversations.mjs` (3 turns + interruption) | **PASS** 0 failures |

Pre-existing failures (unrelated, confirmed previously via `git stash`):
`cv-detailed.test.ts` (missing `/tmp/james_cv_detailed.txt`) and
`agent-sdr.test.ts` #8 (PostgreSQL 15 full-text `ai`→`ai-assisted` match).

## 6. End-to-end evidence

Headless protocol E2E (`p2b-e2e.mjs`), real models, one session:
transcripts correct; every turn ended `complete`; interruption stop latency
**0.02 s** with **0 stale audio frames**; a turn after interruption also
completed. The spoken answers were transcribed back from the returned PCM and
are unmistakably grounded in the corpus — e.g. TaxBloc "on containerized AWS …
Terraform layers", TriBorrow "production RAG … semantic search with cosine
similarity", Agent SDR "PostgreSQL through Prisma … around 40 models".

Browser E2E (`browser-conversations.mjs`, Chromium with a synthetic microphone
= question+silence WAV), **3 conversations + 2 interruptions**: 80 mic frames
captured, 4 `end_utterance` sent by client endpointing, 3+ distinct questions
transcribed, 15 synthesized audio frames received, ≥3 turns `complete`,
2 turns `interrupted` server-side. This exercises real `getUserMedia`,
`AudioWorklet`, WebSocket, and the real STT→authenticated RAG→TTS backend.

**Limitation (honest):** the microphone is a synthetic device (a WAV file), not
a human speaking into a real mic, and the audio **output was not auditioned** —
this environment cannot listen. Browser automation supplied the browser APIs;
a human still needs to place one real spoken call to judge voice quality.

## 7. Performance (measured)

Hardware: macOS 13.7.8, **Intel x86_64**, 8 cores, 16 GB RAM. Docker VM 8 GB;
voice container `cpus: 2`, `mem_limit: 10g`; `OMP_NUM_THREADS=2`; Pocket TTS
`torch.set_num_threads(1)`. **Host load average was 18–30 (8 cores) during all
runs** — heavily oversubscribed, which dominates these numbers. LLM is remote
(portfolio RAG → OpenRouter).

Warm per-stage (in-container `run_turn`):

| Stage | q-aws | q-tax | q-third |
|---|---|---|---|
| STT (end→transcript) | 36.8 s* / 5.4 s(WS) | 8.2 s | 4.3 s |
| RAG first token | 4.7 s | 9.5 s | 7.1 s |
| TTS first audio | 15–26 s | — | — |
| End-of-speech→first audio (WS) | 12.4 s | 279 s† | 13.6 s |
| Total turn (WS) | 25.3 s | 294 s† | 114 s |

\* first call after load. † Anomalous stall under host contention; server logs
show `turn_complete` with no error (no retry/failure).
Cold model load (first turn): STT ~21 s, TTS ~27 s, VAD ~4 s.
Resources: container RSS ~1.2–2.2 GB loaded; CPU modest between turns.

**Target (≤2 s median, ≤4 s p95) → FAIL** on this host: first audible audio was
12–30 s typical, with a 279 s outlier. Contributing factors, all environmental:
2-vCPU container, host load ~3× cores, single-threaded TTS, and a remote LLM.
Interruption→stop latency **0.02 s = PASS**.

## 8. Remaining issues / risks

- **Latency far above target on this hardware** (see §7). Not caused by the
  integration logic; needs a dedicated/less-contended CPU and re-measurement.
- **STT model `small.en` is slow relative to real-time here**; `base.en` is the
  obvious next measurement (not changed — model selection is a deliberate choice).
- **No true voice cloning** and **no human listening evaluation** (Pocket TTS
  cloning weights are gated; not evaluated).
- **Minor unresolved page error** in the Next dev console
  (`appendChild … Unexpected token '}'`) — a dev-overlay/HMR artifact; it did
  not affect the voice flow in any run (all browser checks pass with it present).
  NOT TESTED in a production build.
- Session/attempt limits were observed working (a burst of sessions returned
  **429 attempt-budget**); the automated E2E temporarily raised only the
  per-session duration and reset local SQLite state, then the override was
  reverted. Daily/active-time limits are covered by the service suite.
- Not covered here: real reconnect-after-drop UX, quota-exhaustion UX,
  microphone-permission-denied UI (unit-tested in `talk-to-james.test.tsx`),
  and load/soak behavior.

## 9. Exact local startup (secrets redacted)

```bash
# 1) Portfolio (host) — enables the voice UI; text chat is unaffected.
cd "/Users/user/Desktop/iDENVA/Personal website"
NEXT_PUBLIC_VOICE_ENABLED=1 \
NEXT_PUBLIC_VOICE_BASE_URL=http://localhost:8000 \
npm run dev                       # http://localhost:3000

# 2) Voice service (Docker) — base + local overlay gives host-gateway + port.
cd "/Users/user/Desktop/iDENVA/james-voice-service"
# .env (gitignored) must define, with the portfolio's values:
#   VOICE_RAG_API_URL=http://host.docker.internal:3000/api/rag
#   VOICE_RAG_SERVICE_TOKEN=<same as portfolio RAG_SERVICE_TOKEN>
#   VOICE_HMAC_SECRET=<32+ random bytes>
#   VOICE_PUBLIC_BASE_URL=http://localhost:8000
#   VOICE_ALLOWED_ORIGINS=http://localhost:3000
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build voice
curl -s http://localhost:8000/healthz   # {"status":"ok","db":true}

# 3) Reproduce the browser E2E (needs Chromium via pwtest-local + ffmpeg/say)
cd "/Users/user/Desktop/iDENVA/Personal website/pwtest-local"
HEADED=1 node browser-conversations.mjs
```

Portfolio `.env` keeps `DATABASE_URL` (Neon) + LLM keys + `RAG_SERVICE_TOKEN`;
the voice service never sees the DB, corpus, or filesystem.
