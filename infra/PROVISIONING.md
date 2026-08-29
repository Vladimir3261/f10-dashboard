# Provisioning the analytics server

End-to-end, from nothing to a running ClickHouse lake + ingest + Grafana +
WireGuard gateway on a fresh cloud VM. Two stages, one `make` per step:

```
Terraform  →  create the droplet        (stage 1, DigitalOcean)
   seam     →  terraform output → inventory
Ansible    →  configure everything      (stage 2, provider-agnostic)
```

Everything below runs from the **`infra/`** directory.

---

## 1. Prerequisites

- **Terraform** and **Ansible** installed locally
  (`brew install terraform ansible`).
- A **DigitalOcean API token** (read+write):
  <https://cloud.digitalocean.com/account/api/tokens>.
- An **SSH keypair** whose public key you want on the server (and later the
  Pi). Default is `~/.ssh/id_ed25519.pub`.

## 2. Secrets — `infra/.env`

One gitignored file holds every infra secret. Copy the template and fill it
in:

```bash
cp .env.example .env
```

Set at least:

| variable | what | generate |
|---|---|---|
| `DIGITALOCEAN_TOKEN` | provisioning (Terraform) | from the DO console |
| `CH_PASS` | ClickHouse password | `openssl rand -hex 24` |
| `INGEST_TOKEN` | sync agent bearer token | `openssl rand -hex 32` |
| `GF_ADMIN_PASSWORD` | Grafana admin password | `openssl rand -hex 16` |

> The Makefile loads and exports `.env`, so Terraform gets the DO token and
> Ansible gets the stack secrets with no manual `export`. Avoid `#` inside a
> value (make treats it as a comment); hex values are always safe.

## 3. Choose region / size / keys (optional)

Defaults: region `fra1`, size `s-2vcpu-4gb`, Ubuntu 24.04, key
`~/.ssh/id_ed25519.pub`. To change anything (or authorise more than one
key):

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

```hcl
region       = "ams3"
droplet_size = "s-2vcpu-4gb"
ssh_public_key_files = [
  "~/.ssh/id_ed25519.pub",
  "~/.ssh/laptop.pub",
]
```

> SSH keys are injected at **first boot** via cloud-init. Set them here
> before the first apply. Adding a key later is done by Ansible (it manages
> `authorized_keys` on every `make deploy`) — not by changing this, which
> would force the droplet to be recreated.

---

## The commands

```bash
cd infra

# stage 1 - create the droplet, then generate the Ansible inventory
make init                # once (downloads the DO provider)
make plan                # review what will be created (no changes, free)
make provision           # terraform apply  +  write ansible inventory

# stage 2 - configure the droplet
make deploy              # hardening + Docker + stack + WireGuard
```

That's it. `make provision` prints the droplet IP; `make deploy` finishes
with the WireGuard **server public key** (you'll need it to add the Pi as a
peer).

### What each step does

| command | effect |
|---|---|
| `make init` | `terraform init` — fetch the provider, write the lock |
| `make plan` | show the plan; nothing is created |
| `make apply` | create the droplet + firewall (SSH + WireGuard only) |
| `make inventory` | `terraform output` → `ansible/inventory/hosts.yml` (gitignored) |
| `make provision` | `apply` + `inventory` in one go |
| `make galaxy` | install the Ansible collections (`deploy` does this for you) |
| `make ping` | check Ansible can reach the droplet |
| `make deploy` | run `site.yml`: base → docker → stack → wireguard |
| `make deploy-check` | dry-run the playbook (`--check`) |
| `make destroy` | tear the droplet down |

### The Ansible roles (`make deploy`)

1. **base** — non-root admin user (`f10`), `authorized_keys` managed from
   your keys (so rotation is just a re-deploy), SSH hardened to key-only,
   `ufw` allowing only SSH + WireGuard.
2. **docker** — Docker Engine + Compose plugin from Docker's apt repo.
3. **stack** — clones the repo to `/opt/f10-dashboard`, renders
   `infra/.env` from your local secrets, `docker compose up -d --build`
   (ClickHouse + ingest + Grafana), and waits for ingest to report healthy.
4. **wireguard** — installs WireGuard, generates the server keypair, enables
   forwarding, and starts `wg-quick@wg0`.

---

## 4. Verify

```bash
make output                              # droplet IP
ssh root@$(cd terraform && terraform output -raw droplet_ip)   # log in

# on the server:
docker compose -f /opt/f10-dashboard/infra/docker-compose.yml ps
curl -s localhost:8090/health            # {"ok": true, ...}
```

ClickHouse (8123/9000), ingest (8090) and Grafana (3000) are **not** exposed
publicly — only SSH and WireGuard are. Reach Grafana over an SSH tunnel:

```bash
ssh -L 3000:localhost:3000 root@<droplet-ip>   # then open http://localhost:3000
```

## 5. Connect a Raspberry Pi (next)

The WireGuard server is up but has no peers yet. To add the Pi:

1. Get the **server public key** from the end of `make deploy` output (and
   the endpoint `<droplet-ip>:51820`).
2. On the Pi, fill `hardware/raspberry-pi/f10pi/config/wireguard.conf` with
   that endpoint + key and run its `configure-wireguard.sh`.
3. Add the Pi back as a peer on the server: put its public key in
   `ansible/group_vars/all.yml` under `wireguard_peers`, then `make deploy`.

(See `hardware/raspberry-pi/f10pi/docs/wireguard.md`.)

## 6. Tear down

```bash
make destroy
```

Removes the droplet and firewall. Your `.env`, `terraform.tfvars` and local
state stay on your machine.

---

## Notes & safety

- **Nothing here commits secrets.** `.env`, `*.tfvars`, `*.tfstate` and the
  generated inventory are gitignored; the provider lock is committed for
  reproducibility.
- **`make apply` creates billable infrastructure.** `s-2vcpu-4gb` is about
  $24/mo on DigitalOcean.
- **DigitalOcean is a quickstart, not a lock-in.** Only `terraform/` is
  provider-specific; the Ansible layer configures any Ubuntu host, so you
  can swap in another provider (or a bare VM) and still `make deploy`.
- **First `make ping`/`deploy` may retry** while the fresh droplet finishes
  booting — the playbook waits up to 5 minutes for SSH.
