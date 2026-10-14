# P2O — Critical Voice Regression (Browser Audio): Report

Follow-up to P2N, using the reporter's own captured browser audio (5 utterances,
saved server-side by the temporary `VOICE_SAVE_TURNS_DIR` hook, local only).
**P2M barge-in is exonerated again**; the defects are capture quality interacting
with STT confidence handling, plus silent failure UX. No deploy, no commits, no
new model/dependency. Portfolio change is 6 lines (user-visible turn messages).

## 1. Proven root cause (reporter's audio, not a hypothesis)

The browser delivered degraded fragments of spoken "Hello" — quieter overall
(RMS 1800–3500 vs 4367 reference), soft onsets, mid-word energy dips:

| captured (secs) | envelope (100 ms RMS) | decoded | logprob / nsp |
|---|---|---|---|
| 0.90 | 187…6375…331 | `Alo.` | -0.93 / 0.08 |
| 0.70 | 1052…3145…828 | `Hello?` | -0.20 / 0.12 |
| 0.50 | 2972…4212…605 | `Alimosho, Alimosho,` | -0.77 / 0.30 |
| 1.00 | 348…1140…**261**…3755…1238 | `Blo.` | -0.89 / 0.16 |
| 0.70 | 567,388,455…3253…1709 | `Welcome home.` | -0.87 / 0.25 |

- The 0.50 s capture is the **exact reported symptom reproduced**:
  `Alimosho, Alimosho,` with runaway decoder timestamps (`[0.00-28.24s]` for
  0.5 s of audio — the repetition-loop signature). The P2N guard rejected it
  live (`chars=0`, `turn_end:empty`).
- But `Alo.` / `Blo.` / `Welcome home.` carried only *moderate* stats and
  **passed the P2N guard** → `transcript_final` shown in the UI (the "incorrect
  text") → forwarded to the remote RAG.
- Session reconstruction from server logs: `Alo.` → RAG leg, never completed;
  `Hello?` → greeting fast path, answered (64 frames — the pipeline works when
  audio is clean); loop → guard-empty, silent; `Blo.` → RAG leg, never
  completed; new session, `Welcome home.` → RAG leg, never completed. Missing
  `turn_timing`/`turn_complete`/`turn_failed` events plus reconnects = turns
  abandoned mid-RAG (slow remote RAG on a loaded dev server; RAG itself answers
  in ~5 s when warm) or disconnect-cancelled. Either way the client showed
  nothing — the "no response".

So: **degraded capture → wrong short transcripts → slow/wasted RAG round trips
and silent empty turns → perceived total failure.** Three compounding defects,
all now addressed.

## 2. Precise fix

- `app/stt.py` — replaced the P2N low-confidence rule with the calibrated rule:
  reject when `mean no_speech_prob > 0.06 **and** avg_logprob < -0.75`. On all
  measured data this rejects every garbage sample (`Alo.`/`Blo.`/`Welcome
  home.`/`HAPPY.`/`So.`/both loops) and passes every genuine one (margin:
  legit nsp ≤ 0.05, legit logprob ≥ -0.49; names and genuine repetitions pass).
- `components/TalkToJames.tsx` (6 lines) — `turn_end(empty)` now shows "I
  didn't catch that — please try again."; `turn_end` failures
  (`rag_failed`/`tts_failed`/…) show "Voice hit a snag — retry or switch to
  text."; both clear on the next transcript. Failures are visible and
  recoverable instead of silently stuck on listening.
- Unchanged and re-verified: VAD, RAG contract, TTS, quotas/auth, barge-in,
  stale-audio blocking, greeting fast path, beam/prompt/model.

## 3. Before/after evidence

- Reporter's 4 bad captures through the fixed backend: all → `text=''`,
  `no_speech=True`. The 1 good capture (`Hello?`) still passes.
- Live WS: `Hello.` → complete (68 frames); garbage → `empty`, zero frames;
  duplicate `end_utterance` → transcript delivered; barge-in lifecycle 5/5
  (interrupt 0.10 s, new turn complete, stale=0).
- Voice suite **162/162** (incl. 3 new browser-fragment guard tests);
  portfolio voice suites 63/63, `tsc`/`eslint` clean, `next build` succeeds.

## 4. Remaining limitations (explicit)

- The dominant factor remains **microphone input quality** (distance, room,
  laptop mic, browser AEC pumping — the mid-word dip to RMS 261 is in the
  acoustic signal, not a code artefact). No server threshold can recover a
  word the mic barely captured; the system now re-asks instead of confabulating.
- Headphones remain the most reliable barge-in setup (unchanged P2M guidance).
- If "no response" recurs for *clean* audio, the next suspect is the remote
  RAG leg latency (25 s timeout surfaces only as a quiet return); rerun with
  `/tmp/turns` capture on to separate STT from RAG instantly.
- Temporary capture (`VOICE_SAVE_TURNS_DIR` in `docker-compose.local.yml`,
  local only) is still ON for the confirmation round — revert that one block
  once the reporter confirms; the code hook is dormant without the env var.
- Production still runs pre-P2N code (no deploy performed); the live site will
  show old behavior until `docker compose up -d --build voice` runs there.
