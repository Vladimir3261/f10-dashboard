# f10pi — Raspberry Pi 4 in-car runtime host

Turns a clean **Raspberry Pi OS Lite (64-bit)** install into the vehicle
telemetry host: Wi-Fi for Internet, a WireGuard VPN for remote management,
and `eth0` dedicated to the BMW ENET link, with the read-only telemetry
runtime autostarting on boot.

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

# 2. fill in local secrets from the templates (these files are gitignored)
cp config/local.env.example        config/local.env
cp config/wifi.example.env         config/wifi.env
cp config/wireguard.example.conf   config/wireguard.conf
$EDITOR config/*.env config/*.conf   # put your real SSIDs/PSKs/keys here

# 3. provision (idempotent — safe to re-run)
sudo ./scripts/bootstrap.sh

# 4. verify
./scripts/verify.sh
```

`bootstrap.sh` runs the individual `configure-*.sh` steps in order:
hostname → Wi-Fi profiles → WireGuard → SSH → BMW `eth0` → application
services. Each step is idempotent and can also be run on its own.

## The application on the Pi

The telemetry runtime (`live.py` + `bmwdiag`) is **stdlib-only** — no
`pip install`, no virtualenv required. `f10-dashboard.service` runs
`run_car.sh` (loads every verified channel; dashboard on `:8080`), and
`f10-sync.service` runs the sync agent that ships drives to the ClickHouse
lake over `wlan0`. Both are read-only on the car.

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
| `local.env.example` | `local.env` | hostname, paths, toggles |
| `wifi.example.env` | `wifi.env` | Wi-Fi SSIDs + PSKs + priorities |
| `wireguard.example.conf` | `wireguard.conf` | WireGuard keys + endpoint |

## Status

Provisioned and verified manually; the scripts here codify that. See the
[verified-state checklist](docs/recovery.md#current-verified-state) for
what's proven vs still open (BMW `eth0` on-car test, app autostart,
key-only SSH hardening, unattended cold-boot recovery).
