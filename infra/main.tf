# Root composition.
#
# This root wires the provider-neutral application contract to a concrete
# provider implementation. The provider module is responsible for creating and
# configuring provider resources; the shared module is responsible for the
# bootstrap that any compatible VPS provider can reuse (cloud-init user data)
# and for the normalized network/firewall contract.
#
# To add another VPS provider, add `providers/<name>/` and a module block here
# that accepts the same inputs and returns the same outputs. See README.md.

module "hetzner" {
  source = "./providers/hetzner"

  hcloud_token = var.hcloud_token

  environment = var.environment
  server_name = var.server_name
  server_type = var.server_type
  image       = var.image
  location    = var.location
  enable_ipv4 = var.enable_ipv4
  enable_ipv6 = var.enable_ipv6
  labels      = var.labels

  ssh_port          = var.ssh_port
  ssh_allowed_cidrs = var.ssh_allowed_cidrs
  ssh_key_name      = var.ssh_key_name
  ssh_public_key    = var.ssh_public_key

  app_hostname         = var.app_hostname
  deploy_user          = var.deploy_user
  app_dir              = var.app_dir
  public_web_ports     = var.public_web_ports
  extra_firewall_rules = var.extra_firewall_rules

  enable_host_firewall = var.enable_host_firewall
  enable_ssh_hardening = var.enable_ssh_hardening
  swap_size_mb         = var.swap_size_mb
  data_volume_size_gb  = var.data_volume_size_gb
  extra_packages       = var.extra_packages
}
