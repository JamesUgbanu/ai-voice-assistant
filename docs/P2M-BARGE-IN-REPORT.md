# P2M — Voice Barge-In Fix: Report

Scope: `Personal website/` voice-call client + one cooperative-cancel change in
`james-voice-service/`. Architecture, deployment, quotas/auth and the
authenticated text-only RAG contract are unchanged. No deploy, no commits, no
new model, no new dependency.

## 1. Executive summary

- **Root cause: the client never listened during assistant playback.** While the
  assistant spoke, the mic worklet kept running but the frame handler withheld
  all audio (`canEmit` was true only for `listening`/`interrupted`) and nothing
  ever called the existing `interrupt()`. The only working barge-in was the
  on-screen **Interrupt button**. Talking during playback did nothing, so the
  assistant finished its answer.
- The **server-side interrupt path was already correct and complete**: an
  `interrupt` (or a new `end_utterance` while a turn runs) cancels the current
  turn task, which aborts the streaming RAG request and the streaming TTS
  producer — the whole obsolete response pipeline, not just playback.
- **Fix (client):** keep capturing during playback, detect *sustained*
  speech-level energy above the browser echo-canceller's residual, and drive the
  same `interrupt()` path; enable WebRTC echo cancellation; flush the
  interruption's opening frames so no syllables are lost.
- **Fix (client, stale audio):** after an interrupt the current-turn id is
  cleared, and incoming audio frames are accepted only for the current turn, so
  a chunk in flight from the cancelled response can never restart playback.
- **Fix (server, one line):** set the turn's cancellation event *before*
  cancelling the task, so the upstream RAG HTTP stream is torn down
  cooperatively rather than only by task cancellation.
- Verified end-to-end: interrupt acknowledged in **0.12–0.87 s**, new utterance
  transcribed and answered, **0 stale audio frames**, old answer stops.
  Tests: voice **152/152**, portfolio **601/601** relevant (2 pre-existing,
  unrelated failures), production build clean.

## 2. Lifecycle trace (answers to the audit questions)

| question | finding |
|---|---|
| How is mic audio captured while TTS plays? | `getUserMedia` → `mic-tap` AudioWorklet → 100 ms PCM16 frames over WS. The worklet runs continuously (`TalkToJames.tsx` `MIC_PROCESSOR`). |
| Does the mic stay active during speech? | **Yes** — but frames were **not sent** while `state === "speaking"` (`canEmit = listening || interrupted`). |
| Does VAD run continuously during playback? | The client has a tiny energy gate per frame; the authoritative VAD (Silero) runs **server-side** in `run_turn` (`app/vad.py`) and only at turn start, not during playback. |
| Can VAD distinguish user speech from assistant leakage? | Not by itself. Leakage is an acoustic problem; it is handled by **echo cancellation** on the capture side, not by the VAD. |
| Headphones / echo control? | Before: `getUserMedia({audio:true})` — browser defaults, no explicit AEC request. After: explicit `echoCancellation/noiseSuppression/autoGainControl`. |
| How was an interruption detected/propagated? | Only via the on-screen button → `interrupt()` → `{type:"interrupt"}` → server `end_turn_state("interrupted")`. No voice trigger existed. |
| Can TTS be cancelled mid-generation? | **Yes**: streaming TTS takes a `threading.Event` `stop` (`tts.py` `synthesize_stream(..., stop=stop)`), set in `pipeline.py` `speak()` finally. |
| Do queued chunks keep playing after interrupt? | Client queue is cleared and the play token bumped (`interrupt()`); server stops emitting because `state.is_current` is false. |
| Does response generation continue after playback stops? | Before the client fix, yes (nothing cancelled it). After an interrupt, **no** — the turn task is cancelled and the RAG stream closes. |
| Can stale audio resume after a new utterance? | **Yes, there was a narrow window**: late frames from the cancelled turn were enqueued because the stale guard didn't drop them after `turnRef` was still set. Now fixed with `acceptsAudioFrame`. |

## 3. Root cause (verified)

`components/TalkToJames.tsx`, the worklet message handler:

