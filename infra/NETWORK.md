# Network map

Exactly what is exposed to the internet, what is not, and how the car, the
Raspberry Pi, the analytics server and your laptop reach each other.

Two supported configurations, both built from the same code:

- **[Case A — IP only](#case-a--ip-only-no-domains)**: no domains, no TLS.
  Grafana on a plain-HTTP port restricted to an IP allowlist.
- **[Case B — domains + TLS](#case-b--domains--tls)**: real hostnames,
  Let's Encrypt certificates, and the Pi's dashboard published through the
  server.

Which one you get is decided by a single thing: whether `GRAFANA_DOMAIN` /
`DASHBOARD_DOMAIN` are set in `infra/.env`. Everything else follows.

Addresses below are placeholders: `<DROPLET_IP>` is the server's public IPv4,
`203.0.113.4` stands for an address on your allowlist, and `10.77.0.0/24` is
the private WireGuard subnet.

---

## The four enforcement layers

Access is filtered four times, outermost first. Each layer is managed by a
different part of this repo, which is why a change in one place alone (a
common trap) does not take effect.

| # | Layer | Managed by | Filters on |
|---|---|---|---|
| 1 | DigitalOcean cloud firewall | **Terraform** (`make apply`) | port + source CIDR, before the packet reaches the VM |
| 2 | `ufw` on the host | **Ansible** (`make deploy`) | port + source, for host processes |
| 3 | nginx vhost rules | **Ansible** (`make deploy`) | `allow`/`deny` by IP, or HTTP Basic Auth |
| 4 | Docker port binding | `docker-compose.yml` | whether a container port is published at all |

> **Layer 1 and layer 2 are updated by different commands.** Setting a domain
> and running only `make deploy` leaves the cloud firewall shut, and
> Let's Encrypt then fails with a bare "Timeout during connect". Run
> `make apply` too. (There is now a preflight that catches this and says so.)

## The one public hole: temporary share links

Everything on the dashboard vhost sits behind HTTP Basic Auth except a
single prefix, **`/s/`**, which exists so a live view can be handed to
someone without giving them the dashboard password.

| | Owner (`/`) | Share link (`/s/?t=<token>`) |
|---|---|---|
| nginx Basic Auth | required | **off** |
| credential | the vhost password | the bearer token in the URL |
| VIN | shown in full | **always masked** to the last 4 |
| gateway IP / ECU list | shown | stripped |
| live view (`/api/snapshot`, `/api/stream`, `/api/meta`) | yes | yes |
| drive history (`/api/runs`, `/api/history`) | yes | **404** |
| sync status and controls (`/api/sync`) | yes | **404** |
| minting or revoking links (`/api/share`) | yes | **404** |
| lifetime | until the password changes | 15 min - 12 h, then dead |

nginx only turns `auth_basic` off for the prefix; it never sees a token and
needs no reload when one is minted or revoked. **`live.py` is the
authority** - it validates the token, serves only the allowlist above, and
masks the VIN independently of `--redact-vin`. Tokens are held in memory
only, so a dashboard restart invalidates every outstanding link.

Mint and revoke from the **share** chip in the dashboard header. To turn
the feature off completely, launch with `--no-share`; the prefix then 404s
and no link can be created.

> **Know what the LAN can do.** `/api/share` is owner-only in the sense
> that it is unreachable through `/s/` and sits behind Basic Auth at the
> edge - but the Pi's own `:8080` has never had a login, so anyone already
> on the car's network can mint a link and publish it outward. That is a
> real step up from merely reading the dashboard on the LAN. Use
> `--no-share` if the Pi ever joins a network you do not control.

> **Docker bypasses `ufw`.** A container port published on `0.0.0.0` inserts
> iptables rules *ahead* of ufw's INPUT chain and is reachable regardless of
> ufw. That is why nothing is published publicly by Docker here — Grafana and
> ingest bind to `127.0.0.1`, ClickHouse is not published at all, and the only
> public listener is nginx, an ordinary host process that ufw governs
> normally.

---

## Case A — IP only (no domains)

`GRAFANA_DOMAIN` and `DASHBOARD_DOMAIN` empty. No certbot, no port 443.

```
                    INTERNET
                        │
     ┌──────────────────┼───────────────────────────┐
     │  DO firewall     │  22/tcp   ← ssh_allowed_cidrs
     │  (layer 1)       │  51820/udp ← anywhere
     │                  │  <GF_PUBLIC_PORT>/tcp ← GF_ALLOWED_IPS only
     └──────────────────┼───────────────────────────┘
                        ▼
     ┌───────────────── ANALYTICS SERVER ─────────────────┐
     │  ufw: 22, 51820, <GF_PUBLIC_PORT> (per-source)      │
     │                                                     │
     │  nginx :80  ── allow <allowlist>; deny all; ──┐     │
     │                                                │     │
     │                       127.0.0.1:3000  Grafana ◄┘     │
     │                    10.77.0.1:8090  ingest (VPN only)│
     │                       (compose net)   ClickHouse     │
     │                                                     │
     │  wg0 10.77.0.1  ◄── WireGuard tunnel                 │
     └─────────────────────────────────────────────────────┘
                        ▲
                        │ initiated OUTBOUND by the Pi
     ┌──────────────────┴──────────┐
     │  RASPBERRY PI (behind NAT)  │
     │  wlan0 → internet           │
     │  eth0  → BMW ENET (car)     │
     └─────────────────────────────┘
```

**Publicly reachable:** SSH (22), WireGuard (51820/udp), and
`GF_PUBLIC_PORT` **only from `GF_ALLOWED_IPS`**.

**Grafana** — `http://<DROPLET_IP>:<GF_PUBLIC_PORT>`, allowlisted at both
layer 1 and layer 3. Anyone else gets `403` from nginx (and is dropped by the
cloud firewall before that).

**The Pi dashboard** is *not* published in this mode. Reach it over the
tunnel, or via an SSH tunnel:

```bash
ssh -L 8080:10.77.0.10:8080 root@<DROPLET_IP>   # then http://localhost:8080
```

> ⚠️ Plain HTTP. The allowlist controls *who* connects, not whether traffic
> is readable in transit — the Grafana password crosses the internet in
> cleartext. For anything untrusted, prefer the SSH tunnel, or use Case B.

---

## Case B — domains + TLS

`GRAFANA_DOMAIN` and/or `DASHBOARD_DOMAIN` set. Certificates from
Let's Encrypt, renewed by certbot's own systemd timer.

```
                    INTERNET
                        │
     ┌──────────────────┼──────────────────────────────────┐
     │  DO firewall     │  22/tcp    ← ssh_allowed_cidrs
     │  (layer 1)       │  51820/udp ← anywhere (Pi is NATed)
     │                  │  80/tcp    ← anywhere  (ACME only)
     │                  │  443/tcp   ← anywhere  (filtered at nginx)
     └──────────────────┼──────────────────────────────────┘
                        ▼
     ┌───────────────── ANALYTICS SERVER ──────────────────────┐
     │  ufw: 22, 51820, 80, 443                                 │
     │                                                          │
     │  nginx :80  ─ ACME challenge only, else 301 → HTTPS       │
     │                                                          │
     │  nginx :443                                              │
     │   ├── grafana.example.com  allow <allowlist>; deny all;   │
     │   │        └─────────────────► 127.0.0.1:3000  Grafana    │
     │   └── f10.example.com      HTTP Basic Auth                │
     │            └────────────────► 10.77.0.10:8080  (via wg0)  │
     │                                                          │
     │                    10.77.0.1:8090  ingest (VPN only)     │
     │                       (compose net)   ClickHouse         │
     │  wg0 10.77.0.1                                           │
     └──────────────────────────────────────────────────────────┘
                        ▲
                        │ WireGuard, initiated OUTBOUND by the Pi
     ┌──────────────────┴──────────┐
     │  RASPBERRY PI  10.77.0.10   │
     │  :8080 dashboard (local)    │
     │  eth0 → BMW ENET (car)      │
     └─────────────────────────────┘
```

**Port 80 is open to the whole world, deliberately.** Let's Encrypt validates
the HTTP-01 challenge from many global IPs, so it cannot be allowlisted. That
vhost serves *only* `/.well-known/acme-challenge/` and redirects everything
else to HTTPS. It carries no application traffic.

**Port 443 is open to the world at the firewall, but filtered per vhost:**

| Host | Protection | Proxies to |
|---|---|---|
| `grafana.example.com` | nginx IP allowlist (`GF_ALLOWED_IPS`) → `403` otherwise | `127.0.0.1:3000` |
| `f10.example.com` | HTTP Basic Auth → `401` otherwise | `10.77.0.10:8080` over `wg0`; an "offline" page when the car is down |
| anything else (incl. bare IP) | no matching vhost / no certificate | — |

**Why Basic Auth for the dashboard and an allowlist for Grafana.** The
dashboard is meant to be viewed from a phone on mobile data, where your
address changes constantly, so an IP allowlist is unusable; it also has no
login of its own and serves the VIN, so the playbook refuses to publish it
without a password. Grafana has its own login and is normally used from a
small number of known networks, so the allowlist costs nothing there.

**Bare-IP access in this mode:** `http://<DROPLET_IP>` gets a 301 to
`https://<DROPLET_IP>`, which has no matching certificate — so it is a dead
end by design. Use the hostnames.

---

## The WireGuard tunnel

**Its main job is SSH access to the Raspberry Pi.** The Pi lives in the car
behind CGNAT or a phone hotspot: no public address, no inbound ports, and
almost never on the same LAN as your laptop. The VPS is the one machine with
a stable public address, so the Pi dials *out* to it and the VPS becomes the
jump host you reach the Pi through. Telemetry ingest happens to use the same
tunnel, which is a bonus rather than the reason it exists.

```
   LAPTOP                    VPS                        RASPBERRY PI
   (anywhere)                <DROPLET_IP>               (in the car, NATed)
       │                     wg0 10.77.0.1                  wg0 10.77.0.10
       │                          │                              │
       │  ssh root@<DROPLET_IP>   │                              │
       ├─────────────────────────►│                              │
       │      public, port 22     │   ssh pi@10.77.0.10         │
       │                          ├─────────────────────────────►│
       │                          │   over the WireGuard tunnel  │
                                  ▲                              │
                                  └──── tunnel dialled OUT ──────┘
                                        by the Pi, 51820/udp
```

**Only the Pi is a WireGuard peer.** Your laptop is not, and does not need to
be: it reaches the VPS over ordinary public SSH, and the VPS — being a tunnel
endpoint itself — reaches the Pi directly at `10.77.0.10`. Nothing is
forwarded between peers, so the `ip_forward` and `FORWARD` rules in
`wg0.conf` are not involved in this path at all.

Two hops, or one command with `ProxyJump`:

```bash
ssh root@<DROPLET_IP>            # hop 1: laptop → VPS (public SSH)
ssh pi@10.77.0.10               # hop 2: VPS → Pi (over wg0)

ssh -J root@<DROPLET_IP> pi@10.77.0.10     # both hops in one go
```

Put it in `~/.ssh/config` to make it a single name:

```
Host f10pi
    HostName 10.77.0.10
    User f10
    ProxyJump root@<DROPLET_IP>
```

The Pi's client config still needs the server's VPN address in `AllowedIPs`
(`10.77.0.0/24` covers it), and `PersistentKeepalive = 25` so the NAT mapping
stays open and the VPS can reach back into the tunnel.

| Flow | Path | Notes |
|---|---|---|
| **SSH to the Pi** | laptop → VPS (public 22) → Pi `10.77.0.10:22` (wg0) | **the point of the tunnel**; VPS is the jump host, laptop needs no VPN |
| Tunnel establishment | Pi → `<DROPLET_IP>:51820/udp` | dialled outbound; keepalive holds the NAT mapping open |
| Telemetry upload | Pi → `10.77.0.1:8090` over `wg0` | bearer token; never crosses the public internet in the clear. Ingest is **published on the VPN address**, so this only works if `INGEST_BIND` is that address — bound to loopback the Pi just times out |
| Dashboard viewing | phone → nginx → `10.77.0.10:8080` over `wg0` | Case B only; public path, no VPN on the phone |

Adding your laptop as a second peer is possible but unnecessary for this
model; it only helps if you want the Pi reachable without the VPS SSH hop.
That variant needs `AllowedIPs = 10.77.0.0/24` on every client and relies on
the peer-to-peer forwarding rules.

### First-time setup (before the tunnel exists)

There is a bootstrap gap: until WireGuard is configured on the Pi, none of
the above works. For that first configuration use the LAN
(`ssh <PI_USER>@f10pi.local`) or a monitor and keyboard. The same applies if
the tunnel ever breaks — see
[`recovery.md`](../hardware/raspberry-pi/f10pi/docs/recovery.md).

Note the asymmetry: **viewing the dashboard needs no VPN** (nginx proxies it
over the tunnel on your behalf), whereas **SSH does** — there is no public SSH
path to the Pi, by design.

The Pi's own interfaces stay strictly separated — `wlan0` is the only default
route, and `eth0` is a link-local island for the BMW ENET cable with no
gateway and no DNS. See
[`hardware/raspberry-pi/f10pi/docs/networking.md`](../hardware/raspberry-pi/f10pi/docs/networking.md).

---

## What is never exposed

| Service | Binding | Reached by |
|---|---|---|
| **ClickHouse** | **not published at all** — compose network only | the ingest server and Grafana inside the compose network; humans via `docker compose exec` on the host |
| ingest server | `INGEST_BIND:8090` — the **WireGuard address**, e.g. `10.77.0.1` | the Pi over `wg0` only |
| Grafana | `127.0.0.1:3000` | nginx only |
| Pi dashboard | Pi's own `:8080` | nginx over `wg0` only |

Because ClickHouse publishes no host port, a WireGuard client cannot query it
directly. To run SQL against the lake, use the host:

```bash
ssh root@<DROPLET_IP>
cd /opt/f10-dashboard/infra && docker compose exec -T clickhouse clickhouse-client ...
```

`make lake-status` does exactly this for you.

---

## Verifying the real state

Never trust the diagram over the machine. These read the live configuration:

```bash
# layer 1 - cloud firewall
doctl compute firewall list                  # or the DigitalOcean console

# layers 2-4, on the server
ssh root@<DROPLET_IP>
  ufw status verbose                          # host firewall
  ss -ltnp                                    # what actually listens, and on which address
  docker ps --format '{{.Names}}  {{.Ports}}' # which container ports are published
  nginx -T | grep -E 'server_name|listen|allow|deny|auth_basic'
  wg show wg0                                 # tunnel peers and last handshake

# from outside
curl -sI https://grafana.example.com | head -1   # 302 if allowlisted, 403 if not
curl -sI https://f10.example.com     | head -1   # 401 until you authenticate
```

A quick summary of most of this is `make lake-status`.

---

## Threat-model notes

- **SSH is open to the world by default** (`ssh_allowed_cidrs` defaults to
  `0.0.0.0/0`). Authentication is key-only — the base role disables password
  auth — but restricting this to your own addresses is a cheap, worthwhile
  hardening step.
- **Case A sends the Grafana password in cleartext.** The allowlist limits
  who can connect, not who can read the traffic in transit.
- **Basic Auth is only as good as its password**, and it protects a dashboard
  that serves the VIN. Use a generated value. `live.py --redact-vin` will mask
  the VIN in the HTTP/SSE API if you ever expose the dashboard more widely;
  it does not affect what is stored locally or in the lake.
- **A secret containing `$` must not be routed through Make.** Ansible reads
  `infra/.env` directly for this reason; see `PROVISIONING.md`.
- **Everything here is one host.** A compromise of the analytics server
  reaches the lake and the VPN, and therefore the Pi. The car link itself is
  read-only by design, but treat server access as equivalent to car-data
  access.
