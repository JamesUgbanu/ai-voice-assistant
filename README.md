# james-voice-service

Self-hosted realtime English voice interface for jamesugbanu.com.
**Separate project, separate server, separate deploy** — this repository
shares no code, processes, filesystem, or database with the portfolio.

The portfolio RAG API remains the sole knowledge source. This service owns
microphone audio, speech-to-text, text-to-speech, turn state, voice
sessions, and quotas — nothing else.

## Status

Phase 1 (service skeleton, protocol, sessions, tests) implemented and
unit-tested. Hardware-dependent work is explicitly gated — see
“Phase 2 gate” below and DEPLOY.md. Nothing here is production-accepted
until measured on the target server.

## Architecture

```
Browser mic ──WebSocket/PCM16──▶ voice server ──HTTPS/SSE──▶ portfolio /api/rag
   (AudioWorklet)   turn state      faster-whisper STT ─┐
                    Silero VAD      Pocket TTS ─────────┴──▶ PCM16 chunks ─▶ browser queue
                                  SQLite sessions/quotas (WAL)
```

- RAG runs **only** on the portfolio server. This service holds no
  PostgreSQL credentials, no corpus, no embeddings, no retrieval code.
- No audio leaves the voice server except to the visitor's own browser.
  No recordings, no transcripts persisted (see Privacy).
- Voice-specific limits are enforced server-side; the browser timer is
  display only: 5-minute sessions, ~10 voice-minutes/day/visitor,
  one active session, bounded frames/turns/messages.

## Layout

```
app/
  main.py        FastAPI app: REST sessions, voice WebSocket, health/metrics
  config.py      Env-driven settings, safe defaults, no secrets in code
  protocol.py    v1 message envelope + audio framing validation (stdlib)
  auth.py        HMAC session tickets, visitor ids, salted IP hashes
  store.py       SQLite sessions/attempts/usage with transactional guards
  turns.py       Turn state machine (idle→collecting→processing→speaking)
  pipeline.py    STT→VAD→RAG→TTS orchestration with cancellation
  stt.py         faster-whisper adapter (lazy) + deterministic stub
  tts.py         Pocket TTS adapter (lazy) + deterministic stub
  vad.py         Silero VAD adapter (lazy) + deterministic stub
  rag_client.py  Portfolio RAG SSE client (injected transport for tests)
  logging_util.py  Structured logs with secret/PII redaction
tests/           stdlib unittest suite (no model packages required)
Dockerfile       CPU-only Python, non-root, healthcheck
docker-compose.yml  voice + Caddy TLS, resource caps, log rotation
Caddyfile        voice.jamesugbanu.com → voice service only
PROTOCOL.md      Full browser↔server protocol reference
INTEGRATION.md   Verified portfolio RAG contract + examples
DEPLOY.md        VPS, secrets, firewall, rollback procedures
```

## Quickstart (development)

Requires Python ≥ 3.11. Model packages are optional for unit tests.

```bash
cp .env.example .env   # then fill RAG URL/token + HMAC secret
python3 -m unittest discover -s tests -v
# With dependencies installed (needs network):
# pip install -e .            # CPU torch: see pyproject note first
# VOICE_USE_STUBS=1 uvicorn app.main:app --reload
```

Static checks: `python3 -m py_compile app/*.py tests/*.py`.

## Model verification status (Phase 2 gate — NOT YET RUN)

| Component | Candidate | License | Claim to verify on target CPU |
|---|---|---|---|
| STT | faster-whisper `small.en` → `base.en` | MIT (code + weights) | RTF, first-transcript latency, WER on Nigerian-accented English |
| VAD | Silero VAD | MIT | operating point (default threshold 0.5) on mic audio |
| TTS | Kyutai Pocket TTS 100M, `english` | MIT | RTF, first-audio latency, accent preservation vs authorized clip |

Pocket TTS facts established from Kyutai sources (Jan–Aug 2026): streaming,
~200 ms first chunk, ~6x realtime on M4 / ~2.3–2.5x on cloud x86 4vCPU,
cloning from ~5 s wav, `serve` HTTP mode, `export-voice` safetensors.
**Unmeasured:** ARM Neoverse single-thread throughput, int8 behavior on
this chip, accent fidelity for this voice. Do not deploy publicly until
the Phase 2 benchmark table in DEPLOY.md is filled with real numbers.

## Privacy

- No microphone audio persisted. No transcripts persisted.
- SQLite holds: opaque session/visitor ids, salted IP hash, timestamps,
  durations, statuses. No names, emails, audio, or text content.
- Logs carry session/turn/request ids only; secrets, tokens, cookies,
  audio, and transcripts are redacted or never emitted.
- The authorized reference voice clip (when supplied) lives outside the
  repo at `VOICE_TTS_VOICE_REF` and is never served over HTTP.
