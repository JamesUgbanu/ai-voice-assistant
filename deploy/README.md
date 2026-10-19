# Voice service — production deployment and operations runbook

> **Reference deployment (portfolio-specific).** This runbook describes the
> original scheduled deployment. Replace the GitHub owner, domains, DNS
> records, SSM paths, and secrets before adapting it. It is not required to
> use the voice service.

> **Rename compatibility note.** The project is now AI Voice Assistant
> (`ai-voice-assistant`), and new releases use the internal `voice-service`
> image name. This runbook is retained as historical production evidence:
> identifiers created by the original deployment, including the
> `james-voice-service` image and `james-voice` tunnel/cloud resources, are not
> renamed retroactively. Update `VOICE_IMAGE` deliberately when migrating an
> existing host.

This is the authoritative runbook for the scheduled, disposable Hetzner
deployment. The root `DEPLOY.md` describes an older long-lived-Compose setup and
is **not** the production path.

## Deployment status (update as work progresses)

| Item | State | Evidence / note |
|---|---|---|
| Voice unit tests | ✅ passing | 262 tests, 5 skipped (`python3 -m unittest discover -s tests`) |
| Scheduler unit tests | ✅ passing | 88 tests (`lambda/tests`) |
| Local image | ✅ built | `james-voice-service:0.2.0-local`, `linux/amd64`, 2.11 GB, ID `sha256:e83f63fb…` |
| Reference in image | ✅ verified | 1,440,078 bytes, mode 600, owner `voice`; contents never printed |
| Image published to GHCR | ✅ published | `0.2.0`, private, amd64; digest `<image-digest>` |
| GHCR authentication | ✅ browser | `gh auth login --web`; account `JamesUgbanu`; Docker logged in via credential helper |
| SSM parameters | ✅ created | 8/8 in `eu-west-2`, including `/voice/cloudflared-credentials`; values never printed |
| Hetzner pricing | ✅ verified | API `GET /pricing` (2026-10-17): USD, VAT 0; cx23 $0.0104/h; cpx21 $0.0176/h; IPv4 $0.60/mo |
| Server type choice | ✅ `cx23` | 2 vCPU / 4 GB / 40 GB; $0.0104/h; $5.69/month at 489.7 h with IP |
| Retained Primary IPv4 | ✅ allocated | id `<primary-ip-id>`, `<primary-ipv4>`, fsn1; retained across teardown; currently assigned to the live server; $0.60/month |
| Terraform plan | ✅ applied | 18 resources; the server is ephemeral and provisioned by schedule |
| AWS resources | ✅ verified | Lambda Active (256 MB, 900 s, reserved 1); 4 schedules ENABLED, Africa/Lagos; 7-day logs; 2 alarms |
| TLS approach | ✅ Cloudflare Tunnel | Cloudflare edge TLS; outbound-only; no certs/volume |
| DNS migration | ✅ active | Cloudflare NS `serenity`/`ryan`; 11 records; apex/`www` 200; Brevo/DKIM/DMARC intact |
| Tunnel integration | ✅ applied | tunnel `james-voice` routed; credentials in SSM; Lambda/IAM updated; ingress validated |
| Live verification | ✅ passed | health `db/warm/cloning_active`; WS STT→RAG→cloned TTS; `/metrics` 404 |
| Cold start | ✅ 3 min 33 s | clean provision; first attempt failed on Cloudflare UA 403, fixed in the health probe |
| Teardown / IP | ✅ verified | server deleted; IPv4 `<primary-ipv4>` unassigned after ~15 s |
| HTTPS | ✅ verified during test | Cloudflare Tunnel edge TLS; no origin certificate |
| Schedules | ✅ ENABLED | weekdays 06:00–23:00, weekends 08:00–22:00 Africa/Lagos; DB revision 3; next open Sun 08:00 |
| Host RAM/disk | ⚠️ unverified | SSH access to Hetzner key `<hetzner-ssh-key-name>` was not available in the verification environment |

