# Networking

Three interfaces, three jobs, kept isolated. Managed by NetworkManager
(`nmcli`). See [`architecture.md`](architecture.md) for the overview.

> The **verified, as-built** record of the current deployment (including
> the real first-drive debugging) is `docs/PI_COMMISSIONING.md` at the
> repo root. This file is the reusable/reproducible design; that file is
> what actually happened on the car.

## Routing table — the invariant

```
default        dev wlan0        # Internet: ONLY ever wlan0
<mgmt-subnet>  dev wg0          # management VPN: only the mgmt subnet
169.254.0.0/16 dev eth0         # BMW ENET: link-local, no gateway
```

- **`wlan0` owns the default route.** All Internet/cloud traffic.
- **`wg0` routes only the management subnet** (set by `AllowedIPs`, not
  `0.0.0.0/0`). It is for reaching the Pi and reaching the server
  privately — not for Internet.
- **`eth0` has no gateway and no default route, ever.** It only reaches
  `169.254.0.0/16` on the BMW link.

`verify.sh` and `configure-eth0-bmw.sh` both fail loudly if `eth0` ever
acquires a default route.

## wlan0 — Internet uplink

Multiple Wi-Fi networks with autoconnect **priorities**; the Pi joins the
highest-priority network in range and roams as they come and go:

| network | example priority |
|---|---|
| in-vehicle LTE router | 110 |
| home Wi-Fi | 100 |
| phone hotspot | 70 |

The in-car router usually wants to be **highest**: it is the link that is
actually present while driving, and it is the one you want chosen at every
boot. The trade-off is that it also wins when parked at home, so the Pi uses
mobile data there — put it below home Wi-Fi instead if that matters more than
a predictable in-car link.

Configured from the gitignored `config/wifi.env` by
[`configure-wifi.sh`](../scripts/configure-wifi.sh). Real SSIDs/PSKs never
enter git.

**One profile per SSID.** A Pi flashed with Wi-Fi preconfigured already has a
profile for that network under a different name, and two profiles for one
SSID carry independent priorities. If they tie, which one NetworkManager
picks at boot is not deterministic — seen in the field, where a leftover
profile tied with the in-car LTE router and the Pi could come up on either.
`configure-wifi.sh` therefore deletes any other profile for an SSID it
manages (never the active one). Check with:

```bash
nmcli -t -f NAME,AUTOCONNECT-PRIORITY,TYPE connection show \
  | awk -F: '$3=="802-11-wireless"'
```

Priority decides which network is chosen **when connecting** — at boot, or
after a drop. NetworkManager does not abandon a working connection just
because a higher-priority profile appears, so a new profile takes effect on
the next reboot rather than immediately.

```bash
# inspect (no secrets printed)
nmcli -f NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show
nmcli device wifi list
```

## eth0 — BMW ENET link

`ipv4.method` **must be `link-local`**, not `auto`. The BMW ZGW serves no
DHCP; a `auto`/DHCP profile hangs at "getting IP configuration" with no
address and discovery finds nothing. This was the one real blocker during
commissioning — see `PI_COMMISSIONING.md §2`.
[`configure-eth0-bmw.sh`](../scripts/configure-eth0-bmw.sh) sets
link-local, `never-default yes`, and `ignore-auto-dns yes`. Details in
[`bmw-enet.md`](bmw-enet.md).

## wg0 — management VPN

WireGuard tunnel the Pi initiates outward, so it works from behind
CGNAT/hotspots. See [`wireguard.md`](wireguard.md).

## Why this separation matters

The BMW gateway is an isolated, untrusted embedded link. Letting it near
the default route or DNS would be both a reliability hazard (a dead
link-local "gateway") and a security one. Keeping Internet on `wlan0`,
admin on `wg0`, and the car on an island `eth0` is the whole safety model.
