# P2X — Capture, STT, and Latency Investigation: Report

## 1. Executive summary

- The spectral discrepancy is **methodologically sound** (Hann-windowed FFT
  confirms it) and originates **before our pipeline**: our stages are
  spectrally neutral by construction, and gate-simulation on clean audio
  changes nothing. The bass-heavy/treble-vacant shape enters with the
  browser-delivered audio (room, mic distance/gain, OS/browser processing).
- A temporary `?raw-mic=1` diagnostic (main-thread send-all-frames while
  capturable; endpointing untouched) is added to record what the energy gate
  drops. Verified safe in-harness (turn completes, VAD/guard intact).
- Real track settings ( Chromium): mic 48 kHz mono 16-bit, AEC/NS/AGC all
  honored; AudioContext 44.1 kHz here → worklet ratio 2 → 1.38×-slowed audio
  labeled 16 kHz (tested tolerable; not the bass cause).
- small.en vs base.en on identical audio: recovers exact phrases where
  base.en fails, zero reference regressions; costs ~3.3× latency (warmed
  medians below) and +900 MB RSS. Prompt bias ("Alimosho" hijack) affects
  both models; removing it breaks names — no prompt change made.
- No production-code behavior change beyond the temporary diagnostic flag;
  no thresholds/models/decoding altered.

## 2. Verified audio-path data flow

Mic → `getUserMedia({echoCancellation, noiseSuppression, autoGainControl})`
→ MediaStreamSource (browser resamples track→context rate) → `mic-tap`
worklet: stride-`floor(contextRate/16000)` decimation to ~100 ms PCM16 frames
+ per-frame `{active: mean|abs|>120, level}` → main thread sends active frames
(base64 `audio_frame`) → server validates → `audio_buf` → `end_utterance` →
silence-strip → Silero max-window VAD (0.5) → int16→float32 /32768 →
faster-whisper (base.en default / small.en local overlay; beam 1, temp 0,
`initial_prompt`, cond-prev off) → hallucination guard → transcript/RAG/TTS.
Diag WAVs are written from the exact `audio` bytes passed to `transcribe`
(byte-equality proven); nothing filters, normalizes, or resamples after the
worklet. Sub-gate frames never leave the browser (except `?raw-mic=1`).

## 3. Measurements (reproduction)

- Spectra: FFT band shares, rectangular + Hann verified; gate-simulation
  script `spec2.py` (container /tmp) reproduces the table; reference files
  are clean/file-origin (provenance limit noted — not room recordings).
- Gate effect on clean audio: keeps 70–100% of frames, spectrum unchanged
  (bass 0.0% before and after) — the gate does not manufacture bass.
- Raw-vs-gated transcript on loud turn audio: identical
  ("James built with AWS.", -0.26 both).
- Warmed STT latency, same file (hello.wav), load ~9: base.en 1.9/1.6/1.7 s
  (median 1.7); small.en 8.1/6.1/6.6 s (median 6.6). Earlier moderate-load:
  1.2 s vs 3.9 s. Small samples — medians with ranges, no p95 claims.
- Memory: +900 MB for small.en in-process; container ~1.6–2.1 GiB with both
  resident (single-model production floor ≈ 1.3 base / ≈ 1.8 small).
- Live turns: hello 6.5–7.7 s complete; q-aws 42–69 s (RAG-tail bound).

## 4. Controlled comparisons

- 9 reporter takes: table in P2Y §10 (unchanged findings).
- Prompt isolation (t3 take): WITH "Alimosho skill." (-0.26); WITHOUT
  "Hello, I'm Shuski." (-0.93); WITHOUT just "Alimosho" exact (-0.55);
  nimoh WITHOUT "Alimosho" degrades to "Alimasho" — tradeoff quantified.
- HPF 80/100 Hz: no accuracy change; 100 Hz degrades one take. Not implemented.
- ChatGPT: no same-file test possible from here (no access to its dictation
  input path). Recommended user protocol: (a) play our saved `<id>.wav`
  aloud into ChatGPT dictation and compare with our transcript of that file
  (tests recognizers on near-identical acoustics, modulo room re-transduction);
  (b) separately dictate live into both apps (tests whole chains, cannot
  isolate the model). Do not infer ChatGPT internals from outcomes.

## 5. Root-cause findings

- **Confirmed**: captures are objectively bass-heavy/treble-vacant at the
  browser boundary; our code cannot produce that shape.
- **Confirmed**: base.en + prompt converts ambiguous greeting onsets into
  confident wrong words; small.en recovers 4+ takes; neither model invents
  audio (deterministic reruns match live to 4 decimals).
- **Probable**: mic distance/gain + room/AEC behavior starve the speech band
  (needs the pending close-mic comparison recording).
- **Unresolved**: exact words in ambiguous takes (no listening available);
  user-hardware echo levels; Safari behavior.

## 6. STT accuracy and latency comparison

Exact-match on file refs 8/8 (base) with spot-checks on small; reporter
takes in P2Y §10 table; latencies above (load-labeled). Accent claim: none
made (nimoh origin unverified; reporter captures are the only real-mic data).

## 7. Next steps (ranked)

1. Reporter: close-mic "Hello, how is your skill?" + normal-position same
   phrase; optional `?raw-mic=1` run to expose dropped frames.
2. Owner decision: small.en on larger host vs base.en + re-asks (costs above).
3. Keep prompt intact unless names tradeoff is explicitly accepted.
4. Remove `?raw-mic` + `?voice-diag`-era flags after hardware verdict.

## 8. Files changed (uncommitted, minimal)

- `components/TalkToJames.tsx`: `RAW_MIC_DIAG` flag + send-all branch only.
- No service, threshold, model, prompt, contract, or dependency changes.

## 9. Tests and limitations

Voice 171/171; portfolio voice/wiring 65/65; tsc/eslint clean on touched
files. Live turn + raw-mic turn verified in-harness. Fake-mic limits apply
(accent, AEC, speakers); real-mic confirmation is the reporter's step.