The deployment is live; the ⚠️ row is a measurement follow-up, not a readiness blocker.

## 1. Prerequisites

- macOS with Docker Desktop. The trusted local builder is `desktop-linux`; the
  `default` builder on this machine is broken (`Cannot connect to the Docker
  daemon at unix:///var/run/docker.sock`).
- Python ≥ 3.11 (stdlib only for tests).
- AWS CLI authenticated to account `<aws-account-id>`, region `eu-west-2`.
- Terraform ≥ 1.11 (verified 1.15.2).
- A GitHub account with `write:packages` on `jamesugbanu`.
- A Hetzner Cloud API token with access to the target project (kept in SSM;
  never in source).
- `curl`, `openssl`; optional `hcloud` CLI for IP inspection.

### Placeholders and operator inputs

Values shown as `<...>` are intentionally not published. An authorized operator
obtains them from the live account; never commit or publish them:

- `<aws-account-id>`: `aws sts get-caller-identity --query Account --output text`
  after authenticating the AWS CLI.
- `<primary-ip-id>` / `<primary-ipv4>`: Hetzner Console → Primary IPs (or the
  Hetzner API) for the retained IPv4.
- `<hetzner-ssh-key-name>` / `<hetzner-ssh-key-id>`: Hetzner Console → SSH keys.
- `<image-digest>` / `<registry-digest>`: printed by `scripts/release.sh` and
  `docker buildx imagetools inspect`.
- `<tunnel-id>`: `cloudflared tunnel list` / tunnel info.
- `<voice-repo>`: path to this checkout; scheduler commands use the sibling
  `../terraform-voice-scheduler` checkout.

## 2. Workspace and Git hygiene

```bash
cd <voice-repo>
git status --short          # preserve all existing work; never reset/clean
git check-ignore -v deploy/reference/voice-ref.wav
```

The reference must be git-ignored and must never be copied into a build context
other than this repository's private image. Verification prints only byte size:

```bash
wc -c deploy/reference/voice-ref.wav      # 1440078
```

## 3. GHCR authentication (interactive)

Publishing is authorized, but the package must stay **private** and owned by
`jamesugbanu`.

```bash
docker login ghcr.io        # complete the browser/terminal prompt yourself
```

Verify without printing credentials:

```bash
printf 'protocol=https\nhost=ghcr.io\n\n' | docker-credential-desktop list
```

A `ghcr.io` entry means the login succeeded. Do not paste a token into chat or
shell history. If the credential helper stores it, leave it there.

Log out when finished publishing:

```bash
docker logout ghcr.io
```

## 4. Build and verify the local image

Use the release script's build path with the trusted local builder
(`BUILDER` defaults to `desktop-linux`; never a remote builder):

```bash
cd <voice-repo>
docker buildx build --builder desktop-linux --platform linux/amd64 \
  -t james-voice-service:0.2.0-local --load .
```

Verify architecture, user, reference path and permissions (never print audio):

```bash
docker image inspect james-voice-service:0.2.0-local \
  --format '{{.Architecture}} {{.Os}} user={{.Config.User}} id={{.Id}}'

docker run --rm --platform linux/amd64 --entrypoint sh \
  james-voice-service:0.2.0-local -c \
  'p=/srv/voice/voice-reference/processed/voice-ref.wav; \
   [ -s "$p" ] && stat -c "bytes=%s mode=%a owner=%U" "$p"'

docker run --rm --platform linux/amd64 --entrypoint sh \
  james-voice-service:0.2.0-local -c 'id'
```

Expected: `linux amd64`, user `voice`, reference `1440078` bytes, mode `600`,
owner `voice`, and uid/gid `999(voice)` (not root).

## 5. Publish the private image and record the digest

`scripts/release.sh` runs the tests, rebuilds/pushes `linux/amd64`, and prints
the registry digest. It refuses `latest`/`stable` and requires the reference
preflight.

