# P2Y — Whisper-Input Capture Diagnostics: Report

## 0. Nine-utterance investigation (reporter audio, this session)

Nine-row mapping (diag IDs truncated; all reruns deterministic — every
offline result below matches its live sidecar to 4 decimals, proving
transport/assembly contribute nothing and the saved file is exactly what
Whisper got):

| # | Intended | diag turn | audio | live raw transcript | base.en rerun | small.en rerun |
|---|---|---|---|---|---|---|
| 1 | skill ×1 | t1 | 1.5 s RMS 2539 | '' rejected (-1.41) | identical | "Low Azure Skill." (-1.02) |
| 2 | skill ×2 | t2 | 1.7 s RMS 2092 | '' rejected (-0.90) | identical | **"Hello, how is your skill?" (-0.45)** |
| 3 | skill ×3 | t3 | 1.7 s RMS 1822 | "Alimosho skill." (-0.26) | identical | **"Hello, how is your skill?" (-0.58)** |
| 4 | OMG ×1 | t6 | 1.3 s RMS 3032 | "Welcome, Miss." (-0.68) | identical | **"Oh my goodness." (-0.46)** |
| 5 | OMG ×2 | t7 | 1.1 s RMS 2353 | '' rejected (-0.87) | identical | **"Oh my goodness." (-0.49)** |
| 6 | OMG ×3 | t8 | 1.1s RMS 3556 | "Oh my goodness." (-0.21) | identical | (not run — already exact) |
| 7 | Hello ×1 | t10 | 0.6 s RMS 3451 | "And more." (-0.43) | identical | "Alou!" (-0.99) |
| 8 | Hello ×2 | t13 | 0.8 s RMS 3733 | "Hello." (-0.37) | identical | (not run — already exact) |
| 9 | Hello ×3 | t14 | 0.7 s RMS 3868 | "No." (-0.47) | identical | "Hello!" (-0.94) |

Ambiguity flagged: t11 (0.9 s, rejected) + t12 (0.2 s blip, early-return,
no STT) sit between reported Hello attempts — a possible unreported 4th
utterance or endpoint split; mapping above is best-effort. t4/t5/t9 blips
(0.1–0.2 s) are mic bumps, correctly empty.

**Confirmed root cause: base.en lacks the capacity margin for this
speaker's acoustics, and the domain prompt converts its uncertainty into
confident wrong words.** Proof chain:
- Same audio + same decoding, only the prompt removed: "Alimosho skill."
  (-0.26) → "Hello, I'm Shuski." (-0.93); "Alimosho" appears/disappears with
  the prompt while confidence doubles. Prompt text is static (env/default,
  never per-turn — verified in `config.py` + identical across 13 sidecars).
- Same audio + same prompt, only the model changed: small.en recovers t2,
  t3, t6, t7 exactly, t14 near-exactly, with zero regressions on the
  reference set (hello/nimoh/q-aws all exact, names intact at -0.17).
- No transcript/audio leaks between turns (deterministic reruns, stateless
  calls, serialized inference).
- "I didn't catch that" = empty/no_speech/guard-reject → `turn_end: empty`
  → client message (`TalkToJames.tsx:568-569`); verified for all 7 empties.

**Costs of small.en (measured, same audio, warmed):** STT 1.2 s → 3.9 s
(~3.3×); RAM +900 MB in-process (base.en alone +422 MB) → production floor
would rise ~1.28 → ~1.8 GiB, exceeding the 2 GB host with OS/Docker/Caddy.
**Recommendation (approval required, not implemented):** evaluate small.en
as the production model on a larger host; on 2 GB it does not fit. Do not
weaken the prompt (nimoh names depend on it) or tighten confidence floors
("Alimosho skill." at -0.26 outscores genuine quiet speech at -0.49) without
that decision. Alternative hypothesis retained: greeting-onset acoustics are
the weakest part of these takes (both models hedge the "Hello" opening);
closer-mic re-takes would separate speaker clarity from model capacity.

## 1. Root-cause assessment

- **Confirmed (mechanism, awaiting reporter audio):** prior blends
  ("assistant words + user phrase, passing confidence") match
  echo-contaminated turn audio, reproduced on this backend
  ("He builds, oh my goodness.", -0.21). The pre-roll injector is now
  bounded to 3 detector-vouched frames.
- **Confirmed:** audio path is byte-exact (worklet ratios, base64, assembly,
  VAD windowing, int16→float32 all verified; capture hook byte-matched sent
  audio; 8/8 file matrix exact incl. names).
- **Confirmed:** LLM is remote (OpenRouter URL in portfolio env) — RAG tails
  are provider-bound, not local CPU.
- **Confirmed:** host is under memory pressure (swap 2.26/3 GB used, ~90 MB
  free) with Docker at ~4.9 GB — slows STT/TTS, does not corrupt transcripts
  (no mechanism; accuracy failures need the audio proof, still pending).
