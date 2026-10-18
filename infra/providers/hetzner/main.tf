# Hetzner Cloud implementation of the voice-server contract.
#
# Creates ONLY provider resources: SSH key, firewall, server, and an optional
# attached volume. Application bootstrap comes from the shared module's
# cloud-init; application deployment is done separately by
# ../../scripts/deploy.sh using no provider-specific operations.

locals {
  # Merge operator labels over the contract's base labels. Hetzner label keys
  # and values must be lowercase and contain only alphanumerics, '-', '_'.
  merged_labels = merge(
    module.contract.base_labels,
    { for k, v in var.labels : k => v },
  )
  labels = {
    for k, v in local.merged_labels :
    lower(k) => lower(replace(replace(v, " ", "-"), ".", "-"))
  }

  has_public_key = length(trimspace(var.ssh_public_key == null ? "" : var.ssh_public_key)) > 0
  has_key_name   = var.ssh_key_name != null && var.ssh_key_name != ""

  # Referenced SSH key IDs (managed content key or an existing named key).
  ssh_key_ids = local.has_public_key ? [hcloud_ssh_key.managed[0].id] : (local.has_key_name ? [data.hcloud_ssh_key.existing[0].id] : [])
}

# Provider-neutral bootstrap + firewall contract.
module "contract" {
  source = "../../modules/voice-server"

  name                 = var.server_name
  environment          = var.environment
  app_hostname         = var.app_hostname
  deploy_user          = var.deploy_user
  app_dir              = var.app_dir
  ssh_port             = var.ssh_port
  ssh_allowed_cidrs    = var.ssh_allowed_cidrs
  ssh_public_key       = var.ssh_public_key == null ? "" : var.ssh_public_key
  public_web_ports     = var.public_web_ports
  extra_firewall_rules = var.extra_firewall_rules
  enable_ipv4          = var.enable_ipv4
  enable_ipv6          = var.enable_ipv6
  enable_host_firewall = var.enable_host_firewall
  enable_ssh_hardening = var.enable_ssh_hardening
  swap_size_mb         = var.swap_size_mb
  extra_packages       = var.extra_packages
}

# ---------------------------------------------------------------------------
# SSH keys
# ---------------------------------------------------------------------------
resource "hcloud_ssh_key" "managed" {
  count      = local.has_public_key ? 1 : 0
  name       = "${var.server_name}-deploy"
  public_key = trimspace(var.ssh_public_key)
  labels     = local.labels
}

data "hcloud_ssh_key" "existing" {
  count = local.has_public_key || !local.has_key_name ? 0 : 1
  name  = var.ssh_key_name
}

# ---------------------------------------------------------------------------
# Firewall (network security is the PRIMARY control; see README)
# ---------------------------------------------------------------------------
resource "hcloud_firewall" "this" {
  name   = "${var.server_name}-fw"
  labels = local.labels

  dynamic "rule" {
    for_each = module.contract.firewall_rules
    content {
      direction   = "in"
      protocol    = rule.value.protocol
      port        = rule.value.protocol == "icmp" ? null : rule.value.port
      source_ips  = rule.value.source_ips
      description = rule.value.description
    }
  }
}

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
resource "hcloud_server" "this" {
  name         = var.server_name
  server_type  = var.server_type
  image        = var.image
  location     = var.location
  labels       = local.labels
  ssh_keys     = local.ssh_key_ids
  firewall_ids = [hcloud_firewall.this.id]
  user_data    = module.contract.user_data

  public_net {
    ipv4_enabled = var.enable_ipv4
    ipv6_enabled = var.enable_ipv6
  }

  lifecycle {
    # `user_data` is create-only: cloud-init cannot be re-applied to a running
    # server. Ignoring it prevents a harmless template edit from silently
    # planning a server REPLACEMENT. Bootstrap changes require a deliberate
    # reprovision (documented in README). Everything else stays visible to the
    # plan so drift and replacements are not concealed.
    ignore_changes = [user_data]

    precondition {
      condition     = local.has_public_key || local.has_key_name
      error_message = "Provide ssh_public_key or ssh_key_name so key-based SSH is available (password SSH is not enabled)."
    }
  }
}

# ---------------------------------------------------------------------------
# Optional attached volume (disabled by default; see README "Persistent storage")
# ---------------------------------------------------------------------------
resource "hcloud_volume" "data" {
  count    = var.data_volume_size_gb > 0 ? 1 : 0
  name     = "${var.server_name}-data"
  size     = var.data_volume_size_gb
  location = var.location
  format   = "ext4"
  labels   = local.labels
}

resource "hcloud_volume_attachment" "data" {
  count     = var.data_volume_size_gb > 0 ? 1 : 0
  volume_id = hcloud_volume.data[0].id
  server_id = hcloud_server.this.id
  automount = true
}
