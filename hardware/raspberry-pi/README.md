# raspberry-pi/

Raspberry Pi in-car runtime hosts.

## Boards on hand

| Board | Role |
|---|---|
| **Raspberry Pi 4 Model B** | **active target** — the in-car runtime host (`f10pi/`) |
| Raspberry Pi Zero ×2 | secondary / experimental, not provisioned |

The Pi 4 was chosen because it has everything the vehicle topology needs on
one board: **built-in RJ45 Ethernet** (for the BMW ENET cable), Wi-Fi
(Internet), Bluetooth, USB, and micro-HDMI. It can also host future
camera/CV work.

## Subprojects

- **[`f10pi/`](f10pi/README.md)** — the Pi 4 node: reproducible
  provisioning (hostname, Wi-Fi, WireGuard, SSH, BMW `eth0`), the
  application systemd services, config templates, and a verification
  script. This is the only implemented node.

The two Pi Zero boards would each get their own directory here if/when
they are provisioned; they'd share the same script/config patterns and
each take a fixed WireGuard address.
