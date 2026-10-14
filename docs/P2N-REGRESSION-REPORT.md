# P2N — Voice Regression Investigation: Report

Scope: `james-voice-service/` STT guard + turn-robustness fix, with live
protocol-level proof. **No portfolio source file was changed; the P2M barge-in
implementation is exonerated by evidence** (details §2). No deploy, no commits,
no new model, no new dependency, no config change.

Symptoms: (a) simple speech such as "Hello" transcribed as "Alimosho,
Alimosho"; (b) sometimes the assistant does not respond at all.

## 1. Proven root causes (two, both server-side, neither from P2M)

### R1 — Decoder hallucinations reach RAG/UI with no confidence gate (causes a)

`FasterWhisperBackend.transcribe()` (`app/stt.py`) accepted **any** non-empty
decoder output: `no_speech` was set only for empty text, and per-segment
`no_speech_prob` was never inspected. On short/ambiguous audio the greedy
decoder (beam 1, temp 0, domain `initial_prompt` containing "…Chidera,
**Alimosho**, Lagos…") latches onto a token and loops — the exact reported
shape. Reproduced live on the real `base.en` model:

| input | output (before fix) | logprob / no_speech_prob |
|---|---|---|
| 0.12 s vowel fragment | `So.` | -1.73 / 0.59 |
| 0.10 s onset fragment | `HAPPY.` | -1.31 / 0.29 |
| ambiguous fragment, beam 5 | `No. No. No. No. No.` | -1.01 |
| 2×-speed "Hello" | `Come on.` | -0.43 / 0.23 |

The `No. No. No. No. No.` case is the repetition-loop class of the reported
`Alimosho, Alimosho` (the latched token follows the input acoustics and the
prompt vocabulary). Genuine utterances score far better (`Hello.` -0.18/0.03,
`Hello. Hello.` -0.25/0.04, `…Chidera and Alimosho.` -0.32/0.00, AWS question
-0.39/0.00), so the class is separable without touching the model, prompt,
beam size, or proper-noun accuracy.

### R2 — A stray duplicate `end_utterance` murders the in-flight turn (causes b)

`app/main.py` `end_utterance` handler captured the buffer, then — if a turn
task was running — cancelled it and started a new turn **without checking
whether any new audio arrived**. Proven live with `pwtest-local/p2n-double-eou.mjs`
(valid "Hello" frames + `end_utterance`, then a duplicate `end_utterance` 100 ms
later with zero new audio):

- before: `["turn_end:interrupted", "turn_end:empty"]` — transcript never
  delivered, user sees nothing ("does not respond at all");
- after: `["transcript_final:\"Hello.\"", "turn_end:complete"]`.

## 2. P2M barge-in: investigated and cleared

Each §3 failure mode was checked against code and runtime:

- Speaking branch withholds audio until barge-in fires; nothing is sent twice
  (pre-roll sliced, cleared, one-shot `bargeFiredRef`, sent in order after the
  `interrupt` message, so the server observes cancel-before-audio). Normal
  `listening` capture path is byte-identical to before.
- `bargeFiredRef`/guard reset on every non-speaking frame and on each new
  speaking episode (`playNext`); rapid re-barge works.
- `interrupt()` nulls only the obsolete turn id; the next turn re-establishes
  it via `transcript_final`. `acceptsAudioFrame` drops late chunks without
  affecting the current turn (unit-tested; `p2m` stale=0).
- `turn_cancel` is a fresh `asyncio.Event` per utterance (`run_utterance`);
  `end_turn_state` nulls the old one — a cancelled turn cannot poison the next
  (`p2m`: interrupt → new turn transcribed, completed, 58 answer frames).
- Message order `interrupt → turn_end(interrupted) → listening → new turn` is
  what the client implements and what the probes observe.
- Single-turn transcription was verified across 15+ audio variants (short,
  quiet, slowed, leading silence, noise, DC, clipping, tones): every genuine
  utterance transcribes correctly, so P2M changed nothing about recognition.

## 3. Changes (exact files)

- `james-voice-service/app/stt.py` — new pure `is_hallucinated_transcript()`
  plus thresholds; `FasterWhisperBackend.transcribe()` returns
  `no_speech=True` (empty text) when mean `no_speech_prob > 0.5`, or
  (`avg_logprob < -1.0` and `mean_nsp > 0.15`), or (single word repeated ≥2×
  with `avg_logprob < -0.7` and `mean_nsp > 0.15`). Missing stats (stubs)
  always pass through. The dictation endpoint reuses this backend, so composer
  dictation is covered too.
- `james-voice-service/app/main.py` — `end_utterance` with an empty (all-silence)
  buffer is ignored (`empty_end_utterance` event) instead of cancelling the
  in-flight turn; per-utterance frame counter + concise `utterance_audio`
  metadata event.
- `james-voice-service/app/pipeline.py` — concise `turn_stt` metadata event
  (VAD prob, audio secs, transcript chars, logprob, no_speech flag, STT ms; no
  text, per logging convention). VAD/STT/RAG/TTS logic untouched.
- `james-voice-service/tests/test_stt_guard.py` (new, 7 tests, no model).
- `james-voice-service/tests/test_pipeline.py` (+2: no_speech skips RAG;
  cancelled turn cannot cancel the next).
- `Personal website/pwtest-local/p2n-double-eou.mjs` (new live regression probe;
  no product code changed).

## 4. Before/after diagnostic evidence

Trace of one "Hello" (WS probe + server events), after fix:

- `utterance_audio` frames=8 bytes=23002 secs=0.72 → `turn_stt` vad=1.0 chars=6
  logprob=-0.18 no_speech=false 1282 ms → `turn_timing` fast_path, `turn_complete`.
- Garbage 0.10 s clip: backend returns `no_speech=True` → `turn_end:empty`,
  zero audio frames, no transcript emitted (before: hallucinated text would
  have flowed to RAG/UI).
- `nimoh.wav` still `Tell me about Chidera and Alimosho.` (-0.32): prompt
  accuracy preserved; `Hello. Hello.` still passes (genuine repetition kept).

## 5. Test results

- Voice suite **161/161 OK** (was 150; +7 guard, +2 pipeline, +2 pre-existing
  P2M streaming tests).
- Live: `p2n` fixed (transcript delivered); `p2j` hello complete;
  `p2m` barge-in lifecycle 5/5 (interrupt 0.08 s, new turn complete, stale=0);
  `p2d` interrupt stale=0; silence → `empty`, no phantom turn.
- Portfolio: relevant suites 74/74, `tsc` clean, `eslint` clean, `next build`
  succeeds (no portfolio file changed).
- Regression list §6: (1) hello-once ✓ p2j; (2) valid→RAG→complete ✓ p2j +
  pipeline; (3) no aggregation duplication ✓ join-is-1:1 + hello-twice;
  (4)(5) interrupt-once + next turn ✓ p2m + detector tests; (6) no false
  barge-in ✓ threshold/sustain/guard tests + AEC (device-dependent, see §6);
  (7) late audio discarded ✓ predicate tests + p2m; (8) cancel isolation ✓ new
  pipeline test + p2m; (9) empty/silence ✓ silence probe + pipeline test;
  (10) error mapping ✓ existing failure-code tests, unchanged.

## 6. Remaining limitations (verified vs hypothesis)

- **Verified fix:** hallucination class rejected; empty-`end_utterance` no
  longer kills turns; turn-taking, barge-in, stale protection, quotas/auth,
  RAG text-only contract unchanged.
- **Hypothesis (stated, not proven):** the user's exact `Alimosho, Alimosho`
  string most plausibly came from ambiguous mic audio (browser AEC/AGC chain +
  sub-threshold frame dropping makes browser audio choppier than file audio)
  latching onto the prompt token — the loop class is reproduced, the exact
  token/input pair is not, since no browser capture was available here.
- A wrong-but-confident single word on a tiny fragment (e.g. `Love.`
  -0.35/0.37) is irreducible decoder ambiguity and still passes; the
  endpointing (≥900 ms utterance) and VAD layers make such fragments rare on
  the real path.
- If "no response" persists for valid speech in production, the next place to
  look is the remote RAG round trip (25 s timeout → `rag_failed` surfaces only
  as a return to listening) — outside this service, not observed here.
