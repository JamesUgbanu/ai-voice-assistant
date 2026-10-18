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
- Incremental decoding while speaking: rolling-window hypotheses
  (`transcript_partial`) remain internal; only a generic "Processing…" state is
  shown until the authoritative `transcript_final` and spoken answer arrive
  (see §10a and `tests/test_partial_stt.py`).
- Barge-in: sustained speech during playback stops the answer (client energy
  detector + server turn cancel), then the interruption is captured as the
  next turn. A manual Interrupt button does the same.
- Composer dictation: `POST /v1/transcribe` transcribes one recording with
  the same loaded Whisper model (short-lived HMAC ticket, no browser secrets).
- Listen to response: `POST /v1/tts` reads an existing assistant answer through
  the same TTS model; `POST /v1/tts/ticket` mints its short-lived HMAC ticket.
  This path never opens a microphone, runs RAG, or consumes a call session.
- Deterministic greeting fast path: exact-match greetings answer locally
  without an RAG round trip.
- Quotas enforced server-side: 5-minute calls, daily allowance, one active
  session per visitor, per-IP attempt/rate budgets, bounded audio per turn.
- Optional voice booking: captures server-provided slots from the RAG stream,
  collects missing name/email, requires explicit confirmation, and calls the
  portfolio's shared public `/api/booking` service. Booking state is local to the
  call; an uncertain result is never spoken as confirmed.

Limitations: answer speed follows the remote RAG (LLM) latency; recognition
follows microphone quality (see Troubleshooting). The production target is a
**4 GB x86-64 Hetzner host**; the measured x86-64 floor is ~1.3 GiB with one
loaded model per backend and serialized inference (evidence and the live
verification checklist are in `deploy/README.md`).

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
- While collecting, the growing audio is also fed to the incremental path
  (`partial_stt.py`), which emits replaceable `transcript_partial` events. These
  are internal; the UI never renders them. The turn's single authoritative
  decode still produces `transcript_final`.
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
  partial_stt.py     Rolling-window incremental STT (partials) + exact reuse
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
docker-compose.yml   local/dev/manual legacy shape (voice + Caddy TLS, volumes, caps)
docker-compose.local.yml  Local-dev overlay: published port, host gateway,
                     voice-reference mount, localhost origins (NOT for production)
Caddyfile            Legacy/manual-host TLS for voice.jamesugbanu.com; production
                     ingress is the Cloudflare Tunnel
PROTOCOL.md          Full browser↔server protocol reference
INTEGRATION.md       Verified portfolio RAG contract + examples
DEPLOY.md            Legacy long-lived-Compose runbook (not the scheduled path)
deploy/README.md     Authoritative production runbook (release, SSM, IPv4, Cloudflare Tunnel)
deploy/cloud-init.yaml  Bootstrap template (also bundled into the Lambda)
deploy/cost-estimate.py  Hetzner hourly-cost calculator (configured hours)
scripts/release.sh   Build/push the private linux/amd64 image; print digest
docs/                Historical phase reports (`P2*`, `VOICE-*`): measurements,
                     audits, and calibration evidence; not current guarantees
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
python3 -m unittest discover -s tests        # 262 tests (5 skipped), stdlib only
docker compose up -d --build                 # local/dev: voice + Caddy + host reference
docker compose ps                            # voice healthy, caddy running
curl -fsS http://127.0.0.1:8000/healthz      # db/warm true; tts reports the resolved mode
```

The Compose path mounts a host reference and is for local development only.
Production pulls the private image with the reference baked in (Option A); use
`deploy/README.md`, not this Compose file.

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
| `VOICE_STT_STREAMING` | no | `1` | `0` disables incremental partials (rollback switch) |
| `VOICE_STT_PARTIAL_INTERVAL_MS` | no | `900` | Debounce between partial decodes |
| `VOICE_STT_PARTIAL_MIN_AUDIO_MS` | no | `700` | Minimum audio before/growth between partials |
| `VOICE_STT_PARTIAL_MAX_AUDIO_SECONDS` | no | `15` | Stop new partials past this window (cost bound) |
| `VOICE_TTS_MODE` | no | `preset` | `preset` (built-in voice) or `clone` (authorized reference) |
| `VOICE_TTS_PRESET_VOICE` | no | `alba` | Built-in voice in preset mode |
| `VOICE_TTS_VOICE_REF` | clone only | empty | In-container path to prepared reference (mounted read-only) |
| `VOICE_TTS_LANGUAGE` | no | `english` | TTS language |
| `VOICE_TTS_MAX_WAIT_MS` | no | `1200` | Max wait for a natural segment boundary before speaking partial text |
| `VOICE_TTS_FIRST_MAX_WAIT_MS` | no | `600` | Shorter wait before the FIRST segment only, so speech starts sooner |
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
| `VOICE_BOOKING_API_URL` | no | derived | Portfolio booking API; derived from `VOICE_RAG_API_URL` when it ends in `/api/rag` |
| `VOICE_BOOKING_TIMEZONE` | no | `Africa/Lagos` | Timezone sent to RAG labels and read back during voice booking |
| `VOICE_BOOKING_TIMEOUT_SECONDS` | no | `20` | Booking API timeout; timeout is uncertain, never confirmed |
| `VOICE_DB_PATH` | no | `data/voice.sqlite3` | SQLite path (compose sets `/srv/voice/data/…`) |
| `VOICE_PUBLIC_BASE_URL` | no | empty | Origin used to build `ws_url` (`ws://`+path locally, `wss://` in prod) |
| `VOICE_ALLOWED_ORIGINS` | no | portfolio origin | Explicit browser origins (no wildcards) |
| `HF_TOKEN` | clone only | empty | Hugging Face token for gated Pocket TTS weights |
| `VOICE_USE_STUBS` | no | `0` | `1` = stub STT/TTS/VAD for offline runs (never production) |

