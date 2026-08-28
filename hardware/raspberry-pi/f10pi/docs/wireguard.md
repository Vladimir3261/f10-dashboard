# WireGuard management VPN (wg0)

A private tunnel between the Pi and the telemetry server, used for admin
(SSH) and for the sync agent to reach ClickHouse/ingest without ever
crossing the public Internet in cleartext.

## Why WireGuard here

- **Works through CGNAT / phone hotspots.** The Pi initiates the tunnel
  outward to the server's public endpoint, so the Pi needs no inbound
  port and no public IP — essential in a moving car on mobile data.
- **`PersistentKeepalive = 25`** keeps the NAT mapping alive so the server
  can reach back to the Pi (for the reverse dashboard tunnel, admin, etc.)
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
