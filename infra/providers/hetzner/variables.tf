# Provider-specific inputs. These mirror the root variables; the provider
# implementation consumes the provider-neutral contract from ../../modules.

variable "hcloud_token" {
  description = "Hetzner Cloud API token (prefer the HCLOUD_TOKEN env var)."
  type        = string
  default     = null
  sensitive   = true
}

variable "environment" {
  description = "Environment name for labels."
  type        = string
}

variable "server_name" {
  description = "Server name / hostname."
  type        = string
}

variable "server_type" {
  description = "Hetzner server type."
  type        = string
}

variable "image" {
  description = "OS image."
  type        = string
}

variable "location" {
  description = "Hetzner location."
  type        = string
}

variable "enable_ipv4" {
  description = "Allocate public IPv4."
  type        = bool
}

variable "enable_ipv6" {
  description = "Allocate public IPv6."
  type        = bool
}

variable "labels" {
  description = "Extra labels merged with the base labels."
  type        = map(string)
  default     = {}
}

variable "ssh_port" {
  description = "SSH port."
  type        = number
}

variable "ssh_allowed_cidrs" {
  description = "Trusted CIDRs for SSH."
  type        = list(string)
}

variable "ssh_key_name" {
  description = "Name of an existing Hetzner SSH key (mutually exclusive with ssh_public_key)."
  type        = string
  default     = null
}

variable "ssh_public_key" {
  description = "SSH public key content to register (mutually exclusive with ssh_key_name)."
  type        = string
  default     = null
}

variable "app_hostname" {
  description = "Public hostname."
  type        = string
}

variable "deploy_user" {
  description = "Non-root deploy user."
  type        = string
}

variable "app_dir" {
  description = "Application directory."
  type        = string
}

variable "public_web_ports" {
  description = "Public web edge ports."
  type        = list(number)
  default     = [80, 443]
}

variable "extra_firewall_rules" {
  description = "Additional normalized firewall rules."
  type = list(object({
    description = string
    protocol    = string
    port        = string
    source_ips  = list(string)
  }))
  default = []
}

variable "enable_host_firewall" {
  description = "Configure ufw in the guest."
  type        = bool
  default     = false
}

variable "enable_ssh_hardening" {
  description = "Harden SSH in the guest."
  type        = bool
  default     = true
}

variable "swap_size_mb" {
  description = "Swap file size in MB (0 disables)."
  type        = number
  default     = 0
}

variable "data_volume_size_gb" {
  description = "Optional attached volume size in GB (0 disables)."
  type        = number
  default     = 0
}

variable "extra_packages" {
  description = "Extra apt packages."
  type        = list(string)
  default     = []
}
