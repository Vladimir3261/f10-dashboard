# BMW ENET link (eth0)

`eth0` is dedicated to the BMW ENET (Ethernet diagnostics) cable to the
car's central gateway (ZGW). This is a read-only diagnostic link.

## How the link works

- **Physical:** an ENET (OBD-II ↔ RJ45) cable from the car's OBD port to
  the Pi's built-in Ethernet jack.
- **Addressing:** link-local IPv4, `169.254.0.0/16`. The ZGW does **not**
  run DHCP — the host self-assigns a `169.254.x.x` address. The Pi picks
  e.g. `169.254.10.10`; the gateway answers at its own `169.254.x.x`.
- **Discovery:** the runtime UDP-broadcasts the ENET discovery packet to
  `169.254.255.255:6811` (`ENET_DISCOVERY_PORT`), and the gateway replies
  with its address.
- **Transport:** HSFZ over TCP to the gateway on `6801`
  (`ENET_DIAGNOSTIC_PORT`); OBD/UDS requests are routed from there.

`live.py` does all of the above itself. This script only has to make
`eth0` a stable, isolated link-local interface.

## The critical config

`ipv4.method` must be **`link-local`**, never `auto` (DHCP):

```bash
sudo nmcli connection modify bmw-enet \
    ipv4.method link-local \
    ipv4.never-default yes \
    ipv4.ignore-auto-dns yes \
    ipv6.method link-local
```

- `link-local` — because there is no DHCP server on the car; a DHCP
  profile hangs at "getting IP configuration" forever (the real
  first-drive blocker; see `PI_COMMISSIONING.md §2`).
- `never-default yes` — the BMW link must **never** carry the default
  route.
- `ignore-auto-dns yes` — the car must never touch DNS resolution.

[`configure-eth0-bmw.sh`](../scripts/configure-eth0-bmw.sh) applies all of
this and aborts if `eth0` ends up with a default route.

## Verifying the link

```bash
# interface has a link-local address and a carrier
ip -4 addr show eth0            # expect 169.254.x.x/16
cat /sys/class/net/eth0/carrier # 1 = cable connected

# and it must NOT be the default route
ip route show default          # must be dev wlan0, never eth0

# then let the runtime discover the gateway
./run_car.sh                   # logs discovery + starts the dashboard
```

`verify.sh` reports all of the above in one shot.

## Reliability note

The ENET connector is a physical weak point in a moving car — during
drive 6 the cable lost carrier mid-drive, fragmenting the session into
multiple runs (`PI_COMMISSIONING.md §7`). If you see repeated `Errno 99`
(cannot assign requested address) or bursts of short runs, suspect the
connector, not the software. `live.py` re-detects the link-local
interface on every reconnect attempt, so it recovers on its own once the
cable seats again.

## No VIN

The link exposes the car's VIN over diagnostics, but **no VIN is ever
committed** — not in docs, tests, artifacts, or config. On-car artifacts
under `validation-runs/` and `drive-sessions/` are VIN-redacted.
