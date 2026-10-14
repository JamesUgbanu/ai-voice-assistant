# P2L — Composer Dictation on Self-Hosted Whisper: Report

Scope: `Personal website/` composer dictation + `james-voice-service/` STT
endpoint. Voice conversation mode, streaming TTS, voice cloning, barge-in,
cancellation, quota/auth and the authenticated **text-only** RAG contract are all
preserved. No deploy, no commits.

## 1. Executive summary

- The composer mic was **not** using the self-hosted model at all: it was
  `SpeechRecognition` / `webkitSpeechRecognition` — the **browser vendor's cloud
  ASR** (Chrome → Google). Voice mode, by contrast, already ran
  `faster-whisper` `base.en` inside `james-voice-service`.
- Two STT paths existed for the same product surface. This change **removes the
  browser path** and routes dictation through a new, small, authenticated
  endpoint on the **already-loaded** Whisper model — no new model, no new
  dependency, no extra RAM, no extra CPU core.
- Applies the **same audio contract as the WS turn path**: raw PCM16 mono
  **16 kHz** (no server-side decode/resample). Auth is a **short-lived HMAC
  ticket** (`dict:` subject) — **no secrets reach the browser**.
- Inference is **serialized** behind a new `_infer_lock`, so dictation and voice
  turns share the single `base.en` model on one core safely.
- Verified end-to-end against real Whisper: `"Hello."` and
  `"Tell me about Chidera and Alimosho."` (Nigerian names exact); 401 without a
  ticket, 413 over the size cap. Voice mode + barge-in unaffected (**0 stale
  frames**). Tests: voice **150/150**, new portfolio dictation tests **12/12**,
  production build clean.

## 2. Audit finding (no code changed)

| path | engine | where |
|---|---|---|
| composer mic (before) | `SpeechRecognition`/`webkitSpeechRecognition` (browser vendor cloud ASR) | `lib/rag/voice.ts` `VoiceController`, used by `components/VoiceInput.tsx` |
| voice mode | `faster-whisper` `base.en` (CTranslate2) | `james-voice-service` `app/stt.py` `FasterWhisperBackend` |

Voice mode logged `stt_ms` ≈ 2.4 s/turn and reported backend `faster-whisper`;
the composer sent nothing to the self-hosted service. The two paths were
independent, so "voice input" behaved differently in Voice mode vs the composer.

## 3. Design

- **Transport mirrors the WS path.** The client captures 16 kHz PCM16 via an
  AudioWorklet and posts raw `Int16Array` bytes (`application/octet-stream`); the
  server consumes samples directly. No WAV/WebM/mp3 decode, no resample.
- **Bounded.** Size cap = `transcribe_max_seconds (30) × 16000 × 2` bytes →
  `413`; empty/too-small/pure-silence → `400`. Bounded stream read (never buffers
  an unbounded body).
- **Auth without browser secrets.** `POST /v1/transcribe/ticket` mints a
  short-lived HMAC ticket (`auth.issue_ticket`, subject `dict:<visitor>`, TTL
  `transcribe_ticket_ttl_seconds=300`, verified with a matching
  `max_age_seconds`). `POST /v1/transcribe` requires `X-Voice-Ticket` whose
  verified subject starts with `DICTATION_SUBJECT_PREFIX = "dict:"`; missing or
  wrong-scope → `401`.
- **Quota/rate limits.** Per-IP window limits on both endpoints
  (`transcribe_per_minute=20`, tickets `transcribe_ticket_per_minute=30`) via
  `WindowLimiter`; over-limit → `429`.
- **One model, one core.** `FasterWhisperBackend._infer_lock` wraps inference so
  dictation and voice turns serialize instead of oversubscribing the CPU.
- **Warm gating + memory hygiene.** The endpoint `await wait_until_warm()` then
  `asyncio.to_thread(stt.transcribe, …)` and `_trim_memory()`, matching turn
  handling.
- **UX unchanged.** Transcript populates the composer and is inserted **once at
  the end** (`composeTranscript(value, [text])`); **never auto-submitted** — the
  draft stays editable.

## 4. Changes

Server (`james-voice-service/`):

1. `app/transcribe.py` (new) — `DICTATION_SUBJECT_PREFIX`, `TranscribeError`,
   `validate_transcribe_audio(body, *, max_bytes, min_bytes=1600)`
   (`too_large`/`empty`/`bad_audio`, rejects pure silence), `WindowLimiter`.
2. `app/config.py` — `transcribe_max_seconds`(30), `transcribe_per_minute`(20),
   `transcribe_ticket_per_minute`(30), `transcribe_ticket_ttl_seconds`(300).
3. `app/stt.py` — `_infer_lock`; inference serialized in `transcribe()`.
4. `app/main.py` — `POST /v1/transcribe/ticket`, `POST /v1/transcribe`; CORS
   `allow_headers` includes `X-Voice-Ticket`; IP throttles; ticket-scope check;
   bounded read; warm gate; `asyncio.to_thread`; `_trim_memory`; metrics
   `transcribe_ok/unauthorized/rejected/too_large/ticket_rejected`; log
   `dictation_transcribed`.
5. `tests/test_transcribe.py` (new, 9 tests).

Client (`Personal website/`):

6. `lib/rag/dictation.ts` (new) — `DictationStatus`, `DICTATION_MAX_SECONDS=30`,
   `dictationBaseUrl()`, `isDictationSupported()`, `concatPcm16()`, exported
   `mintTicket()` / `postTranscript()` (testable HTTP), the 16 kHz capture
   worklet, and `DictationController` (toggle/stop/setDisabled/dispose/
   notifyManualEdit; ticket caching; max-duration cap; teardown on
   stop/cancel/error/unmount).
