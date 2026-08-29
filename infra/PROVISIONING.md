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

## 3. SSH access, region, size

**Get this right before the first apply** — it's what lets you log in.
Create the file and set your keys:

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
make do-keys        # lists the SSH keys already on your DO account
```

```hcl
# PREFERRED — keys already on your DigitalOcean account, by name.
do_ssh_key_names = ["my-laptop"]

# Optional fallback — a local key that is NOT on your DO account.
ssh_public_key_files = ["~/.ssh/id_ed25519.pub"]

region       = "fra1"
droplet_size = "s-2vcpu-4gb"
```

Two independent mechanisms, and you can use either or both:

| | how | why |
|---|---|---|
| `do_ssh_key_names` | DO attaches the key itself (looked up read-only, so no "key already exists" error) | **preferred** — and because the droplet has keys, DO does *not* set or email a root password, so the box is key-only from first boot |
| `ssh_public_key_files` | injected via cloud-init `user_data` | for a key that isn't registered on your DO account |

> **If neither is set you will get a password prompt instead of key login.**
> With no keys attached, DigitalOcean falls back to a root password. The
> plan will fail with a precondition error rather than build a box you
> can't reach.
>
> SSH keys are applied at **first boot**. Set them before the first apply;
> adding one later is done by Ansible (it manages `authorized_keys` on
> every `make deploy`) — changing them here would force a droplet recreate.

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
| `make do-keys` | list SSH keys registered on your DigitalOcean account |
| `make init` | `terraform init` — fetch the provider, write the lock |
| `make plan` | show the plan; nothing is created |
| `make apply` | create the droplet + firewall (SSH + WireGuard only) |
| `make inventory` | `terraform output` → `ansible/inventory/hosts.yml` (gitignored) |
| `make provision` | `apply` + `inventory` in one go |
| `make galaxy` | install the Ansible collections (`deploy` does this for you) |
| `make ping` | check Ansible can reach the droplet |
| `make deploy` | run `site.yml`: base → docker → stack → nginx → wireguard |
| `make deploy-check` | dry-run the playbook (`--check`) |
| `make lake-status` | stack health + lake row counts |
| `make migrate-lake FROM=...` | copy a lake from an older server |
| `make destroy` | tear the droplet down |

### The Ansible roles (`make deploy`)

1. **base** — non-root admin user (`f10`), `authorized_keys` managed from
   your keys (so rotation is just a re-deploy), SSH hardened to key-only,
   `ufw` allowing only SSH + WireGuard.
2. **docker** — Docker Engine + Compose plugin from Docker's apt repo.
3. **stack** — clones the repo to `/opt/f10-dashboard`, renders
   `infra/.env` from your local secrets, `docker compose up -d --build`
   (ClickHouse + ingest + Grafana), and waits for ingest to report healthy.
4. **nginx** — host reverse proxy for Grafana with an IP allowlist (see §5);
   does nothing until `GF_ALLOWED_IPS` is set.
5. **wireguard** — installs WireGuard, generates the server keypair, enables
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

## 5. Exposing Grafana to specific IPs

By default nothing but SSH and WireGuard faces the internet, and you reach
Grafana over an SSH tunnel. To publish it to a few known addresses instead,
set **one** value in `infra/.env`:

```bash
GF_ALLOWED_IPS=203.0.113.4,198.51.100.0/24    # bare IP = that address only
GF_PUBLIC_PORT=80
```

Then apply both layers — the cloud firewall and the host:

```bash
make apply     # DigitalOcean firewall: opens the port to those CIDRs only
make deploy    # installs/configures nginx with the same allowlist
```

**Empty `GF_ALLOWED_IPS` means nobody** — nginx denies all and the firewall
port is never opened. That's the default.

### How it's enforced

A host **nginx** reverse proxy is the public edge; Grafana's container stays
bound to `127.0.0.1:3000` and is never published to the world.

| layer | does |
|---|---|
| DigitalOcean firewall | drops traffic to the port from anywhere else |
| `ufw` | per-source allow rules for the port |
| nginx `allow`/`deny` | refuses non-allowlisted clients at the proxy |

> **Why nginx and not just publishing the container port?** Docker inserts
> its own iptables rules *ahead* of ufw's INPUT chain, so a published
> container port stays reachable even when ufw says otherwise. nginx is an
> ordinary host process, so ufw governs it normally — and the allowlist
> lives in one readable config instead of a raw iptables chain.

> ⚠️ **Port 80 is plain HTTP.** The allowlist controls *who* can connect,
> not whether traffic is readable in transit — your Grafana login password
> crosses the internet in cleartext. Put TLS in front (a domain +
> Let's Encrypt, or a self-signed cert) before relying on this over
> untrusted networks. The SSH tunnel in §4 remains the encrypted option.

## 6. Migrating an existing lake

Replacing an older server? Copy its data across before decommissioning it —
historical telemetry is the point of the project and cannot be re-collected.

```bash
make migrate-lake FROM=root@old-host ARGS='--dry-run'   # review first
make migrate-lake FROM=root@old-host                     # do it
make lake-status                                         # verify
```

Rows stream **old → your machine → new** over two SSH connections, so the
two servers never need to reach each other, nothing is written to disk, and
neither ClickHouse password is passed on a command line (each is read from
that host's own `infra/.env` at run time).

- **Safe to re-run.** `samples`/`sessions` are `ReplacingMergeTree`, so
  re-inserted rows collapse on merge instead of duplicating — an interrupted
  transfer just gets repeated. Verified: a duplicated copy showed `raw 4 /
  FINAL 2`, and `OPTIMIZE ... FINAL` settled it to 2.
- **Counts look inflated until merges run.** `SELECT count()` includes
  not-yet-collapsed duplicates; `count() FROM t FINAL` gives the true number.
- **Schema drift is handled.** Only columns present on *both* sides are
  copied, so a newer destination column (e.g. `sessions.mappings`, added by
  the mapping-versioning work) simply takes its default on migrated rows
  rather than breaking the transfer.
- **`samples` is copied one month at a time**, so a large table cannot trip
  the memory limit on a small old box.

If the old stack lives somewhere other than `/root/f10-dashboard/infra`,
pass `ARGS='--src-dir /path/to/infra'`.

## 7. Connect a Raspberry Pi (next)

The WireGuard server is up but has no peers yet. To add the Pi:

1. Get the **server public key** from the end of `make deploy` output (and
   the endpoint `<droplet-ip>:51820`).
2. On the Pi, fill `hardware/raspberry-pi/f10pi/config/wireguard.conf` with
   that endpoint + key and run its `configure-wireguard.sh`.
3. Add the Pi back as a peer on the server: put its public key in
   `ansible/group_vars/all.yml` under `wireguard_peers`, then `make deploy`.

(See `hardware/raspberry-pi/f10pi/docs/wireguard.md`.)

## 8. Tear down

```bash
make destroy
```

Removes the droplet and firewall. Your `.env`, `terraform.tfvars` and local
state stay on your machine.

---

## Troubleshooting

### SSH asks for a password instead of using my key

A password *prompt* means the droplet still has `PasswordAuthentication
yes` — i.e. DigitalOcean set a root password because **no SSH key was
attached at create time**, and Ansible (which turns password auth off)
hasn't run yet. Check, in order:

1. **Was a key attached?** `cd terraform && terraform state show
   digitalocean_droplet.analytics | grep -A3 ssh_keys`. Empty means no DO
   key was attached — set `do_ssh_key_names` (see §3) and recreate.
2. **Is your client offering the right key?** With several keys loaded,
   ssh-agent can exhaust the server's `MaxAuthTries` before reaching the
   right one, and you fall through to a password prompt. Force it:
   ```bash
   ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes root@<droplet-ip>
   ```
3. **Did cloud-init apply?** If you relied only on `ssh_public_key_files`,
   log in via the DigitalOcean web console and check:
   ```bash
   cloud-init status --long
   cat /root/.ssh/authorized_keys
   ```

The reliable fix is to set **`do_ssh_key_names`** — with a key attached, DO
never sets a root password in the first place.

### `make deploy` fails to connect

`make ping` first. Fresh droplets take a minute to boot; the playbook waits
up to 5 minutes for SSH. Also confirm `ansible/inventory/hosts.yml` has the
right IP (`make inventory` regenerates it).

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