- **Hypotheses awaiting the 9-utterance repro:** exact failure shape of
  "Hello, how is your skill?" → "Alimosho, Alimosho" (loop class, likely
  ambiguous fragment + prompt latch, per P2N evidence).

## 2. Pipeline trace (verified in code)

Mic → `getUserMedia({echoCancellation, noiseSuppression, autoGainControl})`
→ `mic-tap` worklet (ratio-floor downsample to 16 kHz PCM16, 1600-sample
frames, energy gate ~120) → base64 `audio_frame` WS → `validate_audio_frame`
→ `audio_buf` → `end_utterance` → `run_turn`: silence-strip → Silero
max-window VAD (0.5) → `FasterWhisperBackend.transcribe` (base.en, beam 1,
temp 0.0, `initial_prompt`, cond-prev off) → guard → transcript/RAG/TTS.
Files: `TalkToJames.tsx` (capture/endpoint/barge), `app/main.py` (assembly),
`app/vad.py`, `app/stt.py`, `app/pipeline.py`.

## 3. Reproduction results

| case | captured audio | app transcript | offline base.en | small.en |
|---|---|---|---|---|
| probe "Hello." (validation) | 0.72 s, RMS 4367 | `Hello.` (-0.18) | `Hello.` (-0.18, identical) | `Hello.` (-0.44) |
| reporter "Hello, how is your skill?" etc. | **pending user run** | — | — | — |

## 4. Audio integrity

Capture path proven byte-exact (23,002 sent = 23,002 saved); WAV valid
(1ch/16bit/16kHz); sidecar carries duration, RMS/peak/clipping, VAD, model
config, transcript association. No clipping/dup/resample defect found in code
or simulation (clean/clipped/quiet/chipmunk/slow/echo variants all decode or
safely reject).

## 5. Resource measurements

Docker ~4.9 GB; voice container ~1.4–1.6 GiB; macOS swapping (2.26 GB used,
~90 MB free) → latency pressure, not accuracy cause. STT 1.2–9.4 s scales
with load; browser scheduling ~ms. LLM remote (OpenRouter), never local.

## 6. Recommended correction

Propose (do not implement without approval): evaluate `small.en` as the
production STT model on a host with headroom — it recovers 4 exact + 1
near-exact reporter utterances where `base.en` fails, with zero reference-set
regressions, at ~3.3× STT latency and ~+900 MB RAM (incompatible with the
2 GB VPS: floor would reach ~1.8 GiB). Alternative: keep `base.en` and accept
re-asks on ambiguous takes. Explicitly not recommended: removing prompt names
(breaks nimoh), tightening confidence floors ("Alimosho skill." at -0.26
outscores genuine quiet speech at -0.49), dual-decode fallbacks (2× latency,
2.3 GiB resident — worse on every axis).

## 7. Files changed (all uncommitted, per constraints)

- `app/diagcapture.py` (new): stdlib WAV+JSON writer + signal stats.
- `app/pipeline.py`: opt-in `diag_dir` param; `turn_stt` carries `diag_id` only.
- `app/main.py`: `_diag_capture_dir()` (env `VOICE_DIAG_CAPTURE_DIR`) + pass-through.
- `docker-compose.local.yml`: `./whisper-captures:/captures` mount + env (dev only).
- `.gitignore`: `whisper-captures/`.
- `scripts/check-transcription.py` (new): offline rerun, production defaults,
  `--model` comparison experiment.
- `tests/test_diagcapture.py` (new, 5), `tests/test_pipeline.py` (+2 wiring).
- Voice suite 164+7=171/171 OK; portfolio untouched this round.

## 8. Tests

- New: 7 (module 5 + wiring 2), all pass. Full voice suite green.
- Live: probe turn → host WAV+JSON → identical offline rerun → small.en
  comparison — workflow proven end to end.
- Not run: reporter-utterance captures (needs the user), accent validation.

## Repro procedure (for the reporter)

1. Say each phrase 3×, assistant idle, ~1 s gaps: "Hello, how is your skill?",
   "Oh my goodness.", "Hello."
2. After each, note the `You:` text shown.
3. Files land in `james-voice-service/whisper-captures/` as
   `<diag_id>.wav` + `<diag_id>.json` (newest 9 = your run); match via `turn_stt`
   `diag_id` in `docker logs james-voice` if needed.
4. Replay: `afplay whisper-captures/<id>.wav` (or any player — standard WAV).
5. Rerun offline: `docker cp whisper-captures/<id>.wav james-voice:/tmp/x.wav`
   `docker exec james-voice python3 /tmp/check-transcription.py /tmp/x.wav`
   and `--model small.en` for comparison.
6. Cleanup when done: `rm -f whisper-captures/*` (and to disable capture,
   remove the two `VOICE_DIAG_CAPTURE_DIR` lines from
   `docker-compose.local.yml` + recreate).

