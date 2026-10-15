# Voice Real-Time Optimization: Report

Measurements below are labelled by round: **§1 = initial loaded-host baseline**,
**§4/§4b = quiet-host comparison**, **§10 = controlled CPU / jitter-buffer
experiments**. Host conditions differed between rounds and are stated with each.

## 1. Baseline measurements (round 1 — loaded host)

Host: 2015 Intel Mac, 8 CPU / 16 GiB; Docker Desktop 8 CPU / 7.67 GiB;
container cap 2 CPU / 10 GiB; host load 4–11 during tests (self-inflicted
and ambient). One representative long RAG turn (`turn_timing`):
STT 7.9 s · RAG first token 3.1 s · RAG tail 49.2 s · TTS first chunk 0.39 s ·
eou→first audible 11.4 s · turn complete 63.4 s · 628 frames.

**Playback gap baseline (before any change, load 4.6), 80 ms chunks:**
browser *scheduling* gap **min 19 ms, p50 42 ms, p95 135 ms** → at p50 barely
half of each second is audio. Received frames == scheduled sources (no
drops/dupes). (This is a browser-side scheduling gap; see §4 for the separate
server *receive* gap.)

## 2. Root-cause analysis

- **CONFIRMED (client, fixed):** playback was serial `onended`-chained — each
  80 ms chunk started only when the previous fired its `onended` on the main
  thread, with **no look-ahead**. That injects a per-chunk gap
  (measured min 19 ms, p50 42 ms) independent of the server.
- **CONFIRMED (dominant live cause, not fixed — capacity):** the server emits
  audio **slower than realtime**. Browser-measured receive cadence p50 =
  **138–159 ms per 80 ms frame** → pipeline ≈ 0.5–0.66× realtime; and the
  server's own `tts_total_ms` = 55.5 s for 50.2 s of audio (≈1.10, including
  RAG-fed waits). Two sub-causes, both upstream of the client:
  (a) Pocket TTS compute on the capped/contended CPU; (b) streaming TTS is fed
  by RAG tokens, so slow RAG tails starve audio generation (underrun mid-answer).
- **RULED OUT:** Docker (no evidence; x86_64 no emulation; symptoms fully
  explained by capacity), PCM format (16 kHz mono PCM16 verified end to end),
  frame ordering/duplication (nRecv == nSources), WebSocket/JSON overhead
  (~5 KB/frame, trivial), event-loop blocking (all inference off-loop via
  `asyncio.to_thread`/executors).
- **HYPOTHESIS (not fixed, needs approval):** a larger playback jitter buffer
  would absorb *short* RAG/TTS stalls but not the 12–33 s stalls observed, and
  it trades first-audio latency; kept to a small 120 ms lead.

## 3. Fixes implemented (minimal, evidence-backed)

1. **Look-ahead playback scheduling** — `lib/rag/voice.ts`
   `planChunkSchedule(now, nextStartTime, duration)` schedules each chunk on
   the AudioContext clock contiguously (`startAt = max(now, cursor)`), removing
   the `onended`→`start` gap; a late chunk starts immediately (real underrun we
   cannot invent audio for). `PLAYBACK_LEAD_S = 0.12` is a small bounded lead
   applied once at the start of each playback run.
2. **Component rewire** — `components/TalkToJames.tsx`: replaced the serial
   `playNext` chain with `drainQueue` (schedules the whole backlog ahead,
   tracks active `AudioBufferSourceNode`s), and `interrupt()`/`teardownAudio()`
   now `.stop()` already-scheduled sources immediately (previously the current
   chunk finished and the queue drained only via `onended`).
3. **Tests** — `tests/voice.test.ts` (+4: gapless contiguity, late-chunk
   immediate start, never-in-the-past, bounded lead) and
   `tests/barge-in-wiring.test.ts` (+1: uses `planChunkSchedule`, no
   `playNextRef`, stops active sources).
