# james-voice-service

Self-hosted realtime voice interface for jamesugbanu.com: microphone capture
in the browser, speech-to-text, text-to-speech in a cloned voice, and spoken
conversation over WebSocket — with barge-in (interrupt the assistant by
speaking) and a composer-dictation endpoint that reuses the same STT model.

**Separate project, separate server, separate deploy** — this repository
shares no code, processes, filesystem, or database with the portfolio. The
portfolio RAG API (`POST /api/rag`, SSE) remains the sole knowledge source;
this service owns audio, STT/TTS, turn state, sessions, and quotas.

## 1. Overview

Capabilities (all verified by the unit suite plus live browser/WS probes):

- Voice calls: mic → VAD → faster-whisper `base.en` → portfolio RAG → Pocket
  TTS → streamed PCM16 playback, with per-turn cancellation.
- Barge-in: sustained speech during playback stops the answer (client energy
  detector + server turn cancel), then the interruption is captured as the
  next turn. A manual Interrupt button does the same.
- Composer dictation: `POST /v1/transcribe` transcribes one recording with
  the same loaded Whisper model (short-lived HMAC ticket, no browser secrets).
- Deterministic greeting fast path: exact-match greetings answer locally
  without an RAG round trip.
- Quotas enforced server-side: 5-minute calls, daily allowance, one active
  session per visitor, per-IP attempt/rate budgets, bounded audio per turn.

Limitations: answer speed follows the remote RAG (LLM) latency; recognition
follows microphone quality (see Troubleshooting); the 1 CPU / 2 GB target is
met by reusing one loaded model per backend with serialized inference.

## 2. Architecture

```
Browser mic ──WS/PCM16 16 kHz──▶ voice server ──HTTPS/SSE──▶ portfolio /api/rag
 (AudioWorklet,               TurnState → Silero VAD → faster-whisper (+prompt,
  100 ms frames)                 hallucination guard) → greeting fast-path or
                              RAG token stream → segmentation → streaming TTS
                              ──WS/PCM16 24 kHz chunks──▶ browser queue ─▶ speakers
                              SQLite sessions/quotas (WAL); HMAC tickets
```

- Turn ids correlate STT → RAG → TTS → playback end to end; late frames from a
  superseded turn are discarded, never played.
- `interrupt` (button or voice-triggered) cancels the in-flight turn task,
  which closes the upstream RAG stream and stops the TTS producer — the whole
  obsolete pipeline, not just playback.
- One uvicorn worker (`--workers 1`); asyncio event loop + worker threads for
  blocking inference. In-process rate limiters and model singletons depend on
  the single worker — do not raise it without replacing them.
- Audio never leaves the voice server except to the visitor's own browser. No
  recordings or transcripts are persisted (see Privacy).

## 3. Repository structure

```
app/
  main.py            FastAPI app: REST sessions, /v1/transcribe*, voice WS,
                     health/metrics, warm-up gating, turn orchestration
  config.py          Env-driven settings with safe defaults (see §6)
  protocol.py        v1 envelope + audio-frame validation (stdlib only)
  auth.py            HMAC tickets, visitor ids, salted IP hashes (stdlib)
  store.py           SQLite sessions/attempts/usage, transactional (stdlib)
  turns.py           Turn state machine + sentence splitter (stdlib)
  pipeline.py        STT→VAD→RAG→TTS orchestration, cancellation, timings
  segmentation.py    Bounded RAG-token → speakable-segment buffer (stdlib)
  stt.py             faster-whisper adapter (lazy) + hallucination guard + stub
  tts.py             Pocket TTS adapter, preset/clone + stub
  vad.py             Silero VAD adapter (lazy) + stub
  rag_client.py      Portfolio RAG SSE client, injected transport
  greeting.py        Deterministic greeting fast path (stdlib)
  transcribe.py      Dictation validation + rate-limit windows (stdlib)
  voice_reference.py Reference-audio validation/normalization helpers
  logging_util.py    Structured JSON logs, secrets/PII redacted (stdlib)
tests/               stdlib unittest suite — no model packages required
scripts/prepare-voice-reference.py  Validate + normalize a reference recording
Dockerfile           CPU-only Python 3.12-slim, non-root `voice` user, healthcheck
docker-compose.yml   voice + Caddy TLS, volumes, caps, log rotation (production shape)
docker-compose.local.yml  Local-dev overlay: published port, host gateway,
                     voice-reference mount, localhost origins (NOT for production)
Caddyfile            TLS for voice.jamesugbanu.com; proxies only
                     /v1/sessions, /v1/voice, /healthz (`/metrics` stays internal)
PROTOCOL.md          Full browser↔server protocol reference
INTEGRATION.md       Verified portfolio RAG contract + examples
DEPLOY.md            Host prep, secrets, firewall, update/rollback procedures
docs/                Phase investigation reports (P2A–P2T): measurements and
                     calibration evidence referenced by code comments
voice-reference/     Private recordings (Git-ignored) + tracked README
```

