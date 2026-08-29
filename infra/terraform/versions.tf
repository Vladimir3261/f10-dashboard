# Terraform + provider version pins. DigitalOcean is the only provider
# shipped here - it is a simple, opinionated quickstart, not the only way.
# Anyone can drop in an equivalent provider (Hetzner, AWS, a bare VM) and
# reuse the Ansible layer unchanged; only this directory is DO-specific.

terraform {
  required_version = ">= 1.3.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.40"
    }
  }
}