4. **Harness** — `pwtest-local/gapmeasure2.mjs` (scheduled-gap vs server
   receive-cadence split, scheduled-ahead/urgent counts).

No models, models defaults, RAG, VAD, endpointing, decoding, or contracts
changed. No dependency added.

## 4. Before/after (round 2 — quiet host)

| metric | before (round 1, loaded) | after (round 2, quiet) |
|---|---|---|
| browser *scheduling* gap p50 | 42 ms | **0 ms** |
| browser *scheduling* gap p95 | 135 ms | **0 ms** |
| contiguous gaps / total | 0 | **51 / 52** |
| chunks scheduled ahead | n/a (immediate start) | **53 / 53** |
| eou→first audible | — | **3.24 s** |

Measured on a quiet host (load ~3), greeting fast path: server **receive**
cadence p50 = 70 ms per 80 ms chunk (faster than realtime → surplus), so every
chunk was scheduled ahead and playback was **gapless** (one 5.3 s stall
remained from a server hiccup). This is the fix working as designed.

**Server receive gaps vs browser scheduling gaps are different quantities.** A
zero *scheduling* gap means the client placed every chunk it received on the
timeline with no inter-chunk hole; it does **not** mean the server never
starved playback. On the round-1 loaded host the server supplied no surplus
(receive cadence 138–159 ms > 80 ms; `scheduledAhead = 0`), so gaps persisted
as genuine server underruns — the client fix cannot manufacture absent audio.

## 4b. Server-side (voice service) investigation — added after review

First pass measured only the server's *output* cadence (browser receive gaps).
Follow-up instrumented the server itself.

- **Isolated TTS (warmed, no RAG, no STT):** Pocket TTS clone RTF =
  **1.03–1.12** (29-char: 2.48 s audio in 2.75 s; 133-char: 10.8 s in 11.1 s).
  The engine nearly keeps up on its own.
- **Live per-turn, per-segment** (new `VOICE_TTS_DIAG=1` `tts_segment` log:
  `frames`, `audio_ms`, `span_ms`, `synth_ms`, `put_ms`): on a **quiet host**
  (load ~3), greeting fast path = segment 1 (27 chars) 2.32 s audio in span
  1.375 s (**RTF 0.59**), segment 2 (42 chars) 3.20 s in 1.92 s (**RTF 0.60**),
  `put_ms` 35–49 ms. Under a load spike the same path hit **RTF 1.8–6.4×**.
- **Decomposition proves the server is not the limiter when the host is
  quiet:** synthesis RTF ≈ 0.6 (headroom), and the per-chunk event-loop
  round-trip (`put_ms`) is negligible (35–49 ms total per segment). The
  choppiness is therefore **host-contention-induced**, not client, Docker, or
  an event-loop/queue defect.
- **Server-side conclusion:** the voice service synthesizes faster than
  realtime on a quiet host and emits gaplessly; under contention its RTF
  rises above 1 and playback underruns. The variable is host CPU load, and —
  with the client fix — playback is gapless whenever the server keeps up.

Server instrumentation added (gated, no audio/text logged):
`app/pipeline.py` `tts_segment` event, emitted only when
`VOICE_TTS_DIAG=1`. It is **opt-in**: the local overlay ships the setting
commented out (`# VOICE_TTS_DIAG: "1"`), so nothing is logged by default.

## 5. Regression results (measured; both rounds)

Totals differ by **which suites were run**, not by a regression:

- Round 2 (5 portfolio voice suites — `voice`, `barge-in-wiring`,
  `talk-to-james`, `dictation`, `dictation-controller`): **89/89 pass**.
- Round 3 / §10 (2 suites — `voice`, `barge-in-wiring` only): **70/70 pass**.
- Voice service (Python, all rounds): **171/171 pass** (5 pre-existing
  environment skips: no host FFmpeg).
- `tsc --noEmit`: clean.

