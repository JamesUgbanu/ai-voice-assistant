# Deployment, operations, rollback

> **LEGACY — NOT THE CURRENT DEPLOYMENT RUNBOOK.** This document describes the
> older long-lived Compose host. For the current scheduled Lambda + private
> GHCR + Cloudflare Tunnel lifecycle, see [deploy/README.md](deploy/README.md).
> The content below is retained for historical troubleshooting; the target and
> RAM verdict are stale.

Target: Oracle Cloud Ubuntu ARM VPS (2 OCPU / ~12 GB RAM nominal —
**unverified; Phase 2 records actuals here before any public traffic**).

> **Deployment-target status (added at audit; the lines above are retained
> for reference, not removed).** The current operator-confirmed target is an
> **InterServer VPS with 1 vCPU / 2 GB RAM** — a much smaller host than the
> Oracle plan. Nothing below has been measured on either target host; all
> numbers in §6 are x86_64 dev-host measurements. In particular:
> - The `cpus: 2.0` / `mem_limit: 10g` / `mem_reservation: 6g` values in
>   `docker-compose.yml` are **dev-planning caps, not requirements** — the app
>   runs a single worker and functions under any cap the host enforces, but
>   they were never tuned for a 2 GB host. They are intentionally **unchanged**
>   by this audit; tighten them deliberately when deploying there (see
>   “2 GB resource verdict” below), do not treat the current values as tested.
> - The go/no-go rule in §6 demanding “RAM headroom ≥ 2 GB” predates the
>   2 GB target and is unsatisfiable there as written — it needs operator
>   revision before it can gate a release. Left unchanged for the same reason.
>
> ## 2 GB resource verdict (measured evidence, x86_64 dev host)
>
> - Model floor: **~1.28 GiB** resident weights (torch + base.en int8 +
>   Pocket TTS fp32 + clone state + Silero). Cannot shrink without changing
>   precision/models.
> - Warm idle server RSS: **~1.30 GiB**; post-turn plateau **~1.34–1.55 GiB**;
>   measured peak **1.46 GiB** (two concurrent sessions). Live container at
>   audit time: ~1.40 GiB (`docker stats`), 1.54 GiB VmRSS post-turn.
> - Verdict: fits a 2 GB host **narrowly on paper** (OS + Docker + Caddy need
>   roughly 0.3–0.5 GB alongside it), with essentially no headroom and **no
>   measurement on the actual host or on ARM**. Do not send public traffic
>   until `docker stats` on the target confirms idle ≤ ~1.5 GiB with margin.
> - Non-negotiable on 2 GB: single uvicorn worker (`--workers 1`; in-process
>   rate limiters and model singletons assume it), one call's inference at a
>   time (already serialized), and no second model load (already singleton).
>   Consider `OMP_NUM_THREADS=1` on a single vCPU (currently 2).
>
> ## Production-traffic decision (2 GB VPS)
>
> The 2 GB VPS is **NOT approved for production traffic**. All memory figures
> above come from an x86_64 dev host with abundant RAM; none were measured on
> the target host, and ARM behavior is entirely unmeasured. Know the layers:
> **host RAM** (2 GB physical) vs **container limits** (`mem_limit` caps the
> container, `mem_reservation` only hints the scheduler — neither reserves or
> guarantees anything) vs **process RSS** (what the server actually holds:
> ~1.3–1.55 GiB) vs **model memory** (the ~1.28 GiB floor inside RSS) vs
> **available host memory** (what is left for OS + Docker + Caddy after the
> container takes its share — estimated, not measured).
>
> The §6 go/no-go rule requiring **RAM headroom ≥ 2 GB is kept strict and
> unchanged**: by definition the 2 GB host cannot satisfy it while running
> this stack, so the gate currently reads NO-GO for that host. It is not
> weakened here; an operator may only pass the gate with the evidence below.
>
> Evidence required to approve ANY target host (replace estimates with these):
>
> 1. `docker stats` idle after warm-up (target: container ≤ ~1.5 GiB).
> 2. Peak container and host memory under: cold start, warm-up, concurrent
>    transcription + TTS, two simultaneous sessions, and a 5-minute sustained
>    call (target: no OOM-kill, no container restart, host keeps clear margin).
> 3. `dmesg`/container restart count clean across the soak (no OOM events).
> 4. Cold-start timing (first-session wait) and EOS→first-audio p50 on the
>    target chip (ARM numbers are currently blank).
> 5. 24 h idle drift check (RSS flat, no growth without traffic).
>
> Until all five are recorded on the host itself, the host stays unapproved —
> regardless of how healthy the dev environment looks.

## 1. Host prep (once)

```bash
# Docker + Compose plugin (Ubuntu):
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER   # re-login after
# Firewall: allow 80/443 only (plus your SSH port).
# DNS: voice.jamesugbanu.com A/AAAA → this host. Verify propagation
# before first boot or Caddy cannot complete the ACME challenge.
```

## 2. Configure (never commit secrets)

```bash
cp .env.example .env
# Set: VOICE_RAG_API_URL, VOICE_RAG_SERVICE_TOKEN (must equal the
# portfolio's RAG_SERVICE_TOKEN), VOICE_HMAC_SECRET (32+ random bytes:
#   python3 -c "import secrets; print(secrets.token_hex(32))"),
# VOICE_IP_SALT, model choices after Phase 2.
chmod 600 .env
```

On the portfolio side (Vercel project settings): set `RAG_SERVICE_TOKEN`
to the same value. No portfolio redeploy of code is needed beyond the
bearer-auth change, which ships with the normal site deploy.

### Enabling the cloned voice (opt-in, separately reversible)