```bash
cd <voice-repo>
GHCR_OWNER=jamesugbanu VERSION=0.2.0 scripts/release.sh
```

Record the printed `sha256:…` digest. Verify the manifest and that the package
is private (an anonymous request must be rejected):

```bash
docker buildx imagetools inspect \
  ghcr.io/jamesugbanu/james-voice-service:0.2.0

curl -s -o /dev/null -w '%{http_code}\n' \
  https://ghcr.io/v2/jamesugbanu/james-voice-service/manifests/0.2.0
# expect 401 (private)
```

If `0.2.0` already exists, do not overwrite it; publish the next semantic
version after reviewing the diff.

Pin production to the **digest**:

```text
ghcr.io/jamesugbanu/james-voice-service@sha256:<registry-digest>
```

## 6. AWS identity and region

```bash
aws sts get-caller-identity --query Account --output text   # <aws-account-id>
aws configure get region                                    # eu-west-2
aws ssm get-parameters-by-path --path /voice --recursive \
  --query 'Parameters[].Name' --output text
```

The Terraform default is now `eu-west-2`. Do not apply into another region by
accident.

## 7. Create SSM parameters securely

| Name | Type | Consumer |
|---|---|---|
| `/voice/hetzner-token` | SecureString | lifecycle Lambda (Hetzner API) |
| `/voice/rag-service-token` | SecureString | voice server (RAG bearer) |
| `/voice/hmac-secret` | SecureString | voice server (tickets) |
| `/voice/ip-salt` | SecureString | voice server (hashed IPs) |
| `/voice/hf-token` | SecureString | voice server (gated Pocket TTS weights) |
| `/voice/ghcr-user` | String | voice server (read-only pull) |
| `/voice/ghcr-token` | SecureString | voice server (read-only pull) |
| `/voice/cloudflared-credentials` | SecureString | Lambda → bootstrap cloudflared |

Use the default `alias/aws/ssm` key. Values must never be committed, printed,
logged, or placed in Terraform source/state. A safe local pattern (prompts
without shell history; the secret is written to a 0600 temp file, not argv):

```bash
name=/voice/rag-service-token
umask 077
read -rsp "value for $name: " v; echo
python3 - "$name" "$v" > /tmp/ssm-param.json <<'PY'
import json, sys
print(json.dumps({"Name": sys.argv[1], "Type": "SecureString",
                  "Value": sys.argv[2], "Overwrite": True}))
PY
unset v
aws ssm put-parameter --cli-input-json file:///tmp/ssm-param.json >/dev/null
rm -f /tmp/ssm-param.json
aws ssm get-parameter --name "$name" --query 'Parameter.Name' --output text
```

Repeat for each parameter. `HF_TOKEN` must belong to an account that accepted
the gated `kyutai/pocket-tts` terms. The GHCR token must be a **read-only**
`read:packages` token for the same owner.

Terraform does not create the legacy `/voice/snapshot-id` parameter. The
snapshot-based `start`/`stop` actions are unscheduled and require you to create
that parameter manually if you deliberately use them.

**Approval gate:** creating/changing production secrets requires explicit
approval. Show names/types/consumers and confirm before running the writes.

## 8. Hetzner token and retained Primary IPv4

The Hetzner token is used by the Lambda, never by the voice server. Create it
in the Hetzner console and store it as `/voice/hetzner-token`.

Allocate **one** retained Primary IPv4 in the same location as the server
(`fsn1` by default) in the Hetzner console (Primary IPs → Create → IPv4) and
record its numeric ID. It must survive server deletion; the code only assigns
and verifies it, never deletes it.

Verify pricing before allocation. The Hetzner API `GET /pricing` returned
**USD**, `vat_rate = 0`, and these fsn1 rates on 2026-10-17:

| Item | Rate | 489.7 h/month |
|---|---|---|
| `cx23` (2 vCPU, 4 GB, 40 GB) | $0.0104/h | $5.09 compute |
| `cpx21` (3 vCPU, 4 GB, 80 GB) | $0.0176/h | $8.62 compute |
| Retained Primary IPv4 (fsn1) | $0.60/month | $0.60 |
| **cx23 total** | | **$5.69** (current schedule) |
| **cpx21 total** | | **$9.22** (above $3) |

`cx23` is the cheapest 4 GB x86 option and meets the target; `cpx21` adds a
third vCPU and more disk but exceeds it. The default Terraform value is still
`cpx21`; changing it requires your explicit choice. `cpx11` (2 GB) is cheaper
but below the approved 4 GB memory target.

**Approval gate:** the IP is billable. State the region, recurring charge,
one-time charges, why it is needed, and the rollback (release the IP only with
separate approval; the scheduled lifecycle never releases it).

## 9. Terraform: init, validate, plan

```bash
cd ../terraform-voice-scheduler
cp terraform.tfvars.example terraform.tfvars   # if not present
$EDITOR terraform.tfvars                       # non-secret values only
terraform init
terraform fmt -check -recursive
terraform validate
terraform plan -out=tfplan
```

Review every resource. Expected active-path resources: Lambda, its role/policy,
scheduler role, four EventBridge schedules (DISABLED while
`enable_schedules=false`), log group, and two alarms. No server or snapshot
parameter is created by the apply.

Set at minimum:

```hcl
aws_region        = "eu-west-2"
image_ref         = "ghcr.io/jamesugbanu/james-voice-service@sha256:<digest>"
primary_ip_id     = <primary-ip-id>
health_url        = "https://voice.jamesugbanu.com/healthz"
enable_schedules  = false
manage_hetzner_token_param = false   # token created in SSM
ssh_key_ids       = [<hetzner-ssh-key-id>]  # recommended for operations
```

**Approval gate:** do not run `terraform apply` until the plan has been shown
and approved. If the plan changes materially, re-present it.

## 10. Apply and verify AWS resources

```bash
terraform apply tfplan
aws lambda get-function --function-name voice-scheduler \
  --query 'Configuration.[FunctionName,State,Timeout,MemorySize]' --output text
aws scheduler list-schedules --query 'Schedules[].Name' --output text
```

Schedules must be `DISABLED` at this stage of a first rollout (the current
deployment is enabled). Confirm the Lambda's environment
references parameter **names**, not values, and that the IAM policy reads only
the `/voice/*` parameters it needs.

## 11. DNS and TLS — Cloudflare Tunnel (active)

Active approach: **Cloudflare Tunnel**. Cloudflare terminates public TLS;
`cloudflared` makes outbound-only connections from the server. The origin needs
no inbound 80/443, no Caddy, and no certificate storage — avoiding both daily
ACME reissuance and a persistent volume.

Verified on 2026-10-17:

- Tunnel is available on **all Cloudflare plans**; account limits are 1,000
  tunnels and 50,000 routes. One tunnel and one public hostname are sufficient.
- WebSockets are supported on all plans; enable **Network → WebSockets**. The
  voice client sends continuous audio frames; add a heartbeat if idle gaps occur.
## 12. First provision and live verification

After the image, SSM values, IP, Terraform apply, and Cloudflare Tunnel are ready:

```bash
aws lambda invoke --function-name voice-scheduler \
  --payload '{"action":"provision"}' \
  --cli-binary-format raw-in-base64-out /dev/stdout
```

The Lambda renders cloud-init from SSM, creates the server with the retained IP,
and polls `health_url` until:

- `db=true` (SQLite session/quota store reachable; **not** an added database),
- `warm=true` (models loaded),
- `tts.cloning_active=true` (clone mode; missing reference/inactive cloning
  fails closed).

Verify on the server (SSH key required):

```bash
curl -fsS http://127.0.0.1:8000/healthz | python3 -m json.tool
docker stats --no-stream
df -h /srv/voice
docker logs --tail 100 james-voice
```

