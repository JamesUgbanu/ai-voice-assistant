# Voice-service infrastructure (`infra/`)

> **Reference infrastructure (portfolio-specific).** The defaults and the
> example `terraform.tfvars.example` reference the original deployment.
> Replace names, domains, network ranges, and account-specific values before
> using this with another application.

> **Rename compatibility note.** This module now belongs to AI Voice Assistant
> (`ai-voice-assistant`), but its defaults (`server_name`, `app_dir`, and project
> labels) retain the original `james-voice-service` identifiers so existing
> Terraform state and scheduled deployments are not disrupted. Override these
> values deliberately for a new stack; do not rename live resources without a
> migration plan.

Terraform for the reference **voice-service** host on **Hetzner Cloud**, designed
so the *application deployment* stays provider-neutral and a future VPS provider
can be added without rewriting the voice service.

> This directory only *provisions and configures a host*. It does not deploy the
> application, change DNS, or touch the portfolio RAG service. It never creates
> resources for the RAG service, and it keeps the voice service and RAG service
> on separate servers: the voice service keeps querying the existing remote RAG
> API as the sole source of portfolio knowledge.

---

## 1. What the voice service actually needs (derived from the repo)

Inspected from this repository, not assumed:

| Fact | Source |
|---|---|
| Entry point | `app.main:app`, run by `uvicorn --host 0.0.0.0 --port 8000 --workers 1` (`Dockerfile`) |
| Runtime | CPU-only Python 3.12 (`Dockerfile`); **no GPU** |
| STT | faster-whisper `base.en`, int8 / CTranslate2, CPU (`README` §8, `app/stt.py`) |
| TTS | Kyutai Pocket TTS ~100 M params, fp32, CPU; `preset` always, `clone` needs gated weights (`README` §8, `app/tts.py`) |
| VAD | Silero VAD via `torch.hub` (`README` §8) |
| Internal port | `8000`, `expose` only, never published (`docker-compose.yml`) |
| Public edge | Caddy publishes **80/443**, TLS for `voice.jamesugbanu.com`, proxies **only** `/v1/sessions`, `/v1/voice`, `/healthz`; `/metrics` stays internal (`Caddyfile`) |
| Health endpoint | `GET /healthz` → `{status, db, warm, tts}` (`app/main.py:190`) |
| Persistent state | Docker named volumes `voice-data` (SQLite sessions/quotas), `voice-models` (downloaded weights), `caddy-data`/`caddy-config` (TLS certs); optional read-only bind mount `voice-reference/processed` for clone mode (`docker-compose.yml`) |
| External dependency | remote HTTPS (SSE) RAG API — `POST {VOICE_RAG_API_URL}` with `Authorization: Bearer` (`app/rag_client.py`, `INTEGRATION.md`) |
| Required secrets | `VOICE_RAG_API_URL`, `VOICE_RAG_SERVICE_TOKEN`, `VOICE_HMAC_SECRET`; plus `VOICE_IP_SALT`, optional `HF_TOKEN` (`app/config.py`, `.env.example`) |
| Model download | ~5 GB on first boot into `voice-models` (`README` §4) |
| Verified architecture | **x86_64** (all measurements); **ARM is unmeasured** (`DEPLOY.md`, `README` §15) |
| Existing deployment doc | `DEPLOY.md` (host prep, compose, rollback); original Oracle-ARM plan is superseded but retained there |

This infrastructure does **not** change STT, TTS, voice cloning, RAG integration,
or audio transport. It reuses the existing `Dockerfile`, `docker-compose.yml`,
`Caddyfile`, and `.env` conventions unchanged.

---

## 2. Architecture: provider-specific vs provider-neutral

Three responsibilities are deliberately separated:

1. **Provider provisioning** — `providers/hetzner/` creates Hetzner resources
   (SSH key, firewall, server, optional volume) with the official
   `hetznercloud/hcloud` provider.
2. **Application bootstrap** — `modules/voice-server/` renders provider-neutral
   cloud-init (`cloud-init.yaml.tftpl`): installs Docker + Compose, creates the
   non-root deploy user and application directory, configures log rotation,
   optional swap, optional ufw, and SSH hardening. It contains **no secrets** and
   creates **no provider resources**; it also defines the normalized inbound
   firewall contract.
