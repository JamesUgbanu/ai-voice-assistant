# P2J — Slow First Response + Nigerian-English Recognition: Report

Scope: `james-voice-service/` (voice service) + one UI file in the portfolio.
Architecture preserved: portfolio RAG is the sole knowledge source/LLM; the
voice service reaches it only over authenticated HTTPS/SSE; Pocket TTS **clone**
mode retained. No deploy, no commits, no infra changes.

## 1. Reproduced symptoms and root causes

**Symptom A — "hello" took ~20 s to show text.** Reproduced on the real
WebSocket path. Warm, the first measured turn was `stt 2.9 s + rag 4.3 s +
tts 0.6 s = 7.9 s` to first audio, and up to 20 s+ under load/cold. Root causes:

1. **Greeting went through the remote RAG.** "hello" was forwarded to the
   portfolio RAG, whose social path makes an **LLM call** — ~4.3 s before any
   audio, just to say hi. No local fast path existed.
2. **Cold-start race.** Warm-up ran as a background task; `/v1/sessions` was
   accepted immediately, so the first turn could pay the cold model load
   (STT+TTS+VAD 27–92 s measured) — the "20 s" regime.

**Symptom B — wrong transcript for Nigerian-accented English.** Reproduced even
on clean input: `PostgreSQL → "PostGIRSQL"`, `Chidera → "Chidara"`,
`Alimosho → "Alimasho"` with the shipped `small.en`. Root cause: **no domain
biasing and an un-tuned decoder** (beam 5, default temperature, no
initial_prompt) — the model guesses low-frequency proper nouns/technical terms.

## 2. STT model / configuration comparison (identical recorded inputs, warm)

Fixed set of 5 (`say`-generated, expected transcripts known); WER = token error
rate on normalized text. Latencies are from a contended host — treat as relative.

| config | p50 s | max s | mean WER | short-utterance exact |
|---|---|---|---|---|
| small.en / beam5 / no prompt (shipped) | 15.29 | 20.44 | 0.100 | 2/3 |
| small.en / beam5 / **prompt** | 6.08 | 6.66 | **0.000** | 3/3 |
| small.en / beam1 / prompt | 5.37 | 5.86 | 0.000 | 3/3 |
| **base.en / beam1 / prompt (chosen)** | **1.35** | 2.36 | **0.031** | 3/3 |
| base.en / beam5 / prompt | 1.49 | 2.09 | 0.031 | 3/3 |

