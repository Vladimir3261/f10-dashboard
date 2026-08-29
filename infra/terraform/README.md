# terraform/ — provision the analytics droplet (stage 1)

Creates a single DigitalOcean droplet to host the analytics server
(ClickHouse lake + ingest + Grafana) and the **WireGuard gateway** the
Raspberry Pi tunnels into — so you can reach a NATed Pi over the VPN
without sharing its LAN.

This is a **simple quickstart, not the only way.** Only this directory is
DigitalOcean-specific; swap it for any provider (or a bare VM) and the
Ansible layer still applies. Terraform creates just the VM, its SSH keys
and its firewall — all software config is [`../ansible/`](../ansible).

## Prerequisites

- `terraform` installed.
- A DigitalOcean API token, exported (never put it in a file):
  ```bash
  export DIGITALOCEAN_TOKEN=dop_v1_your_token
  ```
- A local SSH **public** key (default `~/.ssh/id_ed25519.pub`). The same
  key later authorises you on the Pi.

## Use (via the infra Makefile)

```bash
cd infra
cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # edit region etc.
make init          # once
make plan          # review
make provision     # apply + generate the Ansible inventory
```

Then configure it: `make deploy` (stage 2).

## What it opens

The firewall exposes only **SSH (22/tcp)** — restrict `ssh_allowed_cidrs`
to your own address — and **WireGuard (`wireguard_port`/udp)**, which must
be world-reachable because the Pi initiates the tunnel outbound. ClickHouse
(8123/9000), ingest (8090) and Grafana (3000) bind to localhost and are
reached **over the tunnel**, never the public internet.

## Secrets & state

`terraform.tfvars` and all `*.tfstate` are gitignored (state can contain
sensitive data). The provider lock (`.terraform.lock.hcl`) **is** committed
for reproducible provider versions. The API token lives only in your
environment. See [outputs.tf](outputs.tf) for what feeds the next stage.
