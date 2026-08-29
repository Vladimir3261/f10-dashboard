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

## Roles (`site.yml`)

`make deploy` runs, in order:

1. **base** — non-root admin user (`admin_user`, default `f10`),
   `authorized_keys` for admin **and** root managed from
   `admin_ssh_public_keys` (so key rotation is a re-deploy, not a droplet
   recreate), SSH hardened to key-only, and `ufw` allowing only SSH +
   WireGuard.
2. **docker** — Docker Engine + Compose plugin from Docker's apt repo.
3. **stack** — clones the repo to `{{ app_dir }}`, renders `infra/.env`
   from your local secrets (via `lookup('env', ...)` — the Makefile exports
   `.env`), and `docker compose up -d --build` (ClickHouse, ingest,
   Grafana), then waits for ingest to report healthy.
4. **wireguard** — installs WireGuard, generates the server keypair, enables
   IP forwarding, and starts `wg-quick@wg0`. Add the Pi/laptop later via
   `wireguard_peers` in `group_vars/all.yml`.

Non-secret settings live in [`group_vars/all.yml`](group_vars/all.yml);
collections needed are in [`requirements.yml`](requirements.yml) (installed
by `make galaxy`, which `make deploy` runs for you).

Full runbook: [`../PROVISIONING.md`](../PROVISIONING.md).
