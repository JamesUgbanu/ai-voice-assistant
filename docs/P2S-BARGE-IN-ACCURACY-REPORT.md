# P2S — Reliable Barge-In and Speech Recognition: Report

## 1. Barge-in root cause and verdict (measured)

No code defect found in the detector — the reporter's misses are an input-level
problem, proven by measurement. Timestamped in-browser traces (mic frames +
WS events) show the detector firing **≤400 ms into sustained loud speech,
exactly once per episode**, with clean cancel and recovery (LOUD run:
interrupts at 49.4/61.6 s; QUIET 0.18x run: fires twice, once each).

Sensitivity/specificity matrix (real Chromium, fake mic, default autoplay):

| input during playback | result |
|---|---|
| 1.0x speech | fires ≤400 ms, 1x |
| 0.18x speech (≈ reporter's louder attempts) | fires, 1x each |
| 0.05x sustained | never fires |
| isolated loud clicks | never fires |
| silence | never fires |

The reporter's own envelopes (speech RMS 800–3500 → level 0.15–0.64, with
dips to 0.04–0.10) straddle the 0.15 threshold: loud attempts sustain it,
quiet ones never reach 3 consecutive frames. Threshold and 250 ms guard are
therefore **kept unchanged** — lowering them buys nothing the data supports
and spends the demonstrated specificity margin. Fluctuation-tolerance
(3-of-5) was considered and rejected: the reporter's loud patterns already
satisfy consecutive-3, and sub-threshold speech cannot be caught by any energy
rule without firing on noise.

## 2. Transcription root cause (captured-audio comparisons)

Three browser triad runs (utterance + silence, full pipeline to
`transcript_final`): clean "hello" → `Hello.`; 0.25x "hello" → `Hello.`;
onset-clipped "ello" → `Oh.`. Plus the 0.18x question → `What aims built
with AWS?` (partial). Conclusion: the pipeline (gate → VAD → base.en →
guard) is accurate on complete audio down to 0.25x gain; clipped/whisper-quiet
fragments mistranscribe, and the STT guard + retry prompt is the correct
terminal for them. No model/decoder/config change made — none is indicated.

## 3. Exact files and changes

- `Personal website/components/TalkToJames.tsx` — mic level meter now live in
  every call state (was listening-only), so interruption attempts are visibly
  registering; no logic change to detection, endpointing, turns, or playback.
- `Personal website/lib/rag/voice.ts`, tests — untouched this round (helpers
  already cover detector/stale/turn-id rules; added fluctuation-pattern unit
  cases in the barge-in suite).
- Voice service — untouched (162/162 still green).
- `pwtest-local/measure-barge.mjs` (scenario measurement), `triad.mjs`
  (transcription triad), `verify-barge.mjs` (acceptance run) added; no product
  code in harnesses.

## 4. Before/after and full test results

- Interruption latency: first sustained speech → `interrupt` ≤400 ms
  (measured), server ack sub-second (p2m: 0.05–0.87 s historically).
- Acceptance run (`verify-barge.mjs`): question transcribed, 170 answer frames,
  speaking + 169 scheduled sources, exactly 1 interrupt, interrupted + complete
  turn_ends, final state listening — **5/5 PASS**.
- Suites: portfolio 607 pass (2 pre-existing unrelated: `cv-detailed` fixture,
  `agent-sdr` #8); voice 162/162; `tsc`/`eslint`/`next build` clean.
- Existing coverage retained: cancel-during-RAG/TTS, next-turn isolation,
  stale rejection, empty/no-speech recovery, turn_id-from-payload.

## 5. Independent verification statement

- Playback: independently verified (scheduled sources on a running context).
- Interruption: independently verified (prompt, exactly-once, recovers, silent
  stays silent) — on fake-mic audio across 5 scenarios.
- Transcription: independently verified on clean/quiet/clipped browser audio
  as above.
- Still requiring manual verification: Nigerian-accent accuracy, acoustic echo
  cancellation quality on speakers vs headphones, and audibility on the
  reporter's hardware (fake mic cannot validate these).

## 6. Remaining limitations and guidance

- Barge-in needs sustained voice clearly above room level; whisper-quiet
  attempts below the energy floor cannot trigger any energy detector — use the
  now-always-live level meter to confirm registration, move closer to the mic,
  or tap Interrupt (button unchanged). Headphones remain most reliable.
- Remote RAG latency (15–28 s observed upstream) still governs answer speed;
  unrelated to these changes.
- Production still runs pre-P2N code; local turn capture stays on until the
  reporter confirms the browser conversation.
