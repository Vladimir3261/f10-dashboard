# ansible/ — configure the analytics droplet (stage 2)

Configures the droplet Terraform created: the software stack, the
WireGuard gateway, and host hardening. Provider-agnostic — it only needs a
reachable Ubuntu host in the inventory, so it works whether stage 1 was
DigitalOcean or anything else.

## The inventory is generated

`inventory/hosts.yml` is written from Terraform output by `make inventory`
(or `scripts/gen_inventory.py`) and is **gitignored** — it holds the
droplet's public IP. Never edit it by hand; see
[`inventory/hosts.yml.example`](inventory/hosts.yml.example) for its shape.
The admin public keys and WireGuard port flow through it from Terraform.

## Use

```bash
cd infra
make provision     # stage 1 + writes the inventory
make ping          # confirm Ansible can reach the droplet
make deploy        # run site.yml
```

(Needs `ansible` installed for `ping`/`deploy`.)

## Status

`site.yml` is currently a **scaffold** that proves the Terraform→Ansible
seam (it connects and reports facts). The real roles land next:

- base hardening + a non-root admin user (from `admin_ssh_public_keys`)
- Docker + the compose stack (ClickHouse, ingest, Grafana)
- WireGuard gateway for the Pi tunnel
- `ufw` (SSH + WireGuard only; app ports stay on localhost / the tunnel)