3. **Application deployment** — `scripts/deploy.sh` ships the repo over
   `ssh`/`rsync` and runs `docker compose build && docker compose up -d`. It
   uses **no provider API**, so the same artifact and procedure work on any
   compatible VPS.

```
infra/
├── versions.tf                 # Terraform + provider version constraints (+ backend docs)
├── providers.tf                # hcloud provider (token from HCLOUD_TOKEN)
├── main.tf                     # root: wires provider module
├── variables.tf                # root inputs + validations
├── outputs.tf
├── terraform.tfvars.example    # placeholders only
├── .gitignore                  # state/plans/tfvars/secrets
├── README.md                   # this file
├── modules/voice-server/       # provider-neutral contract
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── cloud-init.yaml.tftpl   # provider-neutral bootstrap
│   └── README.md
├── providers/hetzner/          # Hetzner implementation
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── versions.tf
└── scripts/
    ├── deploy.sh               # idempotent, provider-neutral app deploy
    └── health-check.sh         # reusable verification
```

There is a single Terraform root (`infra/`). Environments are selected with
`-var-file` (e.g. `prod.tfvars`) rather than duplicated root configurations.
If you later need fully isolated environments, use separate state backends/workspaces
(§9) instead of copying these files.

---

## 3. Prerequisites

- **Terraform** `>= 1.6` (validated locally with 1.9.8).
- **Hetzner Cloud provider** `hetznercloud/hcloud ~> 1.48` (installed 1.70.0
  at validation time; the lock file records the exact version).
- **CLI tools for deploying/checking** (NOT the provider API): `ssh`, `rsync`,
  `curl`, `python3`. `terraform` itself only needs network access to the
  Terraform registry and the Hetzner API.
- A **Hetzner Cloud account** with billing configured and an API token.
- An **SSH keypair** (public key content or an existing Hetzner key name).
- A **DNS provider** (manual for now; see §10) able to point
  `app_hostname` at the server.

---

## 4. Required credentials and secret handling

| Credential | Where it lives | Notes |
|---|---|---|
| `HCLOUD_TOKEN` | environment variable (preferred) | Never in files. `terraform`-only. |
| `VOICE_RAG_SERVICE_TOKEN` | server `app_dir/.env` (mode 600) | Uploaded by `deploy.sh --secrets`; never committed or printed. |
| `VOICE_HMAC_SECRET` | server `app_dir/.env` | Same. |
| `VOICE_IP_SALT` | server `app_dir/.env` | Same. |
| `HF_TOKEN` | server `app_dir/.env` | Only for clone mode (gated weights). |
| SSH private key | your machine (`~/.ssh`) | Never copied into the image or cloud-init. |

**Rules enforced in this configuration:**

- The provider token is a `sensitive` variable and is documented to come from
  the environment; `terraform.tfvars` is Git-ignored.
- **Cloud-init contains no secrets** (it only prepares the OS). Application
  secrets are injected at runtime by `deploy.sh` over SSH, not written into
  Terraform-managed user data.
- Terraform **outputs contain no secrets** (server IDs, IPs, hostname only).
- `deploy.sh` reads secrets from a local untracked file and installs them with
  mode 600; it never echoes values. `health-check.sh` passes the RAG bearer via
  curl's stdin config so the token never appears in argv or logs.
- Treat **Terraform state as sensitive**: the provider token can be present in
  state. State is Git-ignored; for shared use configure a remote backend (§9).

> Marking a variable/output `sensitive` only redacts CLI output — it does **not**
> keep the value out of state. That is why the token is expected via environment
> and why state must be protected.

---

## 5. Quick start (commands use the real layout)

Run everything from `infra/`.

```bash
# 1. Provider token (never write it into a file)
export HCLOUD_TOKEN="<your-hetzner-api-token>"

# 2. Local config
cp terraform.tfvars.example terraform.tfvars   # .gitignore excludes *.tfvars
$EDITOR terraform.tfvars                        # set ssh_allowed_cidrs, labels, etc.

# 3. Initialize (no remote backend by default)
terraform init

# 4. Format check + validate (no credentials, no network to Hetzner)
terraform fmt -check -recursive
terraform validate

# 5. Review a plan (requires a valid token; makes no changes)
terraform plan -var-file=terraform.tfvars

# 6. Provision — ONLY when explicitly authorized (§20)
terraform apply -var-file=terraform.tfvars
```