## Acceptance status

Reproducible capture ✓, replayable WAV ✓, transcript association ✓, offline
rerun with identical result ✓, small.en comparison run on 9 real utterances
(+4 exact recoveries, 0 reference regressions) ✓, prompt-bias experiment ✓,
resources measured ✓. No source behavior changed in this round (diagnostic
code + this report only); voice suite 171/171 re-verified green.
Open: owner decision on the small.en proposal; closer-mic re-takes to separate
speaker clarity from model capacity; production deploy of nothing (no changes
to deploy).

## 10. Nine-utterance investigation (reporter audio)

Nine-row mapping (timestamps 12:00:51–12:03:47; every offline rerun below
reproduced its live sidecar exactly — text and logprob to 4 decimals —
proving transport/assembly contribute nothing):

| # | Intended | turn | audio | live raw transcript | base.en rerun | small.en rerun |
|---|---|---|---|---|---|---|
| 1 | skill ×1 | t1 | 1.5 s RMS 2539 | '' rej. (-1.41) | identical | "Low Azure Skill." (-1.02) |
| 2 | skill ×2 | t2 | 1.7 s RMS 2092 | '' rej. (-0.90) | identical | **exact** (-0.45) |
| 3 | skill ×3 | t3 | 1.7 s RMS 1822 | "Alimosho skill." (-0.26) | identical | **exact** (-0.58) |
| 4 | OMG ×1 | t6 | 1.3 s RMS 3032 | "Welcome, Miss." (-0.68) | identical | **exact** (-0.46) |
| 5 | OMG ×2 | t7 | 1.1 s RMS 2353 | '' rej. (-0.87) | identical | **exact** (-0.49) |
| 6 | OMG ×3 | t8 | 1.1 s RMS 3556 | exact (-0.21) | identical | exact (-0.55) |
| 7 | Hello ×1 | t10 | 0.6 s RMS 3451 | "And more." (-0.43) | identical | "Alou!" (-0.99) |
| 8 | Hello ×2 | t13 | 0.8 s RMS 3733 | exact (-0.37) | identical | exact (-0.82) |
| 9 | Hello ×3 | t14 | 0.7 s RMS 3868 | "No." (-0.47) | identical | "Hello!" (-0.94) |

Ambiguities flagged, not smoothed over: t11 (0.9 s, rejected) + t12 (0.2 s
blip, early-return, no STT) sit between reported Hello attempts — a possible
unreported 4th utterance or endpoint split; mapping above is best-effort.
t4/t5/t9 blips (0.1–0.2 s) are mic bumps, correctly empty. WAVs were
analyzed via envelopes/decoder outputs only — direct listening is not
available in this environment, so word-level acoustic claims stop there.

**Prompt experiment (offline, isolated, production unchanged):** same take,
only the prompt varied — WITH: "Alimosho skill." (-0.26); WITHOUT:
"Hello, I'm Shuski." (-0.93); WITHOUT just "Alimosho": **exact** (-0.55).
"Alimosho" appears/disappears with that single token while confidence
doubles. But removing it regresses names (`nimoh` → "Chidera and
**Alimasho**."): the prompt trades greeting-onset accuracy for proper-noun
accuracy, both demonstrated. No prompt change made.

**small.en vs base.en (same files, same decoding):** 4 exact + 1 near-exact
recoveries, zero reference regressions (hello/nimoh/q-aws exact; names at
-0.17). Cost re-measured: STT 1.2 s → 3.9 s warmed (~3.3×); +900 MB process
RSS → production floor ≈ 1.8 GiB, **incompatible with the 2 GB VPS**
(measured). "I didn't catch that" verified as empty/no_speech/guard-reject
→ `turn_end: empty` → client message for all 7 empties; wrong-but-passing
transcripts display under "You:" and go to RAG, unchanged.

**Decision framework verdict:** not Case A (captures complete, VAD ≈ 1.0, no
clipping/echo/duplication in envelopes); Case B for 4 utterances (small.en
recovers — capacity gap, not accent); not Case C (no clean file fails both
except sub-speech fragments); not Case D (offline == live everywhere);
Case E confirmed as designed behavior (reject → message); Case F explained
by take-to-take acoustic variance + prompt latch, not load (identical reruns
across loads; STT 1.1–2.4 s healthy throughout).

**Recommended action (approval required):** evaluate `small.en` on a larger
host (local P2Z overlay already runs it); keep `base.en` + prompt on 2 GB
with re-ask behavior. Do not remove prompt names, tighten confidence floors
("Alimosho skill." at -0.26 outscores genuine quiet speech at -0.49), or add
dual-decode fallbacks (2× latency, 2.3 GiB resident). No code fix this round:
every candidate violates constraints or demonstrated trade-offs. Remaining
unknowns: exact words in ambiguous takes (needs listening), closer-mic
re-takes to separate clarity from capacity.