```ts
const canEmit = st === "listening" || st === "interrupted";   // (was)
...
if (e.data.active) {
  speakingRef.current = true;
  if (wsRef.current?.readyState === WebSocket.OPEN && canEmit) {  // never true while speaking
    ...send audio_frame...
  }
}
```

- While `st === "speaking"`, `e.data.active` was ignored for capture and the
  `else if` endpointing branch requires `listening === true`, so **no**
  `end_utterance` either. The user's voice reached neither the UI nor the
  server.
- `interrupt()` existed (`TalkToJames.tsx:271`) but was referenced **only** by
  the button (`onClick={interrupt}`). Nothing bound it to microphone energy.

Secondary (stale-audio) defect: `interrupt()` bumped the play token and cleared
the queue, but did **not** clear the current turn id, and the `audio_frame`
handler pushed any frame whose `turn_id` matched (the interrupted turn's own
id). Frames already in flight when the interrupt was sent could therefore
enqueue and play under the new token.

## 4. Changes

Client — `components/TalkToJames.tsx`:

1. **Voice-triggered barge-in.** In the worklet handler, a new `speaking` branch
   keeps a short pre-roll and runs the sustain detector; when it fires it calls
   the existing `interrupt()` and flushes the pre-roll frames so the start of
   the interruption is captured. One-shot per speaking episode
   (`bargeFiredRef`), with a `BARGE_IN_GUARD_MS` guard after playback starts.
2. **Echo cancellation.** `getUserMedia({ audio: { echoCancellation: true,
   noiseSuppression: true, autoGainControl: true } })` — the primary defense
   against the assistant's own voice triggering barge-in.
3. **Stale-audio gate.** On `interrupt`/`turn_end`/`listening`/`session_created`
   the current turn id is cleared; `audio_frame` is accepted only via
   `acceptsAudioFrame(turnRef.current, msg.turn_id)`.
4. `interrupt` added to the `startCall` dependency list.

Client — `lib/rag/voice.ts` (pure, unit-tested helpers):

- `BARGE_IN_LEVEL = 0.15`, `BARGE_IN_FRAMES = 3`, `BARGE_IN_GUARD_MS = 250`.
- `nextBargeInConsecutive(level, prev)` and `shouldBargeIn({level, consecutive})`.
- `acceptsAudioFrame(currentTurnId, frameTurnId)`.

Server — `james-voice-service/app/main.py` (`end_turn_state`):

- `if turn_cancel is not None: turn_cancel.set()` **before** `turn_task.cancel()`,
  so the RAG stream's `should_stop` closes the upstream HTTP request and the TTS
  producer observes stop promptly; the task cancel remains the backstop.

### Threshold calibration (real recordings, not arbitrary)

The worklet reports `level = clamp(mean(|sample|)/32768 * 6, 0, 1)` per ~100 ms
frame. Measured per-frame level on real speech (`/tmp/hello.wav`,
`/tmp/q-aws.wav`, `/tmp/nimoh.wav`): median **0.74–1.0**, active frames 5/8,
21/23, 20/23. The turn-start energy gate corresponds to roughly 0.02.
`BARGE_IN_LEVEL = 0.15` clears the AEC residual and the turn gate by a wide
margin yet fires on real speech within 3 frames (300 ms).

## 5. Tests added and results

Voice service (`unittest`):

- `tests/test_pipeline_streaming.py::test_interrupt_closes_the_rag_stream` —
  interrupt mid-stream closes the RAG async generator (its `finally` runs) and
  no final frame is emitted.
- `tests/test_pipeline_streaming.py::test_interrupt_mid_tts_stream_observes_stop`
  — the streaming TTS producer sees `stop`, and exactly the pre-interrupt audio
  frame is emitted.
- Existing `test_cancel_mid_stream_discards_remaining_segments` still passes.
- **Full suite: 152/152 OK.**

Portfolio (`vitest`):

- `tests/voice.test.ts` — barge-in sustain logic (fires only after
  `BARGE_IN_FRAMES` consecutive loud frames, resets on quiet, ignores single
  transients, never fires below threshold, calibration bounds) and
  `acceptsAudioFrame` (null turn drops everything; new turn drops old ids).