Production provisioning sets `VOICE_TTS_MODE=clone` and the baked reference
path; clone readiness fails closed if `tts.cloning_active` is not true. Never
set `VOICE_USE_STUBS=1` in production.

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
| TTS | Kyutai Pocket TTS ~100 M params, fp32 | ~550 MB + runtime; **clone** is the production mode and needs the gated `kyutai/pocket-tts` weights (accept terms + `HF_TOKEN`); if the reference/weights are missing it raises instead of substituting. Preset mode is explicit local/dev configuration. |
| Runtime | CPU torch (Linux installs from the CPU index — see Dockerfile) | `OMP_NUM_THREADS=2`, `MALLOC_ARENA_MAX=2` bound threading/fragmentation |

Weights download on first run into `HF_HOME` (`voice-models` volume) and are
reused across restarts. Dependency floors are in `pyproject.toml`; there is no
lockfile (`uv.lock` arrives after hardware validation — pins today would be
fiction). Licenses: faster-whisper/CTranslate2 MIT; Pocket TTS MIT code with
**gated weights** (terms acceptance required for cloning); torch BSD-style.

## 9. Remote RAG integration

- `POST {VOICE_RAG_API_URL}`, JSON `{"question": <3..500 chars>, "history": [≤6 × {question ≤500, answer ≤2000}], "debug": false}`,
  header `Authorization: Bearer <token>`. The service sends the last six
  completed, uncancelled turns so voice conversations retain context; retries
  cannot duplicate them. See `INTEGRATION.md` for the verified contract —
  re-verify there before changing `app/rag_client.py`.
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
  transcribes, and the answer plays. The transcription itself is not shown;
  the panel indicates processing and then plays the reply. Exact greetings
  ("hello", "hi", …) answer instantly without an RAG round trip.
- Type in the composer while a call is active: the typed turn uses the same
  WebSocket, RAG/booking state, and TTS path. Completed text and voice turns
  share one transcript.
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

## 10a. Incremental transcription (live partials)

While the user is speaking, the server re-decodes the audio accumulated so
far on a debounce and emits `transcript_partial`; the hypotheses are internal
(the UI intentionally shows no live transcription). On `end_utterance` the
authoritative `transcript_final` is produced and only that is sent to RAG.

The decoder (faster-whisper) is **batch**, so this is approximate streaming,
not native incremental decoding — stated plainly:

- Each partial re-decodes the **whole utterance window so far** (never a
  suffix), so hypotheses are whole-sentence and nothing is stitched from
  overlapping windows (the duplicate/dropped-word failure mode). Cost grows
  with utterance length, so new partials stop once the window exceeds
  `VOICE_STT_PARTIAL_MAX_AUDIO_SECONDS` (the final decode still uses
  everything).
- The final transcript is always **one whole-utterance decode**. When the
  last partial happened to cover exactly that buffer (the client's ~900 ms
  silence endpoint leaves a window for this), the identical decode is reused
  so the final is immediate; otherwise the normal decode runs. Reuse is by
  exact byte length, never a suffix, so trailing words cannot be clipped.
- Partials are single-flight per connection and gated to one decode per
  process across sessions — a slow decode applies backpressure (skips, never
  queues), so audio cannot pile up. Failures are recoverable and never abort
  the turn.
- Rollback: `VOICE_STT_STREAMING=0` disables partials and reuse entirely;
  behavior returns to the pre-streaming path (final latency unchanged from
  before). See `tests/test_partial_stt.py` for the contract and
  `docs/P2U-LATENCY-REPORT.md` / `docs/P2Y-STT-LATENCY-REPORT.md` for related
  latency and capture measurements.

## 11. Testing and validation