`terraform init -backend=false` is enough for `validate` without credentials.
`terraform plan`/`apply` require a real `HCLOUD_TOKEN`.

---

## 6. Variables

Defaults are starting points; see §7 for sizing rationale. All are validated.

| Variable | Default | Purpose |
|---|---|---|
| `environment` | `production` | Label/naming. |
| `server_name` | `james-voice-service` | Server name / hostname. |
| `server_type` | `cpx21` | vCPU/RAM/arch class. **Changing replaces the server.** |
| `cpu_architecture` | `x86_64` | Must match `server_type` (`cax*`=arm64). ARM unverified. |
| `image` | `ubuntu-24.04` | OS image (bootstrap targets apt/Debian). |
| `location` | `fsn1` | Hetzner location. **Changing replaces the server.** |
| `enable_ipv4` / `enable_ipv6` | `true` / `true` | Public stacks; at least one required. |
| `labels` | `{}` | Extra resource labels. |
| `ssh_port` | `22` | SSH port. |
| `ssh_allowed_cidrs` | *(required, no default)* | Trusted SSH sources. Must be non-empty. |
| `ssh_key_name` / `ssh_public_key` | `null` | Existing key name **or** public key content. |
| `app_hostname` | `voice.jamesugbanu.com` | Public hostname (TLS + checks). |
| `deploy_user` | `deploy` | Non-root app owner. |
| `app_dir` | `/opt/james-voice-service` | Remote app directory. |
| `public_web_ports` | `[80, 443]` | Public web edge. |
| `extra_firewall_rules` | `[]` | Extra normalized inbound rules. |
| `enable_host_firewall` | `false` | ufw in-guest (defense-in-depth; see §8). |
| `enable_ssh_hardening` | `true` | Disable SSH password auth. |
| `swap_size_mb` | `2048` | Swap headroom on small hosts (`0` disables). |
| `data_volume_size_gb` | `0` | Optional attached volume (see §9). |
| `extra_packages` | `[]` | Extra apt packages. |

---

## 7. Server sizing and architecture

### Model resource envelope (from the repo, NOT measured on Hetzner)

- **Weight floor ≈ 1.28 GiB** resident (torch + `base.en` int8 + Pocket TTS
  fp32 + clone state + Silero).
- **Warm idle RSS ≈ 1.30 GiB**, post-turn plateau **≈ 1.34–1.55 GiB**, measured
  peak **1.46 GiB** for two concurrent sessions (x86_64 dev host, `DEPLOY.md`).
- OS + Docker + Caddy add roughly **0.3–0.5 GB**.
- First boot downloads **~5 GB** of weights into `voice-models`.

### Why the historical 1 vCPU / 2 GB target is not the default

`DEPLOY.md` states the 1 CPU / 2 GB VPS is **NOT approved for production
traffic**: the stack fits only narrowly on paper, with no headroom, and nothing
was ever measured on the target host or on ARM. Inference is CPU-bound — TTS
RTF drops below 1× under contention, which is the measured "choppy audio" cause
Memory and CPU both bind on a 2 GB / 1 vCPU
box.

### Defaults chosen here

- **Default `cpx21`** (3 vCPU AMD x86_64 / 4 GB): meets the ~1.55 GiB RSS plus
  OS/build headroom and gives extra cores for CPU-bound TTS.
- **Recommended `cpx31` / `cx32`** (4 vCPU / 8 GB) for real headroom under
  concurrent sessions and smoother TTS.
- **Swap default 2048 MB** as cheap OOM insurance; set `0` on an 8 GB host.

These are **estimates**, not measured guarantees on Hetzner. Before public
traffic, fill the `DEPLOY.md` §6 benchmark table on the actual host (idle RSS,
peak under two concurrent sessions, 5-minute soak, no OOM) — the repo's §6
go/no-go gate stands.

### Architecture

Default is **x86_64** (Hetzner `cx*`/`cpx*`/`ccx*`). `cax*` is **arm64**. ARM is
unverified: `torch`, `CTranslate2`, `faster-whisper`, and the Pocket TTS runtime
must all support aarch64, and throughput is unmeasured. Only switch to
`cpu_architecture = "arm64"` + a `cax*` type after measuring the stack on ARM;
the variable validation prevents mismatches.

### Replacement semantics (important)