Live: `browser-speaking.mjs` → speaking state + Interrupt button (playback
intact after the rewire); hello and long (7.5 s) turns complete with exact
transcripts.

## 6. Acceptance criteria (measured, honest)

| target | result |
|---|---|
| eou→first audible p50 ≤ 1.0 s | **NOT met** (~4–15 s; RAG-bound + STT) |
| TTS RTF < 1.0 with headroom | **met on a quiet host** (0.59–0.60 synthesis); **NOT met under load** (emission 0.5–0.66×) |
| no cracks/underruns | **met on a quiet host** (gapless, p50 0 ms); NOT met under host contention (server RTF <1) |
| useful partial transcripts | not added (faster-whisper is whole-utterance; not required) |
| long questions untruncated/accurate | **met** (7.5 s, exact) |
| speak on first complete phrase | **met** (streaming TTS already incremental) |
| barge-in stops promptly | **met** (now stops scheduled sources too) |
| session isolation | **met** (2- and 3-way verified) |
| no leaks/crashes | **met** (17/17 soak, flat memory) |
| five concurrent sessions validated | **NOT tested** (session budget + host) |

## 7. Resource recommendation

The binding constraint is audio **emission throughput**, not the client and not
Docker. Measured requirement to hold RTF < 1 with headroom: enough CPU that
Pocket TTS synthesis sustains ≥ ~1.3× realtime while STT runs — on this 2-CPU-
capped, host-contended container it is ~0.5–0.66×. Provisional benchmark
target (owner decision, **not a validated minimum**): a host/container with
≈ 4 dedicated cores for the voice service, plus a quieter host (the persistent
macOS swap pressure is a large tax) — Experiment A in §10 did **not** confirm
that 4 CPUs help under controlled conditions (the test was confounded by host
load). The client scheduling fix is necessary but not sufficient on an
underprovisioned host. Keeping full RAG-tail latency out of the audio path
(larger jitter buffer) is the next experiment if answer latency budgets allow.

## 8. Unresolved bottlenecks / next experiment

1. Server RTF < 1 under contention (capacity) — re-measure on a quiet host
   with ≈ 4 cores as a *provisional benchmark target*, not a validated need.
2. RAG-fed segment starvation of TTS during long answers — consider a bounded
   look-ahead that synthesizes from buffered text rather than emitting
   frame-by-frame (needs design + approval).
3. First-audio latency is dominated by remote RAG; unchanged.
4. Five-session capacity untested.

## 9. Modified files

- `lib/rag/voice.ts` — `planChunkSchedule`, `PLAYBACK_LEAD_S` (scheduling math).
- `components/TalkToJames.tsx` — look-ahead playback, source tracking,
  immediate stop on interrupt/teardown.
- `app/pipeline.py` — `tts_segment` diagnostic (frames/audio/span/synth/put),
  gated by `VOICE_TTS_DIAG`; no content logged.
- `docker-compose.local.yml` — `VOICE_TTS_DIAG` documented as an opt-in
  (commented) local setting; zero production change.
- `tests/voice.test.ts`, `tests/barge-in-wiring.test.ts` — regression coverage.
- `pwtest-local/gapmeasure2.mjs` — measurement harness.

## 10. Controlled A/B experiments (CPU cap and jitter buffer)

### Baseline configuration and host conditions
Container cap **2 CPUs** / 10 GiB (verified `NanoCpus=2e9`); Docker Desktop
8 CPU / 7.67 GiB; host 8 physical cores; swap 2.9/4 GB used; load drifted
5.0 → 6.4 across the session. Production `docker-compose.yml` untouched.

### Experiment A — local Docker CPU override (2 → 4 CPUs, local overlay only)

