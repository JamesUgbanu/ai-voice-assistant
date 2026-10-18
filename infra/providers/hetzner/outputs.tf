output "server_id" {
  description = "Hetzner server ID."
  value       = hcloud_server.this.id
}

output "server_name" {
  description = "Server name."
  value       = hcloud_server.this.name
}

output "public_ipv4" {
  description = "Public IPv4 address (null when disabled)."
  value       = var.enable_ipv4 ? hcloud_server.this.ipv4_address : null
}

output "public_ipv6" {
  description = "Public IPv6 address (null when disabled)."
  value       = var.enable_ipv6 ? hcloud_server.this.ipv6_address : null
}

output "app_hostname" {
  description = "Public hostname."
  value       = var.app_hostname
}

output "firewall_id" {
  description = "Firewall ID."
  value       = hcloud_firewall.this.id
}

output "data_volume_id" {
  description = "Attached volume ID (null when disabled)."
  value       = var.data_volume_size_gb > 0 ? hcloud_volume.data[0].id : null
}

output "ssh_key_id" {
  description = "Referenced/created SSH key ID (null when none is managed by Terraform)."
  value       = local.has_public_key ? hcloud_ssh_key.managed[0].id : null
}

output "bootstrap_user_data" {
  description = "Cloud-init used for the server (no secrets)."
  value       = module.contract.user_data
  sensitive   = false
}
