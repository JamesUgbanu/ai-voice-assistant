# Hetzner Cloud provider configuration.
#
# Credentials are NEVER stored in this repository. Supply the API token through
# the `HCLOUD_TOKEN` environment variable (recommended) or, if you must, via the
# sensitive `hcloud_token` variable backed by an untracked `.tfvars` file or a
# secret manager. See README.md ("Required credentials").
provider "hcloud" {
  # `null` falls back to the HCLOUD_TOKEN environment variable.
  token = var.hcloud_token
}
