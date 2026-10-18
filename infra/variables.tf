# Root input variables for the voice-service infrastructure.
#
# These describe the provider-neutral *deployment contract* the voice service
# needs plus the Hetzner-specific placement choices. Defaults reflect the
# repository's measured/estimated requirements (see README.md "Server sizing");
# they are starting points, not guarantees.

# ---------------------------------------------------------------------------
# Provider credentials
# ---------------------------------------------------------------------------
variable "hcloud_token" {
  description = "Hetzner Cloud API token. Prefer the HCLOUD_TOKEN environment variable instead of setting this in a file. Never commit a real token."
  type        = string
  default     = null
  sensitive   = true
}

# ---------------------------------------------------------------------------
# Identity / placement
# ---------------------------------------------------------------------------
variable "environment" {
  description = "Environment name used in labels and resource naming (e.g. production, staging)."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9_-]*$", var.environment))
    error_message = "environment must be lowercase alphanumeric with '-' or '_'."
  }
}

variable "server_name" {
  description = "Name of the Hetzner Cloud server (also used as the OS hostname)."
  type        = string
  default     = "james-voice-service"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*$", var.server_name)) && length(var.server_name) <= 63
    error_message = "server_name must be lowercase alphanumeric with '-', max 63 chars."
  }
}

variable "server_type" {
  description = "Hetzner Cloud server type (defines vCPU/RAM/architecture). See README.md for sizing. Changing this replaces the server."
  type        = string
  default     = "cpx21"
}

variable "cpu_architecture" {
  description = "Expected CPU architecture of server_type: 'x86_64' (cx*/cpx*/ccx*) or 'arm64' (cax*). ARM is UNVERIFIED for this stack; see README.md."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.cpu_architecture)
    error_message = "cpu_architecture must be 'x86_64' or 'arm64'."
  }

  validation {
    condition = (
      (var.cpu_architecture == "arm64" && startswith(var.server_type, "cax")) ||
      (var.cpu_architecture == "x86_64" && !startswith(var.server_type, "cax"))
    )
    error_message = "cpu_architecture must match server_type: cax* is arm64, all other Hetzner shared types are x86_64."
  }
}

variable "image" {
  description = "OS image for the server. The cloud-init bootstrap targets Ubuntu/Debian (apt + docker.io)."
  type        = string
  default     = "ubuntu-24.04"
}

variable "location" {
  description = "Hetzner Cloud location (e.g. fsn1, nbg1, hel1, ash, hil, sin). Changing this replaces the server. See README.md for RAG-latency considerations."
  type        = string
  default     = "fsn1"
}

variable "enable_ipv4" {
  description = "Allocate a public IPv4 address."
  type        = bool
  default     = true
}

variable "enable_ipv6" {
  description = "Allocate a public IPv6 address. The firewall is configured for IPv6 deliberately; see README.md."
  type        = bool
  default     = true

  validation {
    condition     = var.enable_ipv4 || var.enable_ipv6
    error_message = "At least one of enable_ipv4 / enable_ipv6 must be true; the server needs public connectivity."
  }
}

variable "labels" {
  description = "Extra Hetzner resource labels (lowercase alphanumeric, '-', '_'). Merged with the built-in project labels."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------
variable "ssh_port" {
  description = "SSH port exposed by the firewall and cloud-init host firewall."
  type        = number
  default     = 22

  validation {
    condition     = var.ssh_port > 0 && var.ssh_port <= 65535
    error_message = "ssh_port must be a valid TCP port."
  }
}

variable "ssh_allowed_cidrs" {
  description = "CIDRs allowed to reach SSH. REQUIRED and must be non-empty: unrestricted SSH is not an acceptable default. Example: [\"203.0.113.4/32\"]."
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.ssh_allowed_cidrs) > 0
    error_message = "ssh_allowed_cidrs must contain at least one trusted CIDR; do not leave SSH open to the world."
  }

  validation {
    condition     = alltrue([for c in var.ssh_allowed_cidrs : can(cidrhost(c, 0))])
    error_message = "Every entry in ssh_allowed_cidrs must be a valid CIDR (e.g. 203.0.113.4/32 or 2001:db8::/64)."
  }
}

