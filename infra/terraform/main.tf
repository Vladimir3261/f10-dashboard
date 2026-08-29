# The analytics droplet: ClickHouse lake + ingest server + WireGuard
# gateway for the (NATed) Raspberry Pi tunnel. Terraform only creates the
# bare VM and its firewall; all software configuration is the Ansible
# layer's job (infra/ansible), so this stays small and re-runnable.
#
# The API token comes from the DIGITALOCEAN_TOKEN environment variable -
# never a file. See variables.tf.

provider "digitalocean" {}

# Look up SSH keys that ALREADY EXIST on the DigitalOcean account, by name.
# A data source only reads - it never tries to create, so there is no
# "SSH key already exists" error (which is why this is not a resource).
# Attaching these is the native DO path: DO installs them into root's
# authorized_keys itself AND skips setting/emailing a root password, so the
# droplet comes up key-only. List their names with:
#   make do-keys
data "digitalocean_ssh_key" "existing" {
  for_each = toset(var.do_ssh_key_names)
  name     = each.value
}

locals {
  # Read each local public key file once, as plain text. pathexpand handles
  # a leading ~. These are injected via cloud-init - useful for a key that
  # is NOT registered on the DO account.
  ssh_public_keys = [for f in var.ssh_public_key_files : trimspace(file(pathexpand(f)))]

  do_key_fingerprints = [for k in data.digitalocean_ssh_key.existing : k.fingerprint]

  # Belt and braces: write the keys straight into root's authorized_keys.
  # `users:` sets them the normal way; `write_files` with defer:true runs in
  # the final cloud-init stage and guarantees the file exists with the right
  # content/permissions even if the image's default-user handling differs.
  #
  # Built line by line with join() rather than a heredoc: YAML indentation is
  # significant, and heredoc + indent() silently produces a mangled document
  # that cloud-init then ignores.
  cloud_init = join("\n", concat(
    [
      "#cloud-config",
      "disable_root: false",
      "ssh_pwauth: false",
      "users:",
      "  - name: root",
      "    ssh_authorized_keys:",
    ],
    [for k in local.ssh_public_keys : "      - ${k}"],
    [
      "write_files:",
      "  - path: /root/.ssh/authorized_keys",
      "    owner: \"root:root\"",
      "    permissions: \"0600\"",
      "    defer: true",
      "    content: |",
    ],
    [for k in local.ssh_public_keys : "      ${k}"],
    [""],
  ))
}

resource "digitalocean_droplet" "analytics" {
  name   = var.droplet_name
  region = var.region
  size   = var.droplet_size
  image  = var.droplet_image
  tags   = var.tags

  # Native DO keys (preferred - also stops DO setting a root password).
  ssh_keys = local.do_key_fingerprints

  # cloud-init keys, for any local key not registered on the DO account.
  user_data = length(local.ssh_public_keys) > 0 ? local.cloud_init : null

  # Keep the box minimal; Ansible installs Docker, the stack and WireGuard.
  # Recreate only when these change - not on every software change.
  lifecycle {
    ignore_changes = [image]

    precondition {
      condition     = length(var.do_ssh_key_names) > 0 || length(var.ssh_public_key_files) > 0
      error_message = "Set do_ssh_key_names (existing DO account keys) and/or ssh_public_key_files, or you will not be able to log in."
    }
  }
}

resource "digitalocean_firewall" "analytics" {
  name        = "${var.droplet_name}-fw"
  droplet_ids = [digitalocean_droplet.analytics.id]
  tags        = var.tags

  # SSH - restrict ssh_allowed_cidrs to your own address in production.
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.ssh_allowed_cidrs
  }

  # WireGuard - must be reachable from anywhere, because the Pi (behind
  # CGNAT/mobile) initiates the tunnel outbound to this port.
  inbound_rule {
    protocol         = "udp"
    port_range       = tostring(var.wireguard_port)
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # ICMP (ping) for reachability checks.
  inbound_rule {
    protocol         = "icmp"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # ClickHouse (8123/9000), ingest (8090) and Grafana (3000) are NOT opened
  # here: they bind to localhost on the droplet and are reached over the
  # WireGuard tunnel, so they never face the public internet.

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