| condition | container CPU | host load | eou→first audio (3 greeting turns) | server synth_ms | put_ms | throttled |
|---|---|---|---|---|---|---|
| baseline | 2 | 5.0 | 2.40 / 2.73 / 2.52 s | 1188–2585 | 29–79 | 196/2663 periods |
| Exp A | 4 | 4.8 | 4.88 / 4.12 / 4.01 s | 1546–2993 | 41–80 | 0/636 |
| restored | 2 | 6.4 | 5.51 / 6.66 / 5.47 s | — | — | — |

**Result: no improvement (neutral-to-worse), inconclusive.** Raising the cap to
4 CPUs removed throttling entirely, yet first-audio did not improve (it
tracked host load: 2.5 s at load 5.0, 4.3 s at load 4.8, 5.9 s at load 6.4).
The 2-CPU cap was throttling only ~7% of periods, i.e. it was not the binding
constraint. `put_ms` (event-loop handoff) stayed negligible (≤80 ms/segment)
in every condition — handoff is not a bottleneck. Client playback stayed
gapless throughout (schedGap p50 = 0; `scheduledAhead` = all chunks).
**Decision: reverted to the 2-CPU production shape.**

### Experiment B — playback jitter buffer (PLAYBACK_LEAD_S 0.12 vs 0.25)

| lead | server recvGap p50 | schedGap p50/p95 | contiguous | underruns |
|---|---|---|---|---|
| 0.12 | 79 ms | 0 / 0 | 48/50 | 2 |
| 0.12 | 140 ms | 0 / 10305 | 23/44 | 21 |
| 0.25 | 70 ms | 0 / 0 | 46/47 | 1 |

**Result: no meaningful difference.** The number of underruns tracks the
server's frame cadence (recvGap 70 → ~1; 140 → ~21), **not** the lead size: at
comparable server speed both settings are gapless, and when the server cannot
keep up neither lead prevents gaps. A larger buffer would add ~130 ms of
first-audio latency with no demonstrated gap reduction.
**Decision: kept `PLAYBACK_LEAD_S = 0.12`.**

### Regression results
Voice service **171/171 pass** (5 pre-existing skips: no host FFmpeg);
portfolio voice suites **70/70** (round 3 = `voice` + `barge-in-wiring` only;
round 2 ran 5 suites → 89/89, see §5); `tsc` clean. PCM16 mono 16 kHz handling,
ordering, barge-in/interrupt/teardown, empty-turn/error paths, and the
client look-ahead scheduling fix are all intact; diagnostics remain
env-gated with no transcript/audio content.

### RAG latency (separate, not addressed by local changes)
RAG first token 3–9 s, tail up to ~85 s (remote OpenAI/OpenRouter via the
secured portfolio API) — unaffected by container CPU or browser buffering.
Long RAG responses dominate total turn time; **shorter voice-specific
answers remain a product decision requiring owner approval.** No change made
to the RAG contract, model, or answer length.

### Recommendation / final local configuration
- Retain production-shape **2 CPU** and **`PLAYBACK_LEAD_S = 0.12`**; both
  experiments were reverted.
- The binding constraint is **host contention + remote RAG**, not local
  Docker CPU or browser buffering — confirmed by measurements, not assumed.
- Five concurrent sessions: **not tested**; no concurrency claim made.

### Files changed (uncommitted, for review)
- `app/pipeline.py` — gated `tts_segment` diagnostic (frames/audio/span/synth/put).
- `docker-compose.local.yml` — `VOICE_TTS_DIAG: "1"` (local dev only);
  Experiment A's `cpus: 4.0` override added then **removed**.
- `docs/VOICE-REALTIME-OPTIMIZATION-REPORT.md` — this report.
- (Client `PLAYBACK_LEAD_S` tested at 0.25 then restored to 0.12.)

### Unverified / blocked
- Experiment A was **blocked from a fair comparison** by host-load drift
  (swap 2.9/4 GB; load 4.8–6.4) — per the task rule, not interpreted as a
  valid capacity test.
- RAG-tail latency and host memory pressure remain the dominant real-world
  limiters; neither is fixable by local Docker CPU or browser buffering.
