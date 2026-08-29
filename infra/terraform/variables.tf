# Inputs. Copy terraform.tfvars.example -> terraform.tfvars (gitignored)
# and adjust. The DigitalOcean API token is NOT a variable here: the
# provider reads it from the DIGITALOCEAN_TOKEN environment variable. Put it
# in infra/.env (gitignored; the Makefile loads + exports it) or export it
# in your shell:
#
#   export DIGITALOCEAN_TOKEN=dop_v1_xxx

variable "droplet_name" {
  description = "Name/hostname of the analytics droplet."
  type        = string
  default     = "f10-analytics"
}

variable "region" {
  description = "DigitalOcean region slug (e.g. fra1, ams3, nyc3, sgp1). Pick one near you."
  type        = string
  default     = "fra1"
}

variable "droplet_size" {
  description = <<-EOT
    Droplet size slug. ClickHouse + ingest + Grafana + WireGuard on one box
    wants headroom - 4 GB is a sane floor (ClickHouse OOMs on background
    merges at ~2 GB). Shrink at your own risk.
  EOT
  type        = string
  default     = "s-2vcpu-4gb"
}

variable "droplet_image" {
  description = "Base image slug. Ubuntu LTS is what the Ansible layer targets."
  type        = string
  default     = "ubuntu-24-04-x64"
}

variable "do_ssh_key_names" {
  description = <<-EOT
    Names of SSH keys ALREADY registered on your DigitalOcean account to
    authorise on the droplet. Looked up with a data source (read-only), so
    there is never an "SSH key already exists" error. This is the preferred
    path: DO installs them for root itself and, because the droplet has keys,
    skips setting/emailing a root password - so the box is key-only from
    first boot. List the names available on your account with `make do-keys`.
  EOT
  type        = list(string)
  default     = []
}

variable "ssh_public_key_files" {
  description = <<-EOT
    Local SSH PUBLIC key files. Their contents are injected as plain text
    into the droplet's authorized_keys via cloud-init (NOT registered as
    DigitalOcean SSH keys - DO refuses to re-create a key already on your
    account). The matching private keys let you in, and the same keys later
    authorise you on the Raspberry Pi. Never a private key.
  EOT
  type        = list(string)
  default     = ["~/.ssh/id_ed25519.pub"]
}

variable "ssh_allowed_cidrs" {
  description = <<-EOT
    Source CIDRs allowed to reach SSH (22/tcp). Default is open because auth
    is key-only, but restricting this to your own address(es) is strongly
    recommended, e.g. ["203.0.113.4/32"].
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"]
}

variable "grafana_allowed_cidrs" {
  description = <<-EOT
    Comma-separated IPs/CIDRs allowed to reach Grafana through the host
    nginx proxy. Normally NOT set here: the Makefile passes GF_ALLOWED_IPS
    from infra/.env as TF_VAR_grafana_allowed_cidrs, so one value drives
    both this cloud firewall rule and the nginx allowlist. Empty means the
    port is not opened at all.
    A bare IP is treated as /32.
  EOT
  type        = string
  default     = ""
}

variable "grafana_public_port" {
  description = "Port the host nginx proxy serves Grafana on (opened only to grafana_allowed_cidrs)."
  type        = number
  default     = 80
}

variable "wireguard_port" {
  description = "UDP port the WireGuard gateway listens on (opened to the world so a NATed Pi can reach it)."
  type        = number
  default     = 51820
}

variable "tags" {
  description = "DigitalOcean tags applied to the droplet and firewall."
  type        = list(string)
  default     = ["f10", "analytics"]
}
