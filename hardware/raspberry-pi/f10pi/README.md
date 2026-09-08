# f10pi — Raspberry Pi 4 in-car runtime host

Turns a clean **Raspberry Pi OS Lite (64-bit)** install into the vehicle
telemetry host: Wi-Fi for Internet, a WireGuard VPN for remote management,
and `eth0` dedicated to the BMW ENET link, with the read-only telemetry
runtime autostarting on boot and the admin panel on `:8088` as the one
front door to it — on the LAN, over the tunnel, and (Case B in
[`infra/NETWORK.md`](../../../infra/NETWORK.md)) from the internet through
the server's nginx.

Generic hostname: **`f10pi`** (mDNS: `f10pi.local`). Everything
device-specific is a placeholder — real values go in gitignored
`config/*` files (see [Secrets](#secrets-never-committed)).

## Layout

```
f10pi/
  docs/       architecture, networking, wireguard, ssh, bmw-enet, recovery
  scripts/    idempotent bash: bootstrap + configure-* + verify
  systemd/    f10-dashboard.service, f10-sync.service
  config/     *.example templates (real *.env / *.conf are gitignored)
```

## Quick start (on a fresh Pi)

```bash
# 1. clone the project repo
git clone <REPO_URL> ~/f10-dashboard && cd ~/f10-dashboard/hardware/raspberry-pi/f10pi

# 2. fill in local secrets/config from the templates (these files are gitignored)
cp config/local.env.example        config/local.env
cp config/wifi.example.env         config/wifi.env
cp config/wireguard.example.conf   config/wireguard.conf
$EDITOR config/*.env config/*.conf   # set Wi-Fi country, SSIDs/PSKs/keys, etc.

# 3. provision (idempotent — safe to re-run)
sudo ./scripts/bootstrap.sh

# 4. reboot if bootstrap changed Wi-Fi firmware or the kernel cmdline
sudo reboot

# 5. verify
./scripts/verify.sh
./scripts/verify-wifi-regulatory.sh

# 6. the admin panel (the front door; prints the login once)
cd ../admin && sudo ./install.sh
```

`bootstrap.sh` runs the individual `configure-*.sh` steps in order:
hostname → Wi-Fi regulatory/firmware → Wi-Fi profiles → WireGuard → SSH →
BMW `eth0` → application services. Each step is idempotent and can also be
run on its own.

The Wi-Fi regulatory step exists because Raspberry Pi 4 / BCM43455 firmware
`7.45.265` was observed to miss a preferred channel-13 network during the
initial boot scan, causing NetworkManager to fall back to a lower-priority
hotspot. The provisioning script only patches that known-bad firmware version,
uses a pinned Infineon release with verified SHA-256 hashes, and configures the
country before the first boot scan. See
[`docs/wifi-regulatory.md`](docs/wifi-regulatory.md) for the full investigation,
fix, verification commands, and rollback notes.

## The application on the Pi

The telemetry runtime (`live.py` + `bmwdiag`) is **stdlib-only** — no
`pip install`, no virtualenv required. `f10-dashboard.service` runs
`run_car.sh --host 127.0.0.1` (loads every verified channel; the
dashboard on `:8080` **on the loopback only**), and `f10-sync.service`
runs the sync agent that ships drives to the ClickHouse lake over
`wlan0`. Both are read-only on the car.

The phone never talks to `:8080`. It opens the **admin panel**
([`../admin/`](../admin/README.md), `f10-admin.service`, `:8088` on the
LAN and `wg0` addresses): six tabs — the three telemetry views, proxied
to `live.py` behind the panel's login, and System / Car link / Claude
for the box itself. Share links (`/s/?t=…`) pass through to `live.py`'s
own token check. The same page is what the server's nginx publishes at
`https://<DASHBOARD_DOMAIN>/` when one is configured, with the **same
login** — the panel's generated password goes into the server's
`infra/.env` as `DASHBOARD_AUTH_PASSWORD` — and the server's tunnel
address (`10.77.0.1`, `WG_SERVER_IP` in `local.env`) in the panel's
`trusted_proxies`, which `install.sh` writes when `wg0` exists.
`verify.sh` checks both ports: `:8088` listening, `:8080` on the
loopback and nowhere else.

Optional, and not part of the telemetry system:
[`docs/claude-code.md`](docs/claude-code.md) — keeping a coding agent alive
on the Pi in `tmux`, reachable from a phone.

See [`docs/architecture.md`](docs/architecture.md) for the whole picture,
then the topic docs (`networking`, `wireguard`, `ssh`, `bmw-enet`) and
[`docs/recovery.md`](docs/recovery.md) for unattended-recovery scenarios.

## Secrets (never committed)

Only `config/*.example` files are tracked. Copy each to its real name and
fill it in; the real files are gitignored:

| template | real (gitignored) | holds |
|---|---|---|
| `local.env.example` | `local.env` | hostname, paths, toggles, Wi-Fi country |
| `wifi.example.env` | `wifi.env` | Wi-Fi SSIDs + PSKs + priorities |
| `wireguard.example.conf` | `wireguard.conf` | WireGuard keys + endpoint |

## Status

Provisioned and verified manually; the scripts here codify that. See the
[verified-state checklist](docs/recovery.md#current-verified-state) for
what's proven vs still open (BMW `eth0` on-car test, app autostart,
key-only SSH hardening, unattended cold-boot recovery).
