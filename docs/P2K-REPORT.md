# P2K — Idle RAM Audit & Optimization: Report

Scope: `james-voice-service/` only (portfolio unchanged). Voice cloning, STT,
VAD, streaming TTS, barge-in, cancellation, quota/auth and the authenticated
RAG contract are all preserved. No deploy, no commits.

## 1. Executive summary

- The reported ~1.93 GB is **not one number**: the server process RSS was
  ~**1.62–1.70 GiB** while idle, `docker stats` showed ~**1.48 GiB**, and the
  higher figure includes Docker Desktop/macOS overhead. All three were measured
  distinctly.
- **No duplicate model loads and no unbounded leak** were found. Each model
  loads exactly once (`models_warm=1`, `tts_voice_mode=1`), and RSS **plateaus**
  under repeated turns — the earlier growth was glibc heap retention, not a
  growing object graph.
- The **unavoidable floor is ~1.28 GiB of resident model weights**: torch
  ~208 MB, faster-whisper `base.en` ~400 MB, Pocket TTS ~550 MB, cloned-voice
  state ~107 MB, Silero VAD ~4 MB.
- Three safe changes cut idle RSS from **~1.62 GiB → ~1.30 GiB** and held the
  post-soak plateau from **~1.79 GiB → ~1.34–1.40 GiB**, with peak **1.46 GiB**
  under two concurrent sessions — well inside the 10 GB cap.
- Latency and accuracy are unchanged: greeting first-audio **2.15 s**, exact STT,
  barge-in with **0 stale frames**, **141/141** tests pass.

## 2. Baseline measurements and environment

Host: macOS Intel x86_64, 16 GB RAM, host load 7–23 during runs.
Container: Docker 24.0.7, `cpus: 2.0`, `mem_limit: 10g`, `OMP_NUM_THREADS=2`.
One uvicorn worker (PID 1), 5 processes / 14–16 pids.

| view | value |
|---|---|
| server process RSS (VmRSS, idle, after a session of activity) | **1,696,504 KB ≈ 1.62 GiB** |
| `docker stats` container memory | **1.48 GiB / 7.67 GiB** |
| reported figure (Docker Desktop / peak) | **~1.93 GB** |

Idle was **flat** once warm: 1,364,496 KB at t0, t+60 s and t+120 s — no
background growth (the startup warm-up task ends and nothing runs while idle).

## 3. Memory breakdown by component

Measured by staged loading in one process (`gc.collect()` between steps):

| stage | cumulative RSS |
|---|---|
| python baseline | 14.5 MB |
| + torch | 222.6 MB |
| + faster-whisper `base.en` (int8) | 622.5 MB |
| + Pocket TTS model | 1173.1 MB |
| + cloned-voice state (30 s ref) | 1280.4 MB |
| + Silero VAD | 1284.2 MB |

So the **~1.28 GiB floor** is model weights/runtime. Pocket TTS is ~109 M params
(`flow_lm` 89 M + `mimi` 20 M) held in **fp32** (~436 MB of weights) plus runtime
buffers; whisper `base.en` int8 costs ~400 MB including CTranslate2 workspace.
These cannot shrink without changing model precision or size (see §9).

## 4. Root causes confirmed / ruled out

- **A. Duplicate model initialization — RULED OUT.** `get_models()` is a
  singleton behind a lock; STT/TTS/VAD each load once (guarded by per-backend
  locks); logs show one warm-up and one TTS init. The configured
  `VOICE_STT_FALLBACK_MODEL` is never loaded (dead config).
- **B. Model/voice-state residency — CONFIRMED FLOOR.** Weights dominate (§3);
  the early-request `voice state` (30 s) is only ~107 MB, so shortening the
  reference buys little and was **not** changed (voice identity preserved).
- **C. Warm-up — CORRECT.** Warm-up loads the same singletons in a worker
  thread; readiness gating (added in P2J) prevents a request racing it. It
  retains no audio/tensors after finishing. Not disabled.
- **D. Audio queues — ONE UNBOUNDED QUEUE FOUND AND FIXED.** The streaming-TTS
  chunk queue (`asyncio.Queue()`) had no size limit; it is now
  **bounded (maxsize 64)** with producer backpressure and a drain-on-cancel
  path. Per-session `audio_buf` and per-turn `answer_parts` are already bounded
  and cleared per turn.
- **E. Session/task lifecycle — NO RETENTION FOUND.** Memory was unchanged
  across a 3-session churn (+0 KB) and plateaued across 40 greetings + 16
  interrupts.
