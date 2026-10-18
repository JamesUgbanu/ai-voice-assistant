# Provider source constraint for this module. Terraform requires each module
# that uses a provider to declare its source address.
terraform {
  required_providers {
    hcloud = {
      source = "hetznercloud/hcloud"
    }
  }
}