Changing `server_type`, `image`, or `location` forces **server replacement**
(the volume, if any, can be re-attached; see §9). `user_data` is create-only and
is intentionally ignored in the lifecycle to avoid a harmless template edit
silently destroying a running server (§9).

---

## 8. Security model

### Network (provider firewall is the PRIMARY control)

The Hetzner firewall is generated from the contract:

| Port | Source | Purpose |
|---|---|---|
| `ssh_port` (default 22) | `ssh_allowed_cidrs` only | administration |
| 80/tcp | `0.0.0.0/0` **and** `::/0` | ACME challenge + HTTP→HTTPS |
| 443/tcp | `0.0.0.0/0` **and** `::/0` | HTTPS/WSS |
| extra | operator-defined | deliberate only |

- Only 80/443 are exposed for the application; the app port **8000 is never
  published** (it is `expose`-only in compose).
- `/metrics` is not proxied by Caddy and stays inside the compose network.
- SSH is **never** open to the world by default: `ssh_allowed_cidrs` has no
  default and validation rejects an empty list.
- IPv6 is handled deliberately: when enabled, world rules include `::/0` so the
  v6 stack has the same coverage as v4 (no accidental open-IPv6 gap).

### Server

- **Key-only SSH**: password auth is disabled in the guest; the provider only
  injects SSH keys.
- The deploy user is **non-root**; the container also runs as the non-root
  `voice` user (unchanged from the image).
- **SSH hardening** (`enable_ssh_hardening = true`) writes
  `PasswordAuthentication no`, `PermitRootLogin prohibit-password`.
- Docker log rotation (`10m` × 3) is set at the daemon and in compose; the app
  logs structured JSON with secrets/PII redacted by construction.
- `restart: unless-stopped` in compose recovers containers after reboot; Docker
  is enabled at boot.
- `enable_host_firewall` (ufw) is **off by default**: the provider firewall is
  the real control and **Docker publishes bypass ufw's INPUT chain**, so ufw can
  create a false sense of protection. Enable only if you understand that caveat.

### Secrets

See §4. No secret is committed, printed, embedded in the image, or written into
cloud-init. The RAG bearer token lives only in the server's `app_dir/.env` and
on the portfolio side.

### TLS

Handled by the app's existing Caddy container (unchanged): automatic ACME for
`app_hostname`, TLS 1.2/1.3. The infrastructure only opens 80/443 and ensures
the hostname resolves. See §10 for prerequisites and migration timing.

---

## 9. Terraform state and resource lifecycle

- **State**: local `terraform.tfstate` in `infra/` by default (Git-ignored).
  **Local state has no locking.** For production/shared use, configure a remote
  backend in `versions.tf` (S3+DynamoDB, Terraform Cloud, GCS…) and run
  `terraform init -migrate-state`. Backend setup is intentionally separate from
  provider provisioning so state can be introduced without inventing resources.
- **Avoid wrong-account applies**: keep the `HCLOUD_TOKEN` for one account in
  scope, use distinct state keys/workspaces per environment, and always read
  `terraform plan` before applying.
- **Lock files** (`.terraform.lock.hcl`) are committed for reproducible provider
  versions; `.terraform/`, `*.tfstate*`, plans, and `*.tfvars` are ignored.
- **Lifecycle**: the only `lifecycle` setting is `ignore_changes = [user_data]`
  on the server, because cloud-init is create-only and a template tweak must not
  silently plan a replacement. There is **no blanket `ignore_changes`**;
  everything else is visible to the plan so drift and replacements surface.
- **`terraform destroy`** deletes the server, firewall, SSH key, and any
  volume. Consequences: **service outage**, loss of the SQLite session/usage DB
  (regenerable), re-download of ~5 GB of weights, loss of Caddy certs (re-issued
  automatically), and — if the volume is attached — **loss of the volume** unless
  you detach it first (`terraform state rm` / provider console).
- **Migration safety**: this configuration never destroys an old server as part
  of switching. Old and new hosts are separate states/workspaces (§11).

### Optional persistent volume