- **F. Native/heap behavior — CONFIRMED as the growth source.** RSS rose while
  turns ran then **stabilised** (no leak); glibc retained freed inference
  buffers. This is exactly what the two heap controls below address.

## 5. Changes implemented and files affected

1. **`MALLOC_ARENA_MAX=2`** — `Dockerfile` (ENV) and `docker-compose.yml`
   (`environment`). The service is multithreaded (executor + torch +
   CTranslate2); bounding glibc arenas stops cross-thread fragmentation from
   inflating RSS.
2. **`malloc_trim(0)` after warm-up and after every turn** — `app/main.py`
   (`_trim_memory()`, called in `warm_models`' finally and at both turn-end
   paths). Returns freed heap pages to the OS; no-op off glibc.
3. **Bounded, backpressured TTS queue** — `app/pipeline.py` `speak()`:
   `asyncio.Queue(maxsize=64)`, producer blocks via
   `run_coroutine_threadsafe(...).result()`, queue drained on cancel.
4. **VAD init lock** — `app/vad.py` (defensive; prevents a duplicate Silero
   load if warm-up and a turn ever race).
5. Test added: `tests/test_pipeline_streaming.py::test_slow_consumer_applies_
   backpressure_without_loss` (200 chunks > 64, slow consumer, no loss/deadlock).

Not done (deliberately): no model unloading/reload-on-demand, no precision or
model-size change, no second worker process.

## 6. Before-and-after idle and peak

| measurement | before | after |
|---|---|---|
| server RSS, warm idle | ~1.62 GiB | **1.30 GiB** (1,368,280 KB) |
| `docker stats` container, idle | 1.48 GiB | **1.17 GiB** |
| post-soak plateau (40 greet + 16 interrupt) | ~1.79 GiB | **~1.34–1.40 GiB** |
| peak, 2 concurrent long turns | — | **1.46 GiB** (1,530,384 KB) |
| idle drift over 2 min | — | **0 KB** |

## 7. Sequential-turn and cancellation soak

Real WebSocket path, real STT/TTS/RAG:

| phase | RSS after |
|---|---|
| warm idle | 1,368,280 KB |
| +20 greetings | 1,376,400 KB |
| +20 greetings | 1,381,432 KB |
| +8 interrupts (mid-TTS) | 1,388,052 KB |
| +8 interrupts | 1,403,952 KB |
| +3 session churn | 1,403,952 KB |

Monotonic but **decelerating and flat** (+~35 MB across 40 turns, +0 on churn);
cancellation leaves no stale audio and no growing object set. (Pre-fix, the same
soak climbed to ~1.79 GiB and did not settle as tightly.)

## 8. Regression results

- **STT accuracy:** unchanged — greeting "Hello." and "What is his experience
  with PostgreSQL?" both exact (domain prompt from P2J retained).
- **First-audio latency:** greeting **2.15 s** (EOS→first audible, target ≤2.5 s);
  substantive 7.6 s (RAG-bound, unchanged).
- **Voice cloning:** Pocket TTS clone active (`cloning_active=true`); no preset
  fallback.
- **Barge-in:** interrupt acknowledged, **0 stale audio frames**.
- **Streaming order:** verified by tests (multi-chunk ordering) and backpressure
  test.
- **Auth/quota/RAG:** untouched; existing tests pass.
- **Tests:** voice suite **141/141 PASS**; `py_compile` clean. Portfolio
  unchanged (not rebuilt).

## 9. Remaining limitations and recommendations

- **~1.28 GiB is the model floor** with the current stack/precision. Going below
  it requires one of:
  1. **Quantize Pocket TTS** (`load_model(quantize=True)`, dynamic int8, ~48 %
     lower transformer memory) — needs the `torchao` extra and a **listening
     quality check** before adoption; not applied here (voice identity is a
     hard constraint).
  2. **Smaller STT** (`tiny.en`) — rejected: reduces Nigerian-name/technical
     accuracy, which the product requires.
  3. **Lazy load / unload on idle** — rejected for this service: cold load is
     27–124 s (measured), which would destroy first-turn responsiveness. Only
     worthwhile on a much smaller instance where the floor cannot fit; even then
     it should be paired with an explicit "warming" state.
- All numbers are **x86_64 under host load up to 23**; ARM/production not
  tested. Docker Desktop's own overhead sits above the container figure and is
  outside the service's control.
- Small residual (heap fragmentation) may persist on other allocators/kernels;
  the two heap controls are the portable levers.

**Recommended next step:** keep the three changes; if further reduction is
required for a smaller instance, validate **Pocket TTS dynamic int8
quantization** for memory (expect ~150–260 MB) with a human listening check on
the cloned voice before enabling it.