By default `VOICE_TTS_MODE=preset` speaks the built-in Pocket TTS voice
(`VOICE_TTS_PRESET_VOICE`, default `alba`) — a natural voice, but **not** the
cloned voice. To use the authorized reference on the host:

1. Copy the prepared reference (from `scripts/prepare-voice-reference.py`)
   to `./voice-reference/processed/voice-ref.wav` next to `docker-compose.yml`.
   It is Git-ignored and never baked into the image; the base compose mounts
   that directory read-only at `/srv/voice/voice-reference/processed`.
2. In `.env` set `VOICE_TTS_MODE=clone`,
   `VOICE_TTS_VOICE_REF=/srv/voice/voice-reference/processed/voice-ref.wav`,
   and `HF_TOKEN=<gated kyutai/pocket-tts token>`.
3. `docker compose up -d` (recreate) and verify mode:
   `curl -fsS http://127.0.0.1:8000/healthz | python3 -m json.tool` — expect
   `"tts": {"mode": "clone", "ref_configured": true, "cloning_active": true}`.
   If `cloning_active` is false, the clone weights/terms are not loaded or the
   reference file is missing; the adapter raises rather than substituting a
   generic voice (see `docs/P2F-NIGERIAN-ACCENT-REPORT.md` for the measured
   fidelity limit of this model).
4. Rollback: set `VOICE_TTS_MODE=preset` (or unset it) and recreate — no code
   change. This is independent of streaming STT (`VOICE_STT_STREAMING`).

## 3. Deploy

```bash
docker compose up -d --build
docker compose ps                       # voice healthy, caddy running
curl -fsS http://127.0.0.1:8000/healthz  # {"status":"ok",...} (on host)
curl -fsS https://voice.jamesugbanu.com/healthz
```

First boot downloads model weights (faster-whisper checkpoint + Pocket
TTS ~100M params + CPU torch); allow several minutes and ~5 GB disk.
Record exact artifact versions in the Phase 2 benchmark table below.

Health vs readiness: `/healthz` reports process liveness, SQLite reachability,
and a `warm` flag. Models warm in a background worker at startup, and
`/v1/sessions` waits (bounded, 180 s) for warm-up before answering, so the
first turn is not a cold load — but a healthy check alone still does not mean
a turn has ever succeeded. Pre-warm with one real turn (or a `/v1/sessions` +
short voice turn in staging) before opening traffic; model-load failures
surface as explicit turn errors, never silent stubs (`VOICE_USE_STUBS=1`
exists solely for offline unit tests).

## 4. Operate

- Logs: `docker compose logs -f voice` (JSON, secrets/PII redacted by
  construction; audio and transcripts never logged).
- Metrics: `GET /metrics` (counters only) — scrape or curl in checks.
- Sessions: SQLite at the `voice-data` volume; inspect with
  `sqlite3` on a volume copy, never by exposing the file.
- Updates: `docker compose pull/build && docker compose up -d`; in-flight
  calls drain via Caddy (old container stops after connections close).
- Secrets rotation: replace values in `.env`, `docker compose up -d`;
  voice bearer on the portfolio side rotates independently the same way.
  Old HMAC tickets expire within 120 s; old sessions re-auth on next call.

## 5. Rollback (tested order)

1. Portfolio: set `NEXT_PUBLIC_VOICE_ENABLED=false` (or remove) and
   redeploy — the voice button disappears; text assistant untouched.
2. `docker compose stop voice` — in-flight WebSockets close; SQLite
   retains settled usage so no quota is lost or double-counted.
3. To roll the voice service back one image: retag/redeploy the previous
   image (`james-voice-service:<prev>` — tag releases; `latest` is never
   used in production Compose files).
4. Portfolio RAG bearer change rolls back independently (it is additive
   and inert without the header); text clients never observe it.

Rollback must never expose secrets nor break the text assistant — both
properties hold by construction above and are covered by tests.

## 6. Phase 2 benchmark table (FILL WITH MEASURED NUMBERS ONLY)

x86_64 dev-host column filled at audit from P2I/P2J/P2K live runs (Docker
Desktop, host load varies — NOT the target host; ARM column stays blank until
measured on target).

| Metric | small.en | base.en | Notes |
|---|---|---|---|
| STT RTF (CPU) | | beam 1 ≈ 3–4× faster than small.en/beam 5, near-identical transcripts on fixed suite | P2I fixed-suite comparison |
| Time to first transcript | | ~1–2.5 s typical turn STT (`stt_ms` live: 1.1–5.2 s) | dev host, varies with load |
| Pocket TTS time to first audio | | ~0.2–0.8 s server-side (`tts_first_ms`) | segment-size dependent |
| Pocket TTS RTF | | realtime-plus on dev host; full-answer TTS ~11–77 s by length/load | host-load dominated |
| End of speech → first audible audio | | greeting ~2.15 s warm; substantive ~8 s (RAG-bound) | RAG first-token dominates |
| Idle RSS / peak RSS | | idle 1.30 GiB / peak 1.46 GiB (2 concurrent); floor 1.28 GiB weights | post-P2K, x86_64 |
| 5-min sustained run behavior | | RSS plateaus ~1.34–1.40 GiB, no leak; 0 stale frames on interrupt | P2K soak |
| Nigerian-accent WER / observations | | names exact on file audio (`Chidera`, `Alimosho`); no WER study | not validated — do not claim |
| Voice similarity observations | | clone active with authorized reference; no formal listening panel | |
| Go / no-go + concurrency | | single worker, serialized inference; 2 concurrent sessions measured | |

Go/no-go rule: TTS RTF > 1 with headroom on the target chip, first
audio < 1.5 s p50, STT usable on accented technical speech, RAM headroom
≥ 2 GB. Anything weaker → smaller/faster model first; no GPU, no hosted
voice APIs without an explicit approved trade-off.
