# Module: `voice-server` (provider-neutral contract)

This module encodes the parts of provisioning that do **not** depend on a
specific VPS provider, so a future provider can reuse them unchanged:

- **Bootstrap** (`cloud-init.yaml.tftpl`): a cloud-init document that prepares
  any Ubuntu/Debian host — Docker engine, Compose plugin, a non-root deploy
  user, the application directory, log rotation, optional swap, optional host
  firewall (ufw), and SSH hardening. It contains **no secrets**; the application
  `.env` is delivered later over SSH by `../../scripts/deploy.sh`.
- **Network contract**: a normalized, provider-neutral set of inbound firewall
  rules (SSH restricted to `ssh_allowed_cidrs`; the public web edge on
  `public_web_ports`; plus explicit `extra_firewall_rules`).

It creates **no provider resources**. A provider implementation
(`../../providers/<name>/`) maps the outputs onto real infrastructure.

## Inputs

See `variables.tf`. The important ones: `name`, `environment`, `app_hostname`,
`deploy_user`, `app_dir`, `ssh_port`, `ssh_allowed_cidrs`, `public_web_ports`,
`enable_ipv4`, `enable_ipv6`, `swap_size_mb`, `enable_host_firewall`,
`enable_ssh_hardening`.

## Outputs

- `user_data` — rendered cloud-init (no secrets).
- `firewall_rules` — normalized inbound rules; each has `description`,
  `protocol` (`tcp`/`udp`/`icmp`), `port`, `source_ips`.
- `required_ports` — SSH + web edge ports.
- `health_path` — `/healthz`.
- `app_dir`, `deploy_user` — bootstrap facts.
- `base_labels` — project/environment/role labels for provider tagging.

## What a new provider must do

1. Consume `user_data` as the server's cloud-init / user-data.
2. Materialize `firewall_rules` in the provider's firewall (provider firewalls
   are the primary network control).
3. Expose the same outputs the Hetzner provider exposes (`server_id`,
   `public_ipv4`, `public_ipv6`, `app_hostname`, `firewall_id`, ...).

No changes to this module or to the application are required.
