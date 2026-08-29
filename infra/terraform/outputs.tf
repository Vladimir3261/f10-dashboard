# Consumed by infra/scripts/gen_inventory.py to build the (gitignored)
# Ansible inventory, so the config step never needs the IP typed by hand.

output "droplet_ip" {
  description = "Public IPv4 of the analytics droplet."
  value       = digitalocean_droplet.analytics.ipv4_address
}

output "droplet_id" {
  description = "DigitalOcean droplet id."
  value       = digitalocean_droplet.analytics.id
}

output "droplet_name" {
  value = digitalocean_droplet.analytics.name
}

output "ssh_user" {
  description = "Login user Ansible should use (DigitalOcean images log in as root)."
  value       = "root"
}

output "wireguard_port" {
  value = var.wireguard_port
}

output "ssh_public_keys" {
  description = <<-EOT
    The admin public keys loaded onto the droplet. Re-used to authorise the
    same admin on the Raspberry Pi, so one keypair reaches both.
  EOT
  value       = local.ssh_public_keys
}
