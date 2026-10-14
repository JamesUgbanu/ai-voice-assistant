# P2P — Restore Voice Conversation: Report

Follow-up to P2O, driven by the reporter's newest browser captures (7 further
utterances, 10:54–10:57) plus a controlled non-greeting turn. P2M is not the
cause (re-verified). No deploy, no commits, no new model/dependency.

## 1. Exact root cause (staged, evidence-backed)

On the reporter's audio, Whisper itself is accurate on clean input
(`Hello?` -0.20, `That's it, hello.` -0.58); the failures are:

1. **Degraded capture.** Envelopes show quiet onsets and mid-word dips
   (e.g. `[…,1140,261,2127,…]`), plus threshold-skimming blips (0.2 s at RMS
   ~200). The worklet gate drops soft onsets (`Hello` → `ello`-class input)
   and splits words on dips — the fragments that mistranscribe.
2. **Wrong short transcripts passed the P2O guard** (`I hope.` -0.72,
   `The Know.` -0.66 — just above its floors) → shown in UI → sent to RAG.
3. **Remote RAG takes 15–28 s to first token** (external OpenAI path; portfolio
   streams correctly, so this is upstream latency under load, not a voice
   defect and not changed here). A wrong transcript therefore also means a
   ~30 s wait: `rag_ms=28040`, 39 s turn measured live.
4. Users disconnect/retry during the wait (`disconnects` = sessions 8/8);
   in-flight turns cancel silently server-side (no completion event), and
   pre-P2O empty turns returned silently — the "no response".

Last-good / first-bad per stage: capture→frames→VAD→STT→`transcript_final`
all succeed; the turn starts; RAG is slow; TTS works (529-frame answer
completed in-probe, slow only under this machine's load).

## 2. Changed files (minimal)

- `Personal website/lib/rag/voice.ts` — pure `pushPreSpeech` (bounded
  3-frame onset buffer) and `hangoverStep` (2-frame dip bridging). Neither
  touches endpointing, which still keys off real speech + 900 ms silence.
- `Personal website/components/TalkToJames.tsx` — flush buffered onset at
  each new speech run; hangover-send through brief dips (padding never counts
  as voice); reset both on interrupt/endpoint. Plus the P2O turn-end messages
  (kept): "I didn't catch that" on empty, "Voice hit a snag" on failures.
- `Personal website/tests/voice.test.ts`, `tests/barge-in-wiring.test.ts` —
  unit + wiring cover for the above. Voice-service code unchanged this round.

## 3. Test evidence

- Client: voice suites 66/66, full portfolio 605 pass (2 pre-existing unrelated
  failures: `cv-detailed` missing `/tmp` fixture, `agent-sdr` #8), `tsc` /
  `eslint` clean, `next build` succeeds.
- Service (unchanged, re-confirmed): 162/162; live `Hello.` → complete,
  garbage → empty/zero-frames, duplicate-`end_utterance` → delivered,
  barge-in lifecycle 5/5 with stale=0.
- Not automated (stated explicitly): the final microphone-to-response proof in
  the reporter's browser and room; requested below.

## 4. Remaining limitations

- Capture quality (mic distance, room, laptop AEC pumping) remains the
  dominant variable; the pipeline now preserves onsets, bridges dips, rejects
  garbage, and narrates failures instead of confabulating or going quiet.
- RAG latency (15–28 s observed) and full-answer TTS duration under load are
  upstream/environmental; voice answers begin at first audio (~8 s measured)
  and failures are now visible.
- Temporary turn-audio capture (`VOICE_SAVE_TURNS_DIR`, local only) stays ON
  for the confirmation round; revert that one compose block after.
- Production still runs pre-P2N code; nothing here takes effect there until
  `docker compose up -d --build voice` runs on the VPS.