## 4. Prerequisites

- Docker Engine + Compose plugin (for running the service).
- Python ≥ 3.11 **only** for the unit suite (stdlib — no installs needed).
- For model runs: ~5 GB disk for weights (first-run download into the
  `voice-models` volume), ~1.3 GB RAM floor for the loaded models, plus
  `ffmpeg`/`libsndfile1` (already in the image; needed locally only for the
  reference-prep script).
- A browser with microphone access for manual voice testing (Chrome
  recommended; headphones give the most reliable barge-in).

## 5. Quick start

```bash
cp .env.example .env   # then set the three required secrets (see §6)
python3 -m unittest discover -s tests        # 162 tests, stdlib only, ~5 s
docker compose up -d --build                 # build + start voice + Caddy
docker compose ps                            # voice healthy, caddy running
curl -fsS http://127.0.0.1:8000/healthz      # {"status":"ok","db":true,"warm":true}
```

First boot downloads weights (several minutes); `/v1/sessions` waits for
warm-up before answering. Static check without dependencies:
`python3 -m py_compile app/*.py tests/*.py`.

## 6. Configuration

All settings come from the environment (see `app/config.py`); only three have
no default and are required. Never commit real values (`.env` is Git-ignored).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `VOICE_RAG_API_URL` | yes | — | Portfolio RAG endpoint, e.g. `https://www.jamesugbanu.com/api/rag` |
| `VOICE_RAG_SERVICE_TOKEN` | yes | — | Bearer token the portfolio accepts (must equal its `RAG_SERVICE_TOKEN`) |
| `VOICE_HMAC_SECRET` | yes | — | Signs session/dictation tickets (≥32 random bytes) |
| `VOICE_IP_SALT` | no | ephemeral | Salt for hashed IPs in attempt buckets (set for stable buckets) |
| `VOICE_STT_MODEL` | no | `base.en` | faster-whisper checkpoint |
| `VOICE_STT_BEAM_SIZE` | no | `1` | 1 = greedy (fast); higher = slower, rarely better here |
| `VOICE_STT_CPU_THREADS` | no | `0` | CTranslate2 threads (0 = library default) |
| `VOICE_STT_INITIAL_PROMPT` | no | names/tech list | Domain biasing for proper nouns (see `config.py`) |
| `VOICE_STT_HOTWORDS` | no | empty | Extra hotwords (needs a faster-whisper with hotword support) |
| `VOICE_STT_TEMPERATURE` | no | `0.0` | Deterministic decoding; higher risks hallucinations |
| `VOICE_STT_CONDITION_PREVIOUS` | no | `0` | Keep `0`: turns are independent utterances |
| `VOICE_TTS_MODE` | no | `preset` | `preset` (built-in voice) or `clone` (authorized reference) |
| `VOICE_TTS_PRESET_VOICE` | no | `alba` | Built-in voice in preset mode |
| `VOICE_TTS_VOICE_REF` | clone only | empty | In-container path to prepared reference (mounted read-only) |
| `VOICE_TTS_LANGUAGE` | no | `english` | TTS language |
| `VOICE_TTS_MAX_WAIT_MS` | no | `1200` | Max wait for a natural segment boundary before speaking partial text |
| `VOICE_TTS_CLAUSE_TRIGGER_CHARS` | no | `80` | Clause fallback length |
| `VOICE_TTS_MAX_CHARS` / `VOICE_TTS_MIN_CHARS` | no | `400` / `12` | Segment size bounds |
| `VOICE_VAD_SOURCE` | no | `snakers4/silero-vad` | `local:/path` vendors it for reproducible deploys |
| `VOICE_TRANSCRIBE_MAX_SECONDS` | no | `30` | Max composer-dictation recording length |
| `VOICE_TRANSCRIBE_PER_MINUTE` | no | `20` | Dictation requests per IP per minute |
| `VOICE_TRANSCRIBE_TICKET_PER_MINUTE` | no | `30` | Dictation ticket mints per IP per minute |
| `VOICE_TRANSCRIBE_TICKET_TTL_SECONDS` | no | `300` | Dictation ticket lifetime |
| `VOICE_MAX_CALL_SECONDS` | no | `300` | Hard call length (5 min) |
| `VOICE_DAILY_ALLOWANCE_SECONDS` | no | `600` | Voice seconds per visitor per day |
| `VOICE_SESSION_ATTEMPTS` | no | `3` | Session creations per IP per window |
| `VOICE_SESSION_ATTEMPT_WINDOW_SECONDS` | no | `600` | Attempt window |
| `VOICE_STALE_SESSION_SECONDS` | no | `600` | Idle sessions reclaimed after this |
| `VOICE_MAX_AUDIO_FRAME_BYTES` | no | `192000` | Max single WS audio frame |
| `VOICE_MAX_TURN_AUDIO_SECONDS` | no | `60` | Max audio per turn |
| `VOICE_MAX_MESSAGE_BYTES` | no | `262144` | Max WS message |
| `VOICE_RAG_TIMEOUT_SECONDS` | no | `25` | Per-operation timeout on the RAG stream |
| `VOICE_DB_PATH` | no | `data/voice.sqlite3` | SQLite path (compose sets `/srv/voice/data/…`) |
| `VOICE_PUBLIC_BASE_URL` | no | empty | Origin used to build `ws_url` (`ws://`+path locally, `wss://` in prod) |
| `VOICE_ALLOWED_ORIGINS` | no | portfolio origin | Explicit browser origins (no wildcards) |
| `HF_TOKEN` | clone only | empty | Hugging Face token for gated Pocket TTS weights |
| `VOICE_USE_STUBS` | no | `0` | `1` = stub STT/TTS/VAD for offline runs (never production) |
| `VOICE_USE_STUBS` | no | `0` | `1` = stub STT/TTS/VAD for offline runs (never production) |

