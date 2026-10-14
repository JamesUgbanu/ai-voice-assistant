# P2Q — P2M Emergency Recovery: Report

The P2P pre-roll/hangover capture change is reverted. It was proven harmful by
direct A/B on the reporter's own audio. Everything else stands.

## 1. Confirmed root cause

The P2P capture-conditioning change (pre-speech onset buffer + dip hangover)
padded every utterance with near-silence. A/B on the reporter's captures
through the real `base.en` backend, identical samples except padding:

| input | padded (shipped) | core only (reverted) |
|---|---|---|
| "Hello" capture | `Alo.` (-0.43) | **`Hello.` (-0.34)** |
| "Hi" fragment | `Home.` (-0.27, confident!) | correctly **rejected** (-1.25) |

Silence padding tipped the already-marginal mic audio into misdecodes — and
turned a correct rejection into a confident hallucination. The hypothesis
("preserve onsets") was wrong; the test that missed it used loud file audio,
which survives padding that quiet mic audio does not.

## 2. Smallest fix (revert, nothing else)

- `Personal website/lib/rag/voice.ts` — removed `PRE_SPEECH_FRAMES`,
  `HANGOVER_FRAMES`, `pushPreSpeech`, `hangoverStep`.
- `Personal website/components/TalkToJames.tsx` — listening handler restored
  to the P2M form (active-gated send + `shouldFinalizeUtterance` endpointing);
  removed `preSpeechRef`/`hangRef` and their resets. P2O turn-end messages
  ("I didn't catch that" / "Voice hit a snag") kept — proven helpful,
  unrelated to the regression.
- `Personal website/tests/voice.test.ts`, `tests/barge-in-wiring.test.ts` —
  removed the conditioning tests; all other tests kept.
- Voice-service code untouched this round (STT guard, empty-`end_utterance`
  guard, diagnostics all stay — each proven by live evidence).

## 3. Test results

- Static: no conditioning remnants (`grep` clean); `tsc`, `eslint` clean.
- Client suites 63/63; voice service 162/162 untouched; `next build` succeeds.
- Live: `Hello.` → complete, first audio 1.4 s; barge-in lifecycle 5/5
  (interrupt 0.05 s, new turn complete, stale=0).
- P2M auto-barge-in: **remains enabled** — exonerated three times over, works
  live, and the stuck-`Thinking…` cases are explained by wrong-transcript RAG
  trips (15–28 s upstream) on which barge-in cannot fire (it only runs while
  speaking, never while thinking).

## 4. Recovery status (explicit)

- Normal voice conversations: **restored to the P2M baseline behavior** —
  valid speech transcribes ("Hello." live-verified) and completes; garbage is
  rejected with a retry prompt instead of a wrong answer; failures are
  messaged instead of silent.
- A 0.4 s breathy "Hi" fragment remains unrecognizable audio and is now
  rejected with "didn't catch that" rather than answered as "Home." — correct
  degradation, not a fix defect.
- Remaining limitations: mic input quality dominates (unchanged); remote RAG
  latency is upstream; production still needs `docker compose up -d --build
  voice`; temporary turn capture stays on locally until the reporter confirms.
