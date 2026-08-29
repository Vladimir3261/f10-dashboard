# hardware/

The hardware layer of the F10 telemetry project — the physical hosts that
run the (software) runtime in the car and the provisioning that makes them
reproducible. Sits next to `infra/` (the ClickHouse/Grafana lake) and
`bmwdiag`/`live.py` (the read-only diagnostic runtime).

## Public-repo hygiene (read first)

This repository is **public**. Nothing here may contain real
infrastructure-identifying or secret data — no public/private IPs, no
WireGuard or SSH keys, no Wi-Fi SSIDs or PSKs, no MAC addresses, no VIN,
no backend/database credentials, no personal usernames, no identifying
route/GPS data. Documentation and scripts use **placeholders and example
variables** (`<WG_PI_IP>`, `<HOME_WIFI_SSID>`, `<SERVER_USER>`, …). Real
values are injected outside git via gitignored `config/*.env` /
`config/*.conf` files. See each subproject's `config/*.example`.

## Provisioning the Pi

```bash
cd hardware && make pi-setup     # generate local/pi-setup.sh from live infra state
```

Targets in this `Makefile` run on **your laptop** and produce something you
apply to a device. The scripts under `raspberry-pi/f10pi/scripts/` run **on
the Pi** and are invoked by that generated script. Full procedure:
[`infra/PROVISIONING.md`](../infra/PROVISIONING.md) §7.

## Subprojects

| Path | What |
|---|---|
| `raspberry-pi/` | the in-car runtime hosts. **`f10pi/`** (Raspberry Pi 4 B) is the active target and the only implemented one. |
| `esp32/` | reserved — later microcontroller experiments. |
| `power/` | reserved — automotive 12 V power design (not started). |
| `vision/` | reserved — future camera / computer-vision work (not started). |

## Architecture in one picture

```
        Internet / cloud (ClickHouse, Grafana, git)
                    ▲
                    │ wlan0  (Wi-Fi / mobile hotspot / LTE router)
                    │
   remote mgmt ──── wg0 ──── Raspberry Pi 4 ──── eth0 ──── BMW ENET cable ── car
   (WireGuard VPN)                                (169.254.0.0/16, read-only)
```

Three interfaces, three jobs, kept separate:
- **`wlan0`** — Internet + cloud sync. Holds the default route.
- **`eth0`** — BMW ENET only (link-local `169.254.0.0/16`). **Never** the
  default route; vehicle traffic stays isolated on Ethernet.
- **`wg0`** — a private management VPN (WireGuard) so the Pi is reachable
  at a stable address regardless of which Wi-Fi/LTE it is on (works
  through CGNAT because the Pi initiates the tunnel).

The BMW diagnostic runtime is **read-only** and must operate independently
of any development agent running on the same Pi.

Start with [`raspberry-pi/f10pi/README.md`](raspberry-pi/f10pi/README.md).
