# Deployment, operations, rollback

Target: Oracle Cloud Ubuntu ARM VPS (2 OCPU / ~12 GB RAM nominal —
**unverified; Phase 2 records actuals here before any public traffic**).

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

| Metric | small.en | base.en | Notes |
|---|---|---|---|
| STT RTF (CPU) | | | |
| Time to first transcript | | | |
| Pocket TTS time to first audio | | | |
| Pocket TTS RTF | | | |
| End of speech → first audible audio | | | |
| Idle RSS / peak RSS | | | |
| 5-min sustained run behavior | | | |
| Nigerian-accent WER / observations | | | |
| Voice similarity observations | | | |
| Go / no-go + concurrency | | | |

Go/no-go rule: TTS RTF > 1 with headroom on the target chip, first
audio < 1.5 s p50, STT usable on accented technical speech, RAM headroom
≥ 2 GB. Anything weaker → smaller/faster model first; no GPU, no hosted
voice APIs without an explicit approved trade-off.