## 7. Local development

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build voice
curl -fsS http://localhost:8000/healthz     # warm:true once models load (~1–2 min)
curl -fsS http://localhost:8000/metrics     # operational counters only
```

- Voice API on `:8000`; portfolio dev server on `:3000` with
  `NEXT_PUBLIC_VOICE_ENABLED=1` and `NEXT_PUBLIC_VOICE_BASE_URL=http://localhost:8000`.
- RAG URL for local runs: the portfolio dev `/api/rag` with its bearer token.
- Warm-up loads all three models in a worker thread; sessions/turns wait for it.

## 8. Model setup

| Component | Artifact | Notes |
|---|---|---|
| STT | faster-whisper `base.en`, int8 (CTranslate2) | ~400 MB with workspace; beam 1 greedy at temp 0; `initial_prompt` restores proper nouns; a confidence + repetition guard rejects hallucinations (`app/stt.py`) |
| VAD | Silero VAD via `torch.hub` | ~4 MB; `VOICE_VAD_SOURCE=local:…` vendors it; 512-sample minimum window |
| TTS | Kyutai Pocket TTS ~100 M params, fp32 | ~550 MB + runtime; preset voice always works; **clone** needs the gated `kyutai/pocket-tts` weights (accept terms + `HF_TOKEN`) or the adapter raises instead of substituting |
| Runtime | CPU torch (Linux installs from the CPU index — see Dockerfile) | `OMP_NUM_THREADS=2`, `MALLOC_ARENA_MAX=2` bound threading/fragmentation |

Weights download on first run into `HF_HOME` (`voice-models` volume) and are
reused across restarts. Dependency floors are in `pyproject.toml`; there is no
lockfile (`uv.lock` arrives after hardware validation — pins today would be
fiction). Licenses: faster-whisper/CTranslate2 MIT; Pocket TTS MIT code with
**gated weights** (terms acceptance required for cloning); torch BSD-style.