```bash
python3 -m unittest discover -s tests        # 262 tests (5 skipped), stdlib only
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

Production is a disposable Hetzner x86-64 (4 GB target) server provisioned by
AWS Lambda + EventBridge Scheduler and booted from a **pinned private GHCR
image**. The image bakes the authorized reference (Option A), so the host never
receives it and no host bind mount may shadow it.

The authoritative runbook is `deploy/README.md` (release, GHCR, SSM secrets,
retained IPv4, Terraform, Cloudflare Tunnel, live checks). Scheduler detail
lives in `../terraform-voice-scheduler/DEPLOY.md`. Operating hours are changed with
`../terraform-voice-scheduler/scripts/set-voice-hours.sh show|plan|apply`; it
preserves the enabled/disabled state and never provisions a server.

High-level flow:

1. Publish `ghcr.io/jamesugbanu/james-voice-service:<immutable version>` as a
   private `linux/amd64` package and record the registry digest.
2. Create the eight SSM SecureStrings in `eu-west-2` (RAG bearer, HMAC, IP
   salt, HF token, Hetzner token, read-only GHCR user/token, Cloudflare Tunnel
   credentials).
3. Allocate the retained Hetzner Primary IPv4 outside Terraform; set
   `primary_ip_id`.
4. `terraform apply` creates the Lambda/schedules only; schedules stay
   disabled until a manual provision/teardown has been verified.
5. Route `voice.jamesugbanu.com` through the Cloudflare Tunnel (proxied
   CNAME); Cloudflare provides edge TLS. The retained IPv4 is for SSH/ops, not
   public ingress.
6. Invoke `provision`; the host pulls the pinned digest, pre-downloads models,
   starts the container, and `/healthz` must report `db=true`, `warm=true`, and
   `tts.cloning_active=true` in clone mode.

`db` in `/healthz` is the existing SQLite session/quota store, **not** an added
database. The Lambda treats `/healthz` as the readiness gate and fails closed on
a missing reference or inactive cloning.

Legacy `docker-compose.yml` + `Caddyfile` remain for local/dev and manual hosts;
`DEPLOY.md` is the older long-lived-Compose guide and is stale for the scheduled
lifecycle. Use `deploy/README.md` for production.

- Cloudflare Tunnel ingress allows only `/v1/sessions`, `/v1/voice`,
  `/healthz`, `/v1/transcribe`, `/v1/transcribe/ticket`, `/v1/tts`, and
  `/v1/tts/ticket`; `/metrics` is not routed. The app stays loopback-only; no
  inbound 80/443 is exposed. The TTS routes must be added to the deployed
  ingress before the Listen to response control is enabled in production.
- Cloudflare Tunnel is the TLS front; there is no origin certificate to renew.
  Do not expose port 8000 publicly.
- Rollback: pin the previous immutable version or digest and re-provision;
  never use `latest`/`stable` in production.

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

- Secrets live only in `.env` locally; production values live in SSM
  `SecureString` and are rendered into root-only `/etc/voice/runtime.env` at
  boot. The rendered cloud-init user-data is visible to the Hetzner account and
  instance root, so treat it as sensitive. Never commit or log secrets.
- Tickets are short-lived HMACs (120 s session, 300 s dictation, scoped
  `dict:` subject); no jwt library, no lookup, constant-time compare.
- Abuse surface is budgeted: attempt windows, per-IP transcribe limits,
  bounded frames/turns/messages, 60 s max turn audio, single worker.
- Container runs as non-root `voice`; no wildcard CORS origins; missing
  `Origin` (non-browser) is accepted by convention; `/metrics` is
  counters-only and is never publicly proxied (the production bootstrap also
  keeps the app loopback-only).
- **Option A biometric privacy:** the authorized reference is baked into the
  private image. Anyone with pull access can extract and reuse it. Keep the
  package private, issue read-only pull credentials to the server, and never
  mount or log the file. Clone readiness fails closed if the reference is absent
  or `cloning_active` is false.
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
- No dependency pins yet (`uv.lock` pending hardware validation). The
  production target is a 4 GB x86-64 host; ARM throughput is unmeasured. Root
  `DEPLOY.md` is the legacy long-lived-Compose guide; `deploy/README.md` is
  authoritative for the scheduled lifecycle.
- TLS is terminated by Cloudflare at the edge through the Tunnel; there is no
  origin certificate to renew or persist across daily server recreation. The
  retained IPv4 is not used for public ingress. The legacy Caddy/Compose path
  remains only for manual/local hosts.
- Nigerian-English/Pidgin/Yoruba accuracy is **not** claimed as validated;
  proper-noun biasing via `initial_prompt` is measured on file audio only.
- Incremental partials are **approximate** (whole-window re-decode, not native
  streaming): each partial costs CPU proportional to the audio so far, updates
  pause past `VOICE_STT_PARTIAL_MAX_AUDIO_SECONDS` (15 s default), and partial
  text may differ slightly from the final decode. They improve perceived
  latency only; the final transcript and RAG path are unchanged.

Remove `VOICE_SAVE_TURNS_DIR`-style local diagnostics (none present) and any
future `?voice-diag`-style client flags before release builds.
