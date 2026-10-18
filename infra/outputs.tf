# Root outputs.
#
# Nothing here is secret. Terraform state may still contain sensitive values
# (e.g. the provider token via the provider configuration); treat state as
# sensitive and never commit it.

output "server_id" {
  description = "Provider server identifier."
  value       = module.hetzner.server_id
}

output "server_name" {
  description = "Server name / OS hostname."
  value       = module.hetzner.server_name
}

output "public_ipv4" {
  description = "Public IPv4 address (null when disabled)."
  value       = module.hetzner.public_ipv4
}

output "public_ipv6" {
  description = "Public IPv6 address (null when disabled)."
  value       = module.hetzner.public_ipv6
}

output "app_hostname" {
  description = "Public hostname for the voice service."
  value       = module.hetzner.app_hostname
}

output "health_url" {
  description = "Public HTTPS health-check URL."
  value       = "https://${var.app_hostname}/healthz"
}

output "ssh_command" {
  description = "Suggested SSH command (using the deploy user)."
  value       = "ssh -p ${var.ssh_port} ${var.deploy_user}@${coalesce(module.hetzner.public_ipv4, module.hetzner.public_ipv6, "PENDING")}"
}

output "firewall_id" {
  description = "Provider firewall identifier."
  value       = module.hetzner.firewall_id
}

output "data_volume_id" {
  description = "Attached data volume identifier (null when disabled)."
  value       = module.hetzner.data_volume_id
}

output "deploy_command" {
  description = "Non-interactive deploy command for scripts/deploy.sh (host = public IP)."
  value       = "./scripts/deploy.sh --host ${coalesce(module.hetzner.public_ipv4, module.hetzner.public_ipv6, "PENDING")} --user ${var.deploy_user} --app-dir ${var.app_dir} --secrets ./secrets/voice.env"
}
