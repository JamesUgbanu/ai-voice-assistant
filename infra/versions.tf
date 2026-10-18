# Terraform and provider version constraints for the voice-service infrastructure.
#
# This directory (`infra/`) is the single Terraform root configuration.
# Provider-specific resources live in `providers/<name>/`; provider-neutral
# bootstrap lives in `modules/voice-server/`. See README.md.
terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    hcloud = {
      source = "hetznercloud/hcloud"
      # Pin the major/minor line; 1.x is the current Hetzner Cloud provider.
      version = "~> 1.48"
    }
  }

  # ---------------------------------------------------------------------------
  # Backend / state
  # ---------------------------------------------------------------------------
  # Default: local state in this directory (`terraform.tfstate`), which is
  # Git-ignored. This is fine for a single operator but has NO locking.
  #
  # For shared/production use, configure a remote backend and uncomment ONE of
  # the blocks below, then run `terraform init -migrate-state`. Remote backends
  # provide state locking where the backend supports it (S3+DynamoDB,
  # Terraform Cloud, GCS, etc.). Do not add a backend that creates unrelated
  # resources implicitly.
  #
  # backend "s3" {
  #   bucket         = "REPLACE-with-your-state-bucket"
  #   key            = "james-voice-service/production/terraform.tfstate"
  #   region         = "REPLACE-region"
  #   dynamodb_table = "REPLACE-lock-table"   # state locking
  #   encrypt        = true
  # }
  #
  # backend "remote" {
  #   organization = "REPLACE-org"
  #   workspaces { name = "james-voice-service-production" }
  # }
}
