# Fresh install — from nothing to logging drives

Every step to stand the whole system up: the analytics server, the in-car
Raspberry Pi, and the first drive. Follow it top to bottom; each phase ends
with something you can check.

Roughly 45 minutes, most of it waiting for a droplet and an SD card.

| Phase | What you get | Skippable? |
|---|---|---|
| [0. Prerequisites](#0-prerequisites) | tools + accounts | no |
| [1. Secrets](#1-secrets) | `infra/.env` filled in | no |
| [2. The server](#2-the-analytics-server) | ClickHouse + ingest + Grafana + VPN | no |
| [3. Domains + TLS](#3-optional-domains--tls) | HTTPS hostnames | **optional** |
| [4. The Raspberry Pi](#4-the-raspberry-pi) | in-car host on the VPN | no |
| [5. The car](#5-the-car) | a logged, synced drive | no |
| [6. An existing lake](#6-optional-migrating-an-existing-lake) | old data carried over | **optional** |

---

## 0. Prerequisites

**On your laptop**

```bash
brew install terraform ansible        # macOS; use your package manager elsewhere
```

Git and Python 3 as well — both usually already present. Nothing else: the
runtime is stdlib-only, and you never need WireGuard tooling locally.

**Accounts**

- A DigitalOcean account and an API token with **read+write**:
  <https://cloud.digitalocean.com/account/api/tokens>
- An SSH key. If it is already on your DO account, note its **name**
  (`cd infra && make do-keys` lists them once you have the token set).

**Hardware**

- Raspberry Pi 4 + microSD (16 GB+) and a power supply
- An **ENET cable** (OBD-II → RJ45) for the car
- Optionally a domain, if you want HTTPS hostnames in phase 3

```bash
git clone <this repo> && cd f10-dashboard
python3 -m unittest discover          # sanity check: passes with no car, no network
```

---

## 1. Secrets

One gitignored file holds everything:

```bash
cp infra/.env.example infra/.env
```

Fill in at least these four:

| variable | generate with |
|---|---|
| `DIGITALOCEAN_TOKEN` | from the DO console |
| `CH_PASS` | `openssl rand -hex 24` |
| `INGEST_TOKEN` | `openssl rand -hex 32` |
| `GF_ADMIN_PASSWORD` | `openssl rand -hex 16` |

> Hex values are always safe. Avoid `#` in any value — Terraform-bound
> variables come through Make, which treats it as a comment.

Then pick your region and SSH key:

```bash
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
```

```hcl
do_ssh_key_names = ["my-laptop"]     # a key already on your DO account
region           = "fra1"
droplet_size     = "s-2vcpu-4gb"     # 4 GB is a sensible floor for ClickHouse
```

> **Get the SSH key right before the first apply.** Keys are attached at
> droplet creation; with none attached DigitalOcean falls back to a mailed
> root password and you will get a password prompt instead of key login.

---

## 2. The analytics server

```bash
cd infra
make init          # once: downloads the DigitalOcean provider
make plan          # review — creates nothing, costs nothing
make provision     # creates the droplet, writes the Ansible inventory
make deploy        # hardening, Docker, the stack, WireGuard
make lake-migrate  # apply any ClickHouse schema migrations
```

`make provision` asks you to type `yes` and creates **billable** infra
(~$24/mo at `s-2vcpu-4gb`). `make deploy` takes a few minutes — image pulls
and an ingest build.

**`make lake-migrate` is separate from `make deploy`, and needed after any
schema change.** `clickhouse/init/001_schema.sql` runs *only* on a fresh
volume, so a column added to it is invisible to a lake that already exists
— and the ingest server drops unknown columns **silently**, because
ClickHouse runs with `input_format_skip_unknown_fields=1`. Nothing errors;
the column is just quietly absent. Every migration is idempotent, so
running it after each deploy is the right habit. `make lake-migrate-check`
lists what would be applied without touching anything.

**Check it:**

```bash
make lake-status
```

Expect ClickHouse `healthy`, ingest `{"ok": true}`, WireGuard listening, and
an empty lake. Grafana is not public yet — reach it over an SSH tunnel:

```bash
ssh -L 3000:localhost:3000 root@<droplet-ip>     # then http://localhost:3000
```

Log in as `admin` with your `GF_ADMIN_PASSWORD`.

---

## 3. Optional: domains + TLS

Skip this if you are happy with the SSH tunnel. Otherwise you get
`https://grafana.example.com` and the car dashboard at
`https://f10.example.com`.

**First, DNS.** Point an A record for each name at the droplet IP and wait
for it to resolve — the playbook checks, and refuses to call certbot on a
mismatch rather than burning your Let's Encrypt quota (5 per domain per week).

```bash
dig +short grafana.example.com        # must return the droplet IP
```

Then in `infra/.env`:

```bash
GRAFANA_DOMAIN=grafana.example.com
DASHBOARD_DOMAIN=f10.example.com
LETSENCRYPT_EMAIL=you@example.com
GF_ALLOWED_IPS=203.0.113.4            # who may reach Grafana
DASHBOARD_AUTH_PASSWORD=<a real password>
LETSENCRYPT_STAGING=1                 # 1 while testing, then 0
```

```bash
make apply      # opens 80/443 on the cloud firewall
make deploy     # nginx, certificates, vhosts
```

> **Both commands.** `make apply` moves the firewall, `make deploy`
> configures the host. Running only the second leaves port 80 shut and
> certbot fails with a bare "Timeout during connect".

Set `LETSENCRYPT_STAGING=0` and re-run once it works, to get a trusted cert.
Grafana is IP-allowlisted; the dashboard is behind HTTP Basic Auth (it has no
login of its own and serves the VIN). Details:
[`../infra/NETWORK.md`](../infra/NETWORK.md).

---

## 4. The Raspberry Pi

**Flash the card.** Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager,
and use its settings gear to preconfigure **hostname, your SSH public key,
and Wi-Fi**. That way the Pi joins your network on first boot and you never
need a monitor.

Boot it and confirm you can reach it:

```bash
ssh <user>@<pi-hostname>.local
```

**Optionally pre-fill the Wi-Fi list** the Pi should know in the car:

```bash
cp hardware/raspberry-pi/f10pi/config/wifi.example.env \
   hardware/raspberry-pi/f10pi/config/wifi.env
```

```bash
WIFI_1_SSID=MyHomeWiFi
WIFI_1_PSK=home-password
WIFI_1_PRIORITY=100
WIFI_2_SSID=iPhone
WIFI_2_PSK=hotspot-password
WIFI_2_PRIORITY=80
WIFI_COUNT=2
```

Highest priority in range wins. If this file exists the setup script asks no
Wi-Fi questions and applies these networks on every run. If it does not, you
are asked once, and answering no leaves the Pi's Wi-Fi untouched — the right
choice when it already connects fine.

**Then, from the repo root:**

```bash
./setup-pi.sh
```

It asks for the Pi's login and LAN address, then does everything: generates
the Pi's setup script from the live infrastructure, registers it as a
WireGuard peer on the server, copies the script over and runs it. It reuses
an existing checkout on the Pi rather than cloning a second copy.

**Check it:** the script verifies a WireGuard handshake at the end. From then
on the Pi is reachable from anywhere, through the server:

```bash
ssh -J root@<droplet-ip> <user>@10.77.0.10
```

Then delete the generated script — it holds a private key:

```bash
rm local/pi-setup.sh
ssh <user>@<pi> 'rm ~/pi-setup.sh'
```

---

## 5. The car

Connect the **ENET cable** from the car's OBD-II port to the Pi's Ethernet
jack, and switch the ignition on (engine running for real data).

```bash
ssh -J root@<droplet-ip> <user>@10.77.0.10
cd f10-dashboard && ./run_car.sh
```

`run_car.sh` loads every verified channel — use it rather than a bare
`live.py`, which has no gear or DPF data and shows "N".

**Check it:**

- the dashboard on the Pi's `:8080` (or `https://f10.example.com` if you did
  phase 3)
- rows arriving in the lake: `cd infra && make lake-status`

If `eth0` never gets an address, that is the one known blocker — the profile
must be `ipv4.method link-local`, not DHCP. See
[`PI_COMMISSIONING.md`](PI_COMMISSIONING.md#2-the-enet-link--the-one-real-blocker).

Once a drive is logged, analyse it:

```bash
python3 -m analysis.session_report \
    --db local/sessions/drive-<timestamp>.db --out drive-sessions
```

That writes a report, a JSON summary and curves for the session. See
[`RUN_IN_CAR.md`](RUN_IN_CAR.md) for the day-to-day loop.

---

## 6. Optional: migrating an existing lake

Replacing an older server? Copy its data before decommissioning it —
historical telemetry cannot be re-collected.

```bash
cd infra
make migrate-lake FROM=root@old-host ARGS='--dry-run'   # review
make migrate-lake FROM=root@old-host                     # do it
make lake-status
```

Safe to re-run; schema differences between old and new are handled. Details
in [`../infra/PROVISIONING.md`](../infra/PROVISIONING.md#6-migrating-an-existing-lake).

---

## Where things live

| | |
|---|---|
| `infra/` | the analytics server — Terraform, Ansible, the lake |
| `hardware/` | the devices — Raspberry Pi provisioning |
| `live.py`, `bmwdiag/` | the read-only telemetry runtime |
| `mappings/` | what to read from the car and how to decode it |
| `local/` | gitignored: your sessions, generated scripts, state |

Deeper reading: [`../infra/PROVISIONING.md`](../infra/PROVISIONING.md) for the
server, [`../infra/NETWORK.md`](../infra/NETWORK.md) for what is exposed and
how devices reach each other, [`RUN_IN_CAR.md`](RUN_IN_CAR.md) for day-to-day
driving and analysis.

## If something breaks

- **Server:** `make lake-status`, then
  [`PROVISIONING.md` → Troubleshooting](../infra/PROVISIONING.md#troubleshooting)
- **Pi:** [`recovery.md`](../hardware/raspberry-pi/f10pi/docs/recovery.md) —
  wrong Wi-Fi, WireGuard down, SSH lockout, SD-card-only recovery
- **The car link:** [`bmw-enet.md`](../hardware/raspberry-pi/f10pi/docs/bmw-enet.md)

## Tearing it all down

```bash
cd infra && make destroy      # removes the droplet and its firewall
```

Your `.env`, `terraform.tfvars` and local sessions stay on your machine.
