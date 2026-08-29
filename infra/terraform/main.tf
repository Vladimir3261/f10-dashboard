# The analytics droplet: ClickHouse lake + ingest server + WireGuard
# gateway for the (NATed) Raspberry Pi tunnel. Terraform only creates the
# bare VM and its firewall; all software configuration is the Ansible
# layer's job (infra/ansible), so this stays small and re-runnable.
#
# The API token comes from the DIGITALOCEAN_TOKEN environment variable -
# never a file. See variables.tf.

provider "digitalocean" {}

locals {
  # Read each local public key file once, as plain text. pathexpand handles
  # a leading ~.
  ssh_public_keys = [for f in var.ssh_public_key_files : trimspace(file(pathexpand(f)))]

  # Authorise the keys by injecting them as PLAIN TEXT via cloud-init,
  # rather than registering them as digitalocean_ssh_key resources: DO
  # refuses to create a key that already exists on the account (a manually
  # added one), which would break `apply`. cloud-init writes them straight
  # into the default user's authorized_keys, so no DO key registry is
  # touched and no name collisions are possible.
  cloud_init = join("\n", concat(
    ["#cloud-config", "ssh_authorized_keys:"],
    [for k in local.ssh_public_keys : "  - ${k}"],
  ))
}

resource "digitalocean_droplet" "analytics" {
  name      = var.droplet_name
  region    = var.region
  size      = var.droplet_size
  image     = var.droplet_image
  user_data = local.cloud_init
  tags      = var.tags

  # Keep the box minimal; Ansible installs Docker, the stack and WireGuard.
  # Recreate only when these change - not on every software change.
  lifecycle {
    ignore_changes = [image]
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