## 9. Remote RAG integration

- `POST {VOICE_RAG_API_URL}`, JSON `{"question": <3..500 chars>, "history": [≤6 × {question ≤500, answer ≤2000}], "debug": false}`,
  header `Authorization: Bearer <token>`. See `INTEGRATION.md` for the verified
  contract — re-verify there before changing `app/rag_client.py`.
- The portfolio streams SSE (`meta` / `token` / `sources` / `done` / `error`);
  the voice service synthesizes from the first speakable segment (no full
  buffering) and closes the upstream request on cancel/timeout.
- Failures map to turn reasons, never fabricated text: transport/HTTP/empty
  answers surface as `rag_failed` and the caller ends the turn audibly clean.
- Troubleshooting: 401 → token mismatch between the two `.env` files; 429 →
  portfolio or voice rate limits (`/metrics`, portfolio logs); timeouts →
  check portfolio/LLM latency (voice answer speed follows RAG first-token);
  connection refused locally → portfolio dev server not on `:3000`.

## 10. Voice interaction

- Open the Ask James panel → Voice tab → Start talking (microphone permission
  is requested only on this explicit tap, never on page load).
- Speak, pause ~1 s: the client endpoints the utterance, the server
  transcribes, and the answer plays. Exact greetings ("hello", "hi", …) answer
  instantly without an RAG round trip.
- Interrupt anytime: the on-screen Interrupt button, or just start talking —
  sustained speech during playback stops the answer and your speech becomes
  the next turn. Headphones are most reliable; quiet speech below the room
  floor may not trigger voice interruption (use the button).
- Composer dictation (text chat mic) records up to 30 s and posts raw PCM to
  `/v1/transcribe` with a short-lived ticket; the transcript lands in the box
  for editing — never auto-sent.
- If you hear "I didn't catch that", the utterance was unrecognizable: move
  closer to the mic and repeat. "Voice hit a snag" means a pipeline failure;
  retry or switch to text.

## 11. Testing and validation

```bash
python3 -m unittest discover -s tests        # 162 tests, stdlib only (~5 s)
python3 -m unittest tests.test_stt_guard -v  # hallucination-guard contract
python3 -m py_compile app/*.py tests/*.py    # static check (no deps)
```

- Automated here: turn orchestration, cancellation (mid-RAG/mid-TTS),
  segmentation order/backpressure, protocol validation, auth/tickets, store
  guards, STT-guard thresholds on measured stats, VAD policy, privacy
  packaging guards. No model packages required.
- Live (needs Docker + models + portfolio): WS turn probes and real-browser
  mic→response→interrupt→recovery runs live outside this repo (portfolio
  harness); re-run them before any release. Synthetic-mic results never stand
  in for accent/AEC validation.
- Manual checklist: Hello→greeting reply; second question same session;
  interrupt mid-answer; silence during playback (no self-trigger); empty and
  failed turns show messages and recover.
- No ruff/pytest/coverage tooling is installed; `[tool.ruff]` in
  `pyproject.toml` documents the intended line length (100) for editors/CI.

## 12. Deployment

Production shape is `docker-compose.yml` + `Caddyfile` on a single host:

```bash
docker compose up -d --build          # build voice image, (re)create services
docker compose ps                     # voice healthy, caddy running
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS https://voice.jamesugbanu.com/healthz
docker compose logs -f voice          # structured JSON, secrets redacted
```

- DNS for `voice.jamesugbanu.com` must point at the host **before** first boot
  (ACME challenge), firewall 80/443 + SSH only (see DEPLOY.md).
- Caddy proxies **only** `/v1/sessions`, `/v1/voice`, `/healthz`; `/metrics`
  stays reachable inside the compose network, not the internet.
- Persistence: `voice-data` (SQLite) and `voice-models` (weights) volumes
  survive recreates; audio is never written to disk.
- Code changes require image rebuild + recreate (code is baked in; a bare
  restart is not enough). Rollback: `docker compose up -d` against the
  previous image tag (retag before upgrading if you need one-command revert).