variable "ssh_key_name" {
  description = "Name of an EXISTING Hetzner SSH key to reference. Use this OR ssh_public_key (not necessarily both). Mutually exclusive with ssh_public_key."
  type        = string
  default     = null
}

variable "ssh_public_key" {
  description = "SSH public key CONTENT (e.g. 'ssh-ed25519 AAAA... user@host') to register with Hetzner and install for the deploy user. Public keys are not secrets. Use this OR ssh_key_name."
  type        = string
  default     = null
}

# ---------------------------------------------------------------------------
# Application contract (provider-neutral bootstrap/deployment inputs)
# ---------------------------------------------------------------------------
variable "app_hostname" {
  description = "Public hostname the service is served on (Caddy terminates TLS for this name). Must already resolve to the server before first boot. Changing it does not replace the server."
  type        = string
  default     = "voice.jamesugbanu.com"
}

variable "deploy_user" {
  description = "Non-root Linux user that owns the application directory and runs the containers."
  type        = string
  default     = "deploy"

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_-]*$", var.deploy_user)) && var.deploy_user != "root"
    error_message = "deploy_user must be a valid non-root Linux username."
  }
}

variable "app_dir" {
  description = "Absolute directory on the server that holds the application checkout, compose files, and .env."
  type        = string
  default     = "/opt/james-voice-service"

  validation {
    condition     = can(regex("^/", var.app_dir))
    error_message = "app_dir must be an absolute path."
  }
}

variable "public_web_ports" {
  description = "Public inbound TCP ports for the web edge (Caddy). Defaults to 80 (ACME/redirect) and 443 (HTTPS)."
  type        = list(number)
  default     = [80, 443]

  validation {
    condition     = alltrue([for p in var.public_web_ports : p > 0 && p <= 65535])
    error_message = "public_web_ports must contain valid TCP ports."
  }
}

variable "extra_firewall_rules" {
  description = "Additional inbound firewall rules (provider-neutral shape). Ports beyond 80/443 expose the service; add them deliberately."
  type = list(object({
    description = string
    protocol    = string
    port        = string
    source_ips  = list(string)
  }))
  default = []

  validation {
    condition     = alltrue([for r in var.extra_firewall_rules : contains(["tcp", "udp", "icmp"], r.protocol)])
    error_message = "Firewall rule protocol must be one of: tcp, udp, icmp."
  }
}

variable "enable_host_firewall" {
  description = "Additionally configure ufw inside the guest as defense-in-depth. OFF by default because the provider firewall is the primary control and Docker bypasses ufw; see README.md."
  type        = bool
  default     = false
}

variable "enable_ssh_hardening" {
  description = "Disable SSH password authentication and root password login in the guest. Key-only access is required by this infrastructure regardless."
  type        = bool
  default     = true
}

variable "swap_size_mb" {
  description = "Swap file size in MB created at boot. Provides OOM headroom on small hosts; set 0 on hosts with generous RAM. See README.md."
  type        = number
  default     = 2048

  validation {
    condition     = var.swap_size_mb >= 0
    error_message = "swap_size_mb cannot be negative."
  }
}

variable "data_volume_size_gb" {
  description = "Size of an optional attached Hetzner Cloud volume in GB (0 = none). Justified only if weights/session state must survive server replacement; see README.md before enabling."
  type        = number
  default     = 0

  validation {
    condition     = var.data_volume_size_gb >= 0 && floor(var.data_volume_size_gb) == var.data_volume_size_gb
    error_message = "data_volume_size_gb must be a non-negative whole number of GB."
  }
}

variable "extra_packages" {
  description = "Extra apt packages to install during bootstrap (e.g. sqlite3 for inspection). Keep minimal."
  type        = list(string)
  default     = []
}
