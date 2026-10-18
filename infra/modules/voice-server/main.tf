# Provider-neutral voice-server contract.
#
# This module produces:
#   * cloud-init user data that bootstraps any Ubuntu/Debian VPS (Docker engine,
#     compose plugin, non-root deploy user, application directory, optional swap,
#     optional host firewall, SSH hardening), and
#   * a normalized set of inbound firewall rules (provider-neutral shape).
#
# It intentionally creates NO provider resources. A provider implementation
# (../../providers/<name>/) turns these outputs into real infrastructure. This
# separation is what lets the same application artifact and deployment
# procedure be reused across compatible VPS providers.

locals {
  # "World" sources depend on which stacks the server actually has.
  ipv4_world  = var.enable_ipv4 ? ["0.0.0.0/0"] : []
  ipv6_world  = var.enable_ipv6 ? ["::/0"] : []
  web_sources = concat(local.ipv4_world, local.ipv6_world)

  ssh_rules = [
    {
      description = "SSH (trusted sources only)"
      protocol    = "tcp"
      port        = tostring(var.ssh_port)
      source_ips  = var.ssh_allowed_cidrs
    },
  ]

  web_rules = [
    for p in var.public_web_ports : {
      description = p == 80 ? "HTTP (ACME + redirect)" : (p == 443 ? "HTTPS" : "Web port ${p}")
      protocol    = "tcp"
      port        = tostring(p)
      source_ips  = local.web_sources
    }
  ]

  # SSH first, then the web edge, then anything the operator adds explicitly.
  firewall_rules = concat(local.ssh_rules, local.web_rules, var.extra_firewall_rules)

  ssh_authorized_keys = var.ssh_public_key == "" ? [] : [trimspace(var.ssh_public_key)]

  cloud_init = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    name                 = var.name
    environment          = var.environment
    app_hostname         = var.app_hostname
    deploy_user          = var.deploy_user
    app_dir              = var.app_dir
    ssh_port             = var.ssh_port
    ssh_allowed_cidrs    = var.ssh_allowed_cidrs
    public_web_ports     = var.public_web_ports
    ssh_authorized_keys  = local.ssh_authorized_keys
    enable_host_firewall = var.enable_host_firewall
    enable_ssh_hardening = var.enable_ssh_hardening
    swap_size_mb         = var.swap_size_mb
    extra_packages       = var.extra_packages
  })
}

# Fail fast if the security contract is incomplete. `check` assertions surface
# as warnings; the hard guarantees live in the root variable validations.
check "network_contract" {
  assert {
    condition     = var.enable_ipv4 || var.enable_ipv6
    error_message = "At least one of enable_ipv4 / enable_ipv6 must be true."
  }
}