Key findings: (a) the **initial prompt eliminates every misrecognition** —
PostgreSQL, Chidera and Alimosho are exact with it, wrong without it; (b)
**base.en + greedy** is ~4× faster than small.en at WER 0.031 vs 0.000 (only the
long sentence differs: "how James approaches reliable" → "how James' approach
is reliable"); (c) on a **real Nigerian-English segment** (from the authorized
`Alimosho.m4a`), small.en and base.en produce the *same* transcript ("I enjoy
solving practical engineering problems"), i.e. no observed loss on real
Nigerian speech. Chosen default: **`base.en`, `beam_size=1`, initial_prompt**,
all env-configurable. `small.en` remains the documented fallback via
`VOICE_STT_MODEL`/`VOICE_STT_FALLBACK_MODEL`.

Limitations: n=5 synthetic + 1 real segment; a crude WER; no broad
Nigerian-accent corpus. This is directional, not a general accent claim.

## 3. Before/after latency (warm, real WebSocket path)

Definitions: EOS = `end_utterance` sent; transcript = `transcript_final`; first
audio = first `audio_frame` at the client. Host: Intel x86_64, Docker 2 vCPU;
**host load 8–23** during runs (noted per row).

| input | transcript | fast path | STT s | RAG 1st token s | TTS 1st s | EOS→first audio s |
|---|---|---|---|---|---|---|
| hello (baseline) | "Hello!" | no | 2.9 | 4.3 | 0.61 | **7.9** |
| hello (fixed, n=3) | "Hello." | **yes** | 1.3–1.8 | ~0 | 0.22–0.29 | **1.5, 1.8, 2.1** |
| hello (fixed, load spike) | "Hello." | yes | 3.4 | ~0 | 1.14 | 4.55 |
| hello, can you explain his RAG architecture? | exact | **no (guard)** | 6.5 | 9.2 | 1.63 | 17.8 |
| PostgreSQL question (was "PostGIRSQL") | **exact** | no | 2.0–3.3 | 5.6–14.0 | 0.5–0.7 | 9.9–17.1 |
| Chidera/Alimosho (was "Chidara/Alimasho") | **exact** | no | 1.0–3.0 | 6.4–9.2 | 0.3–2.0 | 10.6–13.7 |
| long architecture question | exact-ish | no | 2.8–5.3 | 5.1–6.6 | 0.26–1.1 | 9.4–13.5 |

Greeting fast path, separate from substantive questions: **EOS→transcript
1.3–1.8 s, EOS→first audible 1.5–2.1 s** (3 samples, p50 ≈ 1.8 s). Substantive
turns are bounded by the **remote RAG** (retrieval + LLM, 5–14 s on this
contended host) — unchanged and out of the voice service's control.

## 4. Changes (files affected)

- `app/stt.py` — `SttConfig` gains `initial_prompt`, `hotwords`, `temperature`
  (default 0.0), `condition_on_previous_text` (default False); `transcribe`
  passes them; `Transcript.avg_logprob` from segments; model load guarded by a
  `threading.Lock` (warm-up and a first turn share one model).
- `app/config.py` — defaults **`VOICE_STT_MODEL=base.en`**, **`VOICE_STT_BEAM_SIZE=1`**,
  a portfolio-vocabulary `VOICE_STT_INITIAL_PROMPT`, `VOICE_STT_HOTWORDS`,
  `VOICE_STT_TEMPERATURE`, `VOICE_STT_CONDITION_PREVIOUS`; `_getfloat` helper.
- `app/greeting.py` (**new**) — strict greeting fast path: whole-message exact
  match after normalization; confidence-gated (`avg_logprob` ≥ −0.9).
- `app/pipeline.py` — `run_turn(fast_reply=…)`: a returned string answers via
  the existing streaming/segmentation TTS path with **no RAG call**;
  `TurnResult.fast_path`.
- `app/main.py` — passes `fast_reply`; logs `fast_path`; `warm_models` sets a
  readiness event (even on failure); **`wait_until_warm()` gates
  `/v1/sessions`**; `/healthz` exposes `warm`.
- `components/TalkToJames.tsx` — new **`transcribing`** state shown immediately
  after `end_utterance`, distinct from thinking/speaking; included in
  disconnect/escape handling.
- `.env` / `.env.example` — measured STT defaults documented.
- Tests: `tests/test_greeting.py` (new), fast-reply pipeline test, config
  default assertions updated.

## 5. Greeting-path behavior and false positives

Deterministic, no LLM. `greeting_reply()` returns a short spoken reply only when
the **entire** normalized transcript is an exact known greeting ("hello", "hi",
"hey", "good morning", "how are you?", …, plus ", James"). Tested guards:
"Hello, can you explain my RAG architecture?", "Hi, what has James built with
AWS?", "Hey, how can I contact James?", "Good morning, what projects has he
shipped?" → all fall through to RAG (unit-tested and verified live). A
low-confidence transcript (`avg_logprob < −0.9`) never matches. The reply uses
the existing cloned voice; substantive questions stay RAG-grounded.

## 6. UI behavior changes

Immediately after end of speech the client now shows **"Transcribing…"** (spinner)
instead of a mislabeled "Thinking…", then "Thinking…" when the transcript is
final and RAG begins, then speaking/interrupted/error. No frozen or blank
screen during STT. The final transcript is shown only once finalized (no
speculative text presented as confirmed). Recognition failure surfaces the
existing error/retry path; no uncontrolled auto-retry, so quota is untouched.

## 7. Test commands and outcomes

- Voice service: `python3 -m unittest discover -s tests` → **140/140 PASS**
  (+7: greeting precision/false-positives/confidence, fast-reply bypasses RAG,
  updated config defaults); `py_compile` clean.
- Real WebSocket E2E: greeting (fast path) + substantive question pass; false
  positive ("hello, can you explain…") correctly uses RAG; barge-in → **0 stale
  audio frames**.
- Portfolio: `tsc --noEmit` clean; `eslint` clean; `next build` succeeds;
  vitest **576 passed**, 1 failed — the **pre-existing** `agent-sdr` PG15 case
  (and `cv-detailed` missing fixture), unchanged by this work.
- Cold/warm: `/healthz` reports `warm:false`→`true`; sessions now wait for it.

## 8. Remaining limitations

- **Substantive-turn latency is dominated by the remote RAG** (retrieval +
  LLM, 5–14 s here) — outside the voice service. The greeting target is met;
  the substantive p95 ≤ 4 s target is **not** achievable without RAG-side work.
- Measured on a **contended x86_64** host (load up to 23); not ARM/production.
  STT numbers swing 2–3× with load.
- Accuracy set is small (5 synthetic + 1 real Nigerian segment) with a crude
  WER; no broad Nigerian-accent corpus or human listening test was performed.
- The 20 s symptom was not reproduced at exactly 20 s in a controlled warm run
  (measured 7.9 s warm); the 20 s regime matches cold-load-under-contention,
  which the readiness gate + warm-up remove. Reported honestly rather than
  forced.

## 9. Recommended next step

Ship the STT change (`base.en` + beam 1 + initial prompt) and the greeting fast
path; both are measured wins with bounded accuracy impact. Next, attack the
**remote RAG** latency (the remaining dominant cost for substantive answers):
measure and tune the portfolio's retrieval (embedding round-trip + pgvector) and
LLM first-token, since that now dominates end-to-end time. Then re-measure on a
quiet host and the ARM target, and validate accent accuracy on a larger,
consented Nigerian-English set before any further model change.