- Resource envelope in compose is the dev-planning budget (`cpus: 2.0`,
  `mem_limit: 10g`); the production target is 1 CPU / 2 GB — re-measure
  (§8 floor ≈ 1.3 GB) and tighten these caps when deploying there. The 2 GB
  host is **not approved for production traffic** — see DEPLOY.md for the
  verdict, the strict ≥2 GB-headroom gate, and the evidence required.

## 13. Troubleshooting

| Symptom | Likely cause → check |
|---|---|
| Wrong short transcript (`Alo`, `Blo`) | Quiet/clipped mic input; guard now re-asks instead of answering. Move closer, repeat. |
| Repeated word loops (`No. No…`) | Decoder hallucination on ambiguous audio; rejected by the STT guard (`turn_end: empty`). |
| `I didn't catch that` every time | Input below the energy floor: OS input gain, mic selection, distance, browser tab muted for mic. |
| Answer starts then cuts | You interrupted (button or voice) — intended. Check `turn_end: interrupted`. |
| Thinking… for 15–30 s+ | Upstream RAG latency (LLM); voice answers at first audio. Check portfolio/LLM health. |
| Choppy/robotic voice | CPU contention (TTS needs realtime); check host load, `OMP_NUM_THREADS`, single worker. |
| No audio in browser | Turn never established (see `transcript_final`), AudioContext suspended (click the page once), output device muted. |
| `401/unauthorized` | `VOICE_HMAC_SECRET` mismatch or expired ticket (120 s session, 300 s dictation); clock skew. |
| `429 rate_limited` | Session attempts (3/10 min/IP), dictation windows, or portfolio limits; `/metrics` shows counters. |
| `409 active_session` | One live session per visitor; end the other call or wait out staleness (10 min). |
| WS closes (4403) | Browser origin not in `VOICE_ALLOWED_ORIGINS`. |
| Memory growth | Should plateau (~1.3–1.5 GB); `malloc_trim` runs per turn; investigate if `docker stats` climbs without bound. |
| `stt_failed`/`tts_failed`/`rag_failed` turn ends | Model load failure (weights/terms), RAG down/token, TTS ref missing — logs carry the stage, never internals. |
| Stale session blocking new calls | `recover_stale` reclaims idle rows; check `last_seen_at` and heartbeats. |

## 14. Security and cost controls

- Secrets live only in `.env` (Git-ignored, Docker-ignored, never logged):
  RAG token, HMAC secret, IP salt, `HF_TOKEN`. Rotate by editing `.env` and
  recreating the container.
- Tickets are short-lived HMACs (120 s session, 300 s dictation, scoped
  `dict:` subject); no jwt library, no lookup, constant-time compare.
- Abuse surface is budgeted: attempt windows, per-IP transcribe limits,
  bounded frames/turns/messages, 60 s max turn audio, single worker.
- Container runs as non-root `voice`; no wildcard CORS origins; missing
  `Origin` (non-browser) is accepted by convention; `/metrics` is
  counters-only and not publicly proxied.
- Cost: self-hosted CPU inference (no per-call STT/TTS fees); LLM spend, if
  any, lives on the portfolio side. Model downloads are one-time (~5 GB).

## 15. Known limitations and roadmap

Confirmed (measured, not aspirational):

- Answer latency follows remote RAG first-token (15–28 s observed under load);
  greetings bypass it.
- Recognition follows mic input quality; far/whisper-quiet fragments are
  rejected with a retry prompt rather than answered.
- Voice barge-in needs sustained voice clearly above room level; verified with
  synthetic mics — speaker-echo behavior is hardware-dependent (headphones
  recommended).
- Browser-hardware verification (accent accuracy, AEC, audibility on device)
  requires manual runs; synthetic probes do not transfer.
- No dependency pins yet (`uv.lock` pending hardware validation); ARM target
  throughput unmeasured; `DEPLOY.md` still names the original Oracle-ARM plan
  while the current target is a 1 CPU / 2 GB host — reconcile before release.
- Nigerian-English/Pidgin/Yoruba accuracy is **not** claimed as validated;
  proper-noun biasing via `initial_prompt` is measured on file audio only.

Remove `VOICE_SAVE_TURNS_DIR`-style local diagnostics (none present) and any
future `?voice-diag`-style client flags before release builds.