7. `components/VoiceInput.tsx` (rewritten) — uses `DictationController`; **all**
   `SpeechRecognition`/`webkitSpeechRecognition`/`VoiceController` usage removed;
   same props/UI (`value`, `onTranscript`, `disabled`; aria-labels "Start/Stop
   voice input"); privacy copy corrected (audio goes to the self-hosted server).
8. `tests/dictation.test.ts` (8) + `tests/dictation-controller.test.ts` (4).

Not done (deliberately): no model/dependency change, no second worker, no
server-side audio decoding, no client-side transcript submission.

## 5. End-to-end evidence (real Whisper)

Service rebuilt via the local overlay and warmed; curl against
`http://localhost:8000`:

| request | result |
|---|---|
| `POST /v1/transcribe` no ticket | **401** unauthorized |
| `POST /v1/transcribe/ticket` | ticket minted (108 chars) |
| `POST /v1/transcribe` hello.pcm (16 kHz PCM16) | `{"text":"Hello.","no_speech":false}` |
| `POST /v1/transcribe` nimoh.pcm | `{"text":"Tell me about Chidera and Alimosho.","no_speech":false}` |
| `POST /v1/transcribe` 2 MB body | **413** too large |

Names ("Chidera", "Alimosho") transcribe exactly — the same `base.en` +
`initial_prompt` as Voice mode.

## 6. Regression results

- **Voice mode:** real WS turn — transcript `"Hello."`, `reason=complete`, 48
  audio frames; barge-in probe **"no stale audio after interrupt — stale=0"**.
  The new `_infer_lock` introduced no deadlock (single turn completes).
- **Composer no longer uses browser ASR:** static test asserts `VoiceInput.tsx`
  contains no `SpeechRecognition`/`webkitSpeechRecognition`/`VoiceController`.
- **Audible path routes through the self-hosted service:** controller test drives
  capture → ticket → `POST /v1/transcribe` → `onTranscript("Ask: Hello.")`.
- **Cleanup:** tests assert mic tracks stopped and `AudioContext` closed on
  stop, permission error, and `dispose` (unmount).
- **Errors:** permission denial → "Microphone access is required…", too-short →
  "…didn't catch that" with **no** network call; server 401/413 surface a clean
  message.
- **RAG stays text-only:** PCM is posted only to `/v1/transcribe`; the composer
  handler does `setInput(text)` and **does not submit**. No audio reaches any
  portfolio API.
- **Tests/build:** voice **150/150 PASS**; portfolio dictation **12/12**;
  portfolio suite 588 pass (pre-existing unrelated failures: `cv-detailed`
  missing `/tmp/james_cv_detailed.txt`; `agent-sdr` #8 retrieval assertion —
  neither imports dictation/voice); `tsc --noEmit` clean; `next build` succeeds.

## 7. Resource budget

- **No new model, dependency or process** — dictation reuses the resident
  `base.en` backend. Added state is negligible (two throttle dicts).
- **CPU:** inference now holds `_infer_lock`, serializing dictation with voice
  turns rather than adding load — consistent with the 1 CPU / 2 GB target.
- Transcript latency under the current **host load 7–23** measured at
  `eou→transcript 24 s`, `eou→first audio 89 s` (voice turn) — dominated by host
  contention during image/build runs, **not** a code regression (idle
  `stt_ms` ≈ 2.4 s in P2J). Report as measured, with caveat.

## 8. Deployment / env notes

- Portfolio must be built with **`NEXT_PUBLIC_VOICE_ENABLED=1`** and
  **`NEXT_PUBLIC_VOICE_BASE_URL`** pointing at the voice service (e.g.
  `http://localhost:8000` local, the service origin in prod). Both are
  `NEXT_PUBLIC_*` → **build-time**, so rebuild the portfolio for them to take
  effect. CORS on the service must include the portfolio origin
  (`http://localhost:3000,http://localhost:3001` locally).
- The service must be rebuilt to add the endpoints. Docker Hub is unreachable
  here, so only the local overlay works:
  `docker build -f /tmp/p2b.Dockerfile -t james-voice-service:0.1.0 .` then
  `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
  --no-build voice`.
- New tunables (defaults in `app/config.py`): `transcribe_max_seconds=30`,
  `transcribe_per_minute=20`, `transcribe_ticket_per_minute=30`,
  `transcribe_ticket_ttl_seconds=300`.
- No secrets in the browser: the ticket is minted server-side; signing material
  stays in the service.

## 9. Remaining limitations and recommendations

- The ticket TTL (300 s) and `verify_ticket` window must agree — configured
  together here; if you change one, change the other.
- macOS host `MALLOC_ARENA_MAX` / `malloc_trim` are no-ops (glibc-only); the
  container env is what matters (P2K).
- Dictation uses the same `base.en` model, so its accuracy equals Voice mode; a
  future accuracy change should be made once, in the service, for both paths.
- Not covered by automation: a real browser mic → worklet → endpoint run
  (vendor `SpeechRecognition` cannot be automated and has been removed). The
  capture/POST/insert/cleanup flow is covered by the controller test with fakes;
  a manual click-through on a real device is the last check.

**Recommended next step:** on the target device, click the composer mic, say a
short phrase, and confirm the text lands in the box **without** auto-sending and
that the mic indicator clears on stop — the one remaining manual verification.
