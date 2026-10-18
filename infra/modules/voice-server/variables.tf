# Provider-neutral inputs for the voice-server deployment contract.
#
# Any supported VPS provider implements this contract. Nothing here is
# Hetzner-specific; the outputs (cloud-init user data + normalized firewall
# rules) are consumed by a provider implementation under ../../providers/.

variable "name" {
  description = "Server / hostname (short name, no domain)."
  type        = string
}

variable "environment" {
  description = "Environment label (e.g. production)."
  type        = string
}

variable "app_hostname" {
  description = "Public hostname the service is served on (informational; TLS is handled by the app's Caddy container)."
  type        = string
}

variable "deploy_user" {
  description = "Non-root Linux user that owns the application directory."
  type        = string
}

variable "app_dir" {
  description = "Absolute application directory on the server."
  type        = string
}

variable "ssh_port" {
  description = "SSH port."
  type        = number
}

variable "ssh_allowed_cidrs" {
  description = "CIDRs allowed to reach SSH (non-empty)."
  type        = list(string)
}

variable "ssh_public_key" {
  description = "Optional SSH public key content installed for the deploy user. When empty, the bootstrap copies root's authorized_keys (providers that inject keys for root)."
  type        = string
  default     = ""
}

variable "public_web_ports" {
  description = "Public inbound TCP ports for the web edge."
  type        = list(number)
  default     = [80, 443]
}

variable "extra_firewall_rules" {
  description = "Additional normalized inbound firewall rules."
  type = list(object({
    description = string
    protocol    = string
    port        = string
    source_ips  = list(string)
  }))
  default = []
}

variable "enable_ipv4" {
  description = "Whether the server has a public IPv4 address (affects world CIDRs)."
  type        = bool
  default     = true
}

variable "enable_ipv6" {
  description = "Whether the server has a public IPv6 address (affects world CIDRs)."
  type        = bool
  default     = true
}

variable "enable_host_firewall" {
  description = "Configure ufw inside the guest as defense-in-depth."
  type        = bool
  default     = false
}

variable "enable_ssh_hardening" {
  description = "Disable SSH password auth / root password login in the guest."
  type        = bool
  default     = true
}

variable "swap_size_mb" {
  description = "Swap file size in MB (0 disables)."
  type        = number
  default     = 0
}

variable "extra_packages" {
  description = "Extra apt packages for the bootstrap."
  type        = list(string)
  default     = []
}