`data_volume_size_gb > 0` creates and auto-mounts a Hetzner volume (ext4,
`automount = true`). It is **off by default** because the only non-regenerable
asset is private reference audio and locally-produced voice models — weights
re-download and SQLite is quota bookkeeping. If you enable it, note that the
application's Docker named volumes still live under `/var/lib/docker`; using the
volume for `voice-models`/`voice-data` requires an explicit operator step
(relocate Docker's data-root or add a compose bind-mount override). This is
documented, not automated, to avoid silently changing the unchanged application
deployment.

---

## 10. DNS and TLS

DNS is **out of scope** for this configuration and **manual**, which keeps it
provider-neutral (Hetzner Cloud does not manage arbitrary DNS, and no DNS
credentials are required here).

Before first boot (ACME needs DNS to resolve):

```
A     voice.jamesugbanu.com  -> <public_ipv4 output>
AAAA  voice.jamesugbanu.com  -> <public_ipv6 output>   # if IPv6 enabled
```

- Caddy obtains and renews the certificate automatically once DNS resolves and
  80/443 are reachable.
- To move providers later, update the A/AAAA record to the new IP **only after**
  the replacement passes verification, and allow for DNS TTL propagation and a
  second ACME issuance (§11).
- Do not request certificates or change DNS as part of this task; no live
  changes were made.

---

## 11. Migrating to another VPS provider

The application artifact (`Dockerfile`, `docker-compose.yml`, `Caddyfile`,
`.env` conventions) and `scripts/deploy.sh` are provider-neutral. Moving
provider means **replacing the infrastructure implementation and provisioning a
replacement server**, not rewriting the app. Changing a `provider` variable
alone does **not** migrate a server: providers have different schemas and
lifecycles, so you implement the contract afresh.

### Extension points (already in place)

1. Add `infra/providers/<name>/` declaring that provider, consuming
   `../../modules/voice-server` (user-data + `firewall_rules`) and exposing the
   same outputs: `server_id`, `public_ipv4`, `public_ipv6`, `app_hostname`,
   `firewall_id`.
2. Add a `module "<name>"` block in `infra/main.tf`.
3. Reuse `modules/voice-server/cloud-init.yaml.tftpl` (any provider with
   cloud-init/user-data) and `scripts/deploy.sh` unchanged.

No changes to `modules/voice-server/`, the deployment scripts, or the
application are required.

### Migration procedure (safe, no automatic destruction)

1. **Identify a compatible VPS**: Linux (Ubuntu/Debian), **x86_64** (unless ARM
   is independently verified), ≥ 4 GB RAM (8 GB recommended), public IPv4/IPv6,
   SSH keys, provider firewall, ≥ ~20 GB disk for images + ~5 GB weights.
2. **Implement the new provider** under `infra/providers/<name>/` (see above).
3. **Reuse** the shared bootstrap and `scripts/deploy.sh`.
4. **Supply equivalent config/secrets** via that environment's `-var-file` and a
   local secrets file.
5. **Provision the new VPS while the Hetzner server keeps running.**
6. **Deploy the same artifact**: `deploy.sh --host <new-ip> --secrets <file>`.
7. **Transfer only the persistent data actually required** (inventory below).
8. **Verify**: health, TLS, authentication, STT, TTS, voice cloning, remote RAG.
9. **Run an end-to-end browser voice test** (mic → answer → interrupt → recover)
   against the new hostname or a temporary one.
10. **Switch DNS/traffic only after verification passes.**
11. **Monitor** the new host; **retain the old server** for a defined rollback
    window.
12. **Roll back** to the old server if validation fails (DNS change is reversible;
    the old host still has its state).
13. **Destroy the old server only after explicit human approval** and
    confirmation it is no longer needed. This config never removes the old host
    for you.

### Persistent-data inventory (what can be recreated vs. must be migrated)

| Asset | Location | Recreatable? | Migration |
|---|---|---|---|
| Model weights (~5 GB) | `voice-models` volume | Yes (re-download on first boot) | optional — copy to speed cutover |
| SQLite sessions/quotas | `voice-data` volume | Yes (bookkeeping only) | not required |
| Caddy certs | `caddy-data` volume | Yes (re-issued via ACME) | not required |
| Private reference audio (`voice-reference/processed/voice-ref.wav`) | host bind mount | **No** — Git-ignored, never in the image | **copy manually** if clone mode is used |
| `app_dir/.env` secrets | host file | **No** | **re-supply** via `deploy.sh --secrets` |

Do **not** assume the host is stateless: the reference recording for voice
cloning is private and must be copied by hand (it is excluded from the image and
from `deploy.sh`'s rsync).

### State during a provider migration

Do **not** copy Terraform state between incompatible providers. Create new
resources under a **separate state** (new workspace or `key`) for the new
provider; manage the old Hetzner state separately until you deliberately destroy
it. A provider switch is a new apply, not a state migration.

---

## 12. Application deployment (separate from provisioning)

Provisioning (Terraform) and application updates are decoupled so shipping a new
version does not recreate the VPS.

```bash
cd infra
# First deploy (installs .env, builds, starts; runs public health if --hostname given)
./scripts/deploy.sh --host <public-ip> \
  --secrets ../secrets/voice.env \
  --hostname voice.jamesugbanu.com

# Subsequent releases (same command; rsync ships only changes)
./scripts/deploy.sh --host <public-ip> --hostname voice.jamesugbanu.com
```

- Secrets come from a local, untracked file (`--secrets`), never from Terraform
  and never echoed. Required keys are checked locally before upload.
- `scripts/deploy.sh` is **idempotent**: rsync transfers only changed files and
  `docker compose up -d` recreates only changed containers. `--prune` (opt-in)
  adds `rsync --delete`.
- No Terraform provisioner is used for application deployment; provisioning and
  shipping are independent commands.

The `secrets/voice.env` file (see `infra/.gitignore`) must contain at least:

```
VOICE_RAG_API_URL=https://www.jamesugbanu.com/api/rag
VOICE_RAG_SERVICE_TOKEN=<same value as the portfolio's RAG_SERVICE_TOKEN>
VOICE_HMAC_SECRET=<32+ random bytes>
VOICE_IP_SALT=<random salt>
# HF_TOKEN=<...>            # only for clone mode
```

---

## 13. Health checks and observability

`scripts/health-check.sh` (any provider):

```bash
cd infra
./scripts/health-check.sh --host voice.jamesugbanu.com --require-warm
./scripts/health-check.sh --host voice.jamesugbanu.com \
  --rag-url https://www.jamesugbanu.com/api/rag --rag-token-file ../secrets/rag-token
```

It verifies: reachability; `GET /healthz` returns `200` with `status=ok` and
`db=true`; TLS validity (unless `--insecure`); and, optionally, RAG reachability
with a bearer token read from a file (never printed or placed in argv). It
deliberately does **not** treat a TCP connection as proof that STT/TTS/cloning/RAG
work — that requires a real browser turn.

**Limits / diagnostics** (all inside the repo, unchanged):

- `docker compose logs -f voice` — structured JSON, secrets redacted.
- `docker compose ps` — container health state.
- `GET /metrics` — counters only, **not** publicly proxied (inspect on-host).
- SQLite (`voice-data` volume) — inspect a copy, never expose the file.

**After a reboot**: containers use `restart: unless-stopped` and Docker is
enabled at boot; re-run `health-check.sh --require-warm` to confirm models
re-warmed.

---

## 14. Validation performed (local, no deployment)

Executed with Terraform v1.9.8 (darwin_amd64) against the real `infra/` layout:

| Check | Command | Result |
|---|---|---|
| Format | `terraform fmt -check -recursive` | **clean** |
| Init (no backend) | `terraform init -backend=false` | **success**; hcloud 1.70.0 installed |
| Validate | `terraform validate` | **Success! The configuration is valid.** |
| Plan (offline, example inputs, dummy 64-char token) | `terraform plan -var-file=<example>` | **3 to add, 0 to change, 0 to destroy** |
| Rendered plan resources | `terraform show -json` | `hcloud_ssh_key`, `hcloud_firewall`, `hcloud_server`; firewall = SSH from `203.0.113.4/32`, 80/443 from `0.0.0.0/0` + `::/0` |
| Cloud-init YAML (all-options branch) | `templatefile(...)` → Ruby `YAML.load_file` | **valid YAML**, 17 runcmd entries |
| Cloud-init YAML (minimal branch) | same | **valid YAML**, 7 runcmd entries |
| Shell syntax | `bash -n scripts/deploy.sh`, `bash -n scripts/health-check.sh` | **OK** |
| Secret review | grep over `.tf`/`.tftpl`/`.sh`/examples | **no hardcoded secrets**; only `hcloud_token` is `sensitive`; no secret outputs |
| Path/command review | manual | module paths, template paths, and README commands use the real layout |

Notes and limitations of this validation:

- `terraform plan` was run with a **dummy token of the correct length** and
  produced the plan **without contacting the Hetzner API** (image names resolve
  at apply time). It therefore does **not** prove that a real token/image/location
  combination is deployable; a real `plan`/`apply` needs a valid account.
- `terraform apply`, server creation, DNS changes, and live firewall changes were
  **not** run.
- No `tflint`/`terrascan` and no automated `terraform test` are included
  (not installed / not applicable).

---

## 15. Cost factors (estimates — verify current pricing)

Hetzner Cloud is hourly-billed with a monthly cap. **Estimates only** (EUR,
excl. VAT, subject to change; confirm on Hetzner's pricing page):

| Item | Estimate |
|---|---|
| Server `cpx21` (3 vCPU / 4 GB) | ~€8/mo |
| Server `cpx31` (4 vCPU / 8 GB) | ~€15/mo |
| Server `cx22` (2 vCPU / 4 GB) | ~€4/mo |
| Attached volume | ~€0.05/GB/mo |
| IPv4 (if billed separately) | ~€0.50/mo |
| Backups (if enabled) | ~20% of server |

Network egress: Hetzner includes 20 TB/mo on these classes (varies by location/
type). The self-hosted stack has **no per-call STT/TTS fees**; LLM cost stays on
the portfolio side.

---

## 16. Risks, blockers, and manual decisions

- **Production-traffic gate is unmet.** Per `DEPLOY.md`, the target host must
  pass the measured go/no-go (idle RSS ≤ ~1.5 GiB with margin, peak under two
  concurrent sessions, 5-minute soak, no OOM). **No Hetzner/ARM measurement
  exists.** Do not open public traffic before recording those numbers.
- **Architecture unverified.** x86_64 only; ARM is unmeasured. Keep
  `cpu_architecture = "x86_64"` unless you benchmark ARM yourself.
- **DNS is manual.** You must create A/AAAA records and let them propagate before
  first boot or ACME fails. No DNS credentials are required by this config.
- **Cloud-init packages assume Ubuntu/Debian** (`apt`, `docker.io`, distro
  compose package with an official-binary fallback). Other distros need a
  different bootstrap.
- **Hetzner image name resolution** happens at apply time; a valid token is
  required for a real plan/apply (the offline plan here used a dummy token).
- **Secrets management is process-based**, not a vault: `deploy.sh` copies a
  local untracked env file to the server. For stronger guarantees, extend the
  script to source from a secret manager.
- **Optional volume is not auto-wired** into Docker's data-root (documented
  caveat in §9).
- **`ignore_changes = [user_data]`** means cloud-init template changes do **not**
  apply to an existing server; a deliberate reprovision is required.

---

## 17. Future deployment commands (for a human, when authorized)

```bash
# One-time: point DNS at the server (manual), then allow propagation.

cd infra
export HCLOUD_TOKEN="<token>"
cp terraform.tfvars.example terraform.tfvars && $EDITOR terraform.tfvars

terraform init
terraform fmt -check -recursive && terraform validate
terraform plan -var-file=terraform.tfvars          # REVIEW before applying

terraform apply -var-file=terraform.tfvars         # provision (authorized only)

# Bootstrap finishes asynchronously (cloud-init installs Docker; a few minutes).
# Wait for /var/lib/voice-bootstrap-complete, then deploy:
./scripts/deploy.sh --host "$(terraform output -raw public_ipv4)" \
  --secrets ../secrets/voice.env \
  --hostname voice.jamesugbanu.com

# Verify:
./scripts/health-check.sh --host voice.jamesugbanu.com --require-warm \
  --rag-url https://www.jamesugbanu.com/api/rag --rag-token-file ../secrets/rag-token

# Then complete the browser voice test in the repo's manual checklist.
```

Change the VPS size: set `server_type` in `terraform.tfvars` and re-plan —
**expect replacement** (re-attach the volume if used, then redeploy). Change to
ARM: set `cpu_architecture = "arm64"` and a `cax*` type **only after** verifying
the stack on ARM.

---

## 18. Known limitations

- One root configuration; multiple fully-isolated environments require separate
  state backends/workspaces (documented, not pre-created).
- No DNS provider module (manual DNS by design).
- No automated `terraform test`; validation is `fmt`/`validate`/offline-`plan`
  plus rendered-YAML and shell checks.
- The application's `docker-compose.yml` resource caps are the repo's
  dev-planning values; tighten them deliberately on the target host (the repo
  says so; this task does not change them).