Public checks after TLS:

```bash
curl -fsS https://voice.jamesugbanu.com/healthz
curl -sS -o /dev/null -w '%{http_code}\n' https://voice.jamesugbanu.com/metrics
# expect 404/closed, not 200
```

Test a non-sensitive utterance end-to-end (browser or WS probe), confirm the
cloned voice, and record measured startup time, peak RSS, disk, and latency.
RAG failure must end the turn cleanly (`rag_failed`), never fabricate facts.

## 13. Schedules, stale protection, IP preservation

| Window | Start (Africa/Lagos) | Stop |
|---|---|---|
| Mon–Fri | 06:00 | 23:00 |
| Sat–Sun | 08:00 | 22:00 |

113 h/week ≈ 489.7 h/month (current enabled schedule). Change weekday/weekend hours with the scheduler
manager (it preserves the enabled/disabled state and refuses unrelated plan
changes):

```bash
cd ../terraform-voice-scheduler
export AWS_PROFILE=taxbloc-terraform AWS_REGION=eu-west-2
scripts/set-voice-hours.sh show
scripts/set-voice-hours.sh plan  --weekday-start 17:00 --weekday-stop 21:00  # example values only
scripts/set-voice-hours.sh apply --weekday-start 17:00 --weekday-stop 21:00  # example values only
```

Omitted flags keep the current live value. `plan` is non-applying; `apply`
requires `y/yes`, applies only the refreshed reviewed plan, verifies live, and
then writes `voice-hours.auto.tfvars`. It never enables/disables schedules or
provisions a server.

Each provision labels the server with the Lagos local date (`voice-window`). A stop only deletes a server whose label matches its own
window; delayed/duplicate stops cannot delete a newer server. Reserved
concurrency is 1, retries are bounded, and uncertain creates/deletes are
reconciled, never blindly repeated.

Verify without destroying anything: inspect labels, schedules, and Lambda logs.
A destructive teardown test requires separate approval; the retained Primary IP
must be confirmed present and assignable after any approved teardown.

Enable schedules only after a manual provision/teardown cycle is verified:

```bash
terraform plan -var 'enable_schedules=true' -out=build/enable.tfplan
# review the 4 DISABLED -> ENABLED changes, then:
terraform apply build/enable.tfplan
```

Enabling is separate from changing hours; `set-voice-hours.sh apply` rejects
any state change. The current schedules are **ENABLED**; use the enable block above only for a first rollout or rebuild.

## 14. Costs (489.7 h/month)

Verified via Hetzner `GET /pricing` on 2026-10-17 (account currency **USD**,
`vat_rate = 0`):

- `cx23` (2 vCPU / 4 GB / 40 GB): $0.0104/h × 489.7 = **$5.09** compute;
  + $0.60 IPv4 = **$5.69/month**.
- `cpx21` (3 vCPU / 4 GB / 80 GB): $0.0176/h × 489.7 = **$8.62** compute;
  + $0.60 = **$9.22/month**.
- No VAT is applied by this account and no FX conversion is needed.

The estimator's labels say EUR, but the arithmetic is currency-agnostic: pass
the USD values with `--vat-pct 0 --eur-usd 1.0`.

```bash
python3 deploy/cost-estimate.py --hourly 0.0104 --hours-week 45 \
  --ipv4-month 0.60 --vat-pct 0 --eur-usd 1.0 --budget-usd 3
```

AWS: ~14 invocations/week (≤900 s, 256 MB), EventBridge Scheduler and SSM
standard parameters in the free tier, 7-day CloudWatch logs. Expected
well under $0.10/month; not separately metered in this workspace. CloudWatch
ingestion is not free.

