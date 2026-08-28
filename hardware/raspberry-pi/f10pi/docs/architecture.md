# f10pi architecture

The Pi is the in-car host for the read-only telemetry runtime. Three
network interfaces, each with one job, kept strictly separate:

```
                       ┌────────────────────────────────────────┐
                       │            Raspberry Pi 4 (f10pi)        │
                       │                                          │
  Internet  ~~wifi~~►  │  wlan0 ── default route ── cloud/lake    │
  (home / hotspot /    │           (ClickHouse lake, git, updates)│
   in-car LTE)         │                                          │
                       │  wg0   ── management VPN ── your laptop   │
  admin  ~~wg~~►       │           (SSH, ClickHouse over tunnel)  │
                       │                                          │
  BMW F10   ──cable──► │  eth0  ── link-local ONLY ── BMW ENET    │
  (ENET/OBD)           │           (169.254.x.x, no default route)│
                       └────────────────────────────────────────┘
                                        │
                                   live.py + bmwdiag
                                   (:8080 dashboard, SQLite log)
                                        │
                                   sync agent ──► lake (over wlan0)
```

## Interface roles (never blurred)

| iface | role | route | notes |
|---|---|---|---|
| `wlan0` | Internet | **default** | home Wi-Fi / hotspot / LTE, by priority |
| `wg0` | management VPN | management subnet only | reach the Pi + reach the server privately |
| `eth0` | BMW ENET | **never default**, link-local | 169.254.x.x, no DNS, dedicated to the car |

The one hard rule that makes this safe: **`eth0` must never carry the
default route and never provide DNS.** The BMW gateway is an untrusted,
isolated link; all real Internet traffic goes out `wlan0`.
`configure-eth0-bmw.sh` and `verify.sh` both assert this.

## The application

- **`live.py` + `bmwdiag`** — stdlib-only Python. Discovers the BMW
  gateway on `eth0` by UDP broadcast, speaks HSFZ, polls read-only, logs
  to SQLite, and serves the dashboard on `:8080`. Launched via
  `run_car.sh` (loads every verified channel) by `f10-dashboard.service`.
- **sync agent** (`infra/sync/agent.py`) — reads the SQLite logs
  read-only and ships drives to the ClickHouse lake over `wlan0` (reaching
  the server via its `wg0` address). Runs as `f10-sync.service`.

Because the runtime is stdlib-only, provisioning needs **no pip installs
and no virtualenv** — a major portability win for an embedded host.

## Boot sequence

1. systemd brings up networking; NetworkManager connects the
   highest-priority Wi-Fi in range (`wlan0`).
2. `wg-quick@wg0` establishes the management tunnel (the Pi initiates, so
   it works through CGNAT).
3. `bmw-enet` profile assigns the fixed link-local address to `eth0`.
4. `f10-dashboard.service` starts the runtime; it retries until the BMW
   gateway answers, so the car being off at boot is fine.
5. `f10-sync.service` starts shipping any pending drives when Internet is
   up.

See [`networking.md`](networking.md), [`wireguard.md`](wireguard.md),
[`ssh.md`](ssh.md), and [`bmw-enet.md`](bmw-enet.md) for each piece, and
[`recovery.md`](recovery.md) for failure handling.
