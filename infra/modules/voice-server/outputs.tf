output "user_data" {
  description = "Rendered cloud-init user data (contains NO secrets; safe to pass as provider user-data)."
  value       = local.cloud_init
}

output "firewall_rules" {
  description = "Normalized inbound firewall rules for the provider to materialize."
  value       = local.firewall_rules
}

output "required_ports" {
  description = "Public ports the application architecture requires (SSH + web edge)."
  value       = distinct(concat([var.ssh_port], var.public_web_ports))
}

output "health_path" {
  description = "Application health-check path (proxied by the app's Caddy edge)."
  value       = "/healthz"
}

output "app_dir" {
  description = "Application directory created by the bootstrap."
  value       = var.app_dir
}

output "deploy_user" {
  description = "Non-root deployment user created by the bootstrap."
  value       = var.deploy_user
}

output "base_labels" {
  description = "Provider-neutral base labels for resource tagging."
  value = {
    project     = "james-voice-service"
    environment = var.environment
    managed-by  = "terraform"
    role        = "voice-server"
  }
}