GHCR: GitHub's billing documentation states container-image storage and
bandwidth for the Container registry are **currently free** (GitHub must give
at least one month's notice of a change). Re-check before relying on it.
No snapshots, volumes, or traffic overage are used by this design.

## 15. Rollback and recovery

- **Bad image:** set `image_ref` to the previous digest, `terraform apply`,
  then `provision` (or wait for the next window).
- **Failed provision:** read the Lambda logs (`provision_decision`,
  `provision_creating`, `provision_ready`, `failed`). If the create outcome was
  uncertain, the handler adopts one matching server and never repeats the POST.
  Inspect `GET /servers?name=james-voice` before retrying.
- **Failed teardown:** a stop refuses unless the window label matches. If the
  delete is uncertain, the handler verifies absence and never repeats it.
- **IP missing/unassignable:** stop; do not create duplicates. Inspect the
  retained Primary IP in Hetzner and reconcile manually.
- **Secrets:** rotate in SSM, then re-provision; the server reads them at boot.
  Rotate the GHCR read-only token independently from the push credential.
- **Terraform:** `terraform destroy` removes AWS resources only; it does not
  delete the Hetzner server or the IP. Stop the server first if desired.

## 16. Troubleshooting

| Symptom | Check |
|---|---|
| `docker login ghcr.io` fails | account has `write:packages`; owner is `jamesugbanu`; 2FA/PAT flow completed |
| Push denied / package public | package visibility in GitHub; anonymous manifest request must be 401 |
| Provision fails before readiness | Lambda logs; SSM parameter names/values exist in eu-west-2; `health_url`/Cloudflare Tunnel/DNS correct |
| Readiness timeout | `docker logs james-voice`; models downloading; `HF_TOKEN` valid and gated terms accepted |
| `cloning_active=false` | reference path/mode inside image; `VOICE_TTS_MODE=clone`; gated weights loaded; never fall back to preset |
| `db=false` | `/srv/voice/data` writable by uid 999; volume ownership |
| RAG 401 | `rag-service-token` must equal the portfolio's bearer token |
| RAG timeouts | portfolio/LLM latency; voice answer speed follows RAG first token |
| Tunnel/certificate errors | check the Cloudflare dashboard tunnel connector and proxied DNS route; edge TLS is managed by Cloudflare |
| Stop refused as stale | server label `voice-window` does not match the stop window; inspect before acting |
| Lambda throttled | reserved concurrency 1; a manual invoke overlapped a scheduled one; retry after it finishes |

## 17. Teardown

- **Scheduled lifecycle (approved):** `teardown` deletes only the server for its
  own window and leaves the retained Primary IPv4 in place.
- **Permanent teardown (separate approval):** disable schedules
  (`terraform apply -var 'enable_schedules=false'`), run a manual `teardown`,
  then `terraform destroy` for AWS resources. Delete the Primary IPv4, SSM
  secrets, GHCR package, and DNS record only with explicit approval and after
  confirming nothing else needs them.
- Never delete a server or release the IP while provisioning/deletion state is
  uncertain; reconcile first.

## 18. Operational checklist

- [ ] Reference still git-ignored and not in `git status`.
- [ ] Tests pass (262 voice / 88 scheduler) and Terraform validates.
- [ ] GHCR package private, amd64, digest recorded.
- [ ] Eight SSM parameters exist; no secret in source/state/logs.
- [ ] Primary IPv4 allocated and retained; `primary_ip_id` set.
- [ ] `image_ref` pinned to a digest.
- [ ] Terraform plan reviewed and approved; current deployment schedules ENABLED.
- [ ] Cloudflare Tunnel active; proxied DNS route and public HTTPS verified.
- [ ] Manual `provision` reaches `db/warm/cloning_active=true`.
- [ ] Public HTTPS `/healthz` works; `/metrics` is closed.
- [ ] RAG, STT, cloned TTS verified with a non-sensitive phrase.
- [ ] Startup/RAM/disk/latency recorded on the real host.
- [ ] Manual `teardown` preserves the IP (if approved to test).
- [ ] Schedules enabled only after the above.
