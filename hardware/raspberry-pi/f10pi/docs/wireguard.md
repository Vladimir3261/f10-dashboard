# WireGuard management VPN (wg0)

**The reason this tunnel exists: SSH access to the Pi from anywhere.** The Pi
sits in the car behind CGNAT or a phone hotspot — no public address, no
inbound ports, and hardly ever on the same LAN as your laptop. The server is
the one machine with a stable public address, so the Pi and your laptop both
dial *out* to it and the server relays between them:

```
  laptop  ──public SSH──►  VPS 10.77.0.1  ──wg0──►  Pi 10.77.0.10
                           (jump host)

  ssh -J root@<vps> pi@<WG_PI_IP>     # one command, two hops
```

**Only the Pi joins the VPN.** Your laptop does not: it reaches the VPS over
ordinary public SSH, and the VPS — a tunnel endpoint itself — reaches the Pi
directly. So the tunnel needs exactly one peer.

The sync agent also reaches ingest over this tunnel, so the bearer token and
telemetry never cross the public Internet in cleartext — useful, but
secondary to remote access.

> **Bootstrap gap:** none of this works until WireGuard is configured on the
> Pi. Do that first configuration over the LAN (`f10pi.local`) or with a
> monitor and keyboard.

## Why WireGuard here

- **Works through CGNAT / phone hotspots.** The Pi initiates the tunnel
  outward to the server's public endpoint, so the Pi needs no inbound
  port and no public IP — essential in a moving car on mobile data.
- **`PersistentKeepalive = 25`** keeps the NAT mapping alive so the server
  can reach back to the Pi (SSH in, and the dashboard proxy)
  even when the Pi is idle.
- **The private management IP is the only address used in docs.** All
  committed docs refer to the server as `<WG_SERVER_IP>` (e.g. the
  example `10.77.0.1`) — never its public IP.

## Addressing (use your own subnet)

Pick a private subnet and give each node a fixed address. Example only:

| node | example wg IP |
|---|---|
| server | `<WG_SERVER_IP>` (e.g. `10.77.0.1`) |
| this Pi | `<WG_PI_IP>` (e.g. `10.77.0.10`) |

`AllowedIPs` on the Pi is the **management subnet only** (e.g.
`10.77.0.0/24`) — never `0.0.0.0/0`, which would pull Internet through the
tunnel and off `wlan0`.

## Setup

1. Generate a keypair **on the Pi** (private key never leaves it):
   ```bash
   umask 077
   wg genkey | tee pi_private.key | wg pubkey > pi_public.key
   ```
2. Add the Pi as a `[Peer]` on the server (server keeps its own private
   key; give it the Pi's **public** key and wg IP).
3. Fill `config/wireguard.conf` (copied from the `.example`) with the Pi's
   private key, the server's **public** key, the server endpoint, and the
   `AllowedIPs` subnet. This file is gitignored.
4. Install + enable:
   ```bash
   sudo ./scripts/configure-wireguard.sh
   ```

## Verify (no keys printed)

```bash
./scripts/verify.sh              # reports handshake age + server ping
sudo wg show wg0 latest-handshakes   # a recent, nonzero handshake = up
```

A handshake within the last ~2–3 minutes means the tunnel is healthy.

## Secrets rule

- **Private keys never leave the device that generated them** and never
  enter git.
- Only public keys are exchanged.
- The server's public endpoint IP lives in the owner's notes, not git.

Failure handling — see [`recovery.md`](recovery.md#wireguard-failure).