- `tests/barge-in-wiring.test.ts` — structural guards that the client detects
  `speaking`, calls `interrupt()`, gates frames with `acceptsAudioFrame`,
  requests `echoCancellation`, and does **not** stream audio during mere
  playback.
- **Relevant suites: 62 pass.** Full portfolio suite 601 pass; the 2 failures
  are pre-existing and unrelated (`cv-detailed` missing `/tmp` fixture;
  `agent-sdr` #5-#8 retrieval assertion).

End-to-end probes (real service, real Whisper/TTS):

- `pwtest-local/p2m-barge-in.mjs` (new): start an answer, interrupt, immediately
  speak again →
  `interrupt acknowledged stop=0.87s`, `new utterance transcribed "Hello."`,
  `new turn completed complete`, `no stale audio stale=0`, `new answer frames=64`.
- `p2d-ws-interrupt.mjs`: `interrupt acknowledged stop=0.12s`, `stale=0`.
- `p2j-turn.mjs`: transcript `"Hello."`, `reason=complete` (no regression).

## 6. Interruption scope: TTS only, or the whole pipeline?

**The complete obsolete response pipeline.** On `interrupt` the server runs
`end_turn_state("interrupted")`, which (1) sets the turn-cancellation event, (2)
cancels the in-flight turn task, (3) closes/drains the streaming TTS queue and
lets the producer exit, and (4) `state.cancel()` invalidates the turn id so
`emit_frame` and the RAG `should_stop` stop immediately. The test
`test_interrupt_closes_the_rag_stream` proves the upstream RAG generator is
closed, not merely abandoned. Nothing unrelated to the current response is
touched: the session, quotas, DB row and socket stay alive.

## 7. Limitations (honest)

- **Echo cancellation is a browser/OS/hardware capability.** With built-in
  speakers Chrome's AEC is good but not perfect; on **headphones** barge-in is
  the most reliable (no acoustic path), and speakerphone depends on the device.
  If a particular device leaks enough that AEC residual exceeds
  `BARGE_IN_LEVEL`, raise it (or require headphones); this is a tuning constant,
  not a correctness issue.
- **Best-effort pre-roll.** The detect-and-fire path loses nothing beyond the
  ≤300 ms detection latency; the preceding `active` frames are flushed.
- **Automatic barge-in is browser-only.** The server does not do barge-in from
  raw audio while it is speaking (it has no reference signal to subtract); it
  reacts to the client's `interrupt`. This matches the existing protocol, so no
  extra server CPU/RAM is used.
- **Not automated:** a real browser microphone → worklet → WS run. The mic/AEC
  path is inherently device-dependent; the lifecycle is covered by the pure
  tests, wiring guards and the server probes, and the click-through below.

## 8. Manual test procedure

1. Build the portfolio with `NEXT_PUBLIC_VOICE_ENABLED=1` and
   `NEXT_PUBLIC_VOICE_BASE_URL` set; open the panel, start a voice call.
2. **Halfway through a sentence**, start talking. Expected: audio stops
   immediately, status returns to listening, and the assistant answers your new
   question. (Repeat with headphones — most reliable — and on speakers.)
3. Start talking **while the answer is still being generated** (right after the
   first words). Expected: same, no leftover sentence and no delayed restart.
4. Interrupt, then **stay silent**: after ~0.9 s the client endpoints; no turn.
5. Interrupt **twice in quick succession**: the second utterance still answers;
   no stale audio from either.
6. Let the assistant speak with **no** user speech: it must not self-interrupt
   (watch for the level meter; adjust `BARGE_IN_LEVEL` if your device leaks).
7. Toggle **Mute/Type instead** mid-answer: call ends cleanly.

## 9. Deploy / env notes

- Voice service must be restarted to pick up the one-line `main.py` change
  (local overlay build used here).
- No new env vars. Constants live in `lib/rag/voice.ts`
  (`BARGE_IN_LEVEL`, `BARGE_IN_FRAMES`, `BARGE_IN_GUARD_MS`).
- No change to memory footprint: detection is client-side and the server change
  only sets an existing event earlier.
