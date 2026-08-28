# Raspberry Pi in-car host — commissioning record

What was actually done to turn a bare Pi 4B into the in-car host, and what
was changed on the VPS to support it. Written 2026-08-28, the first
session run from the Pi rather than the laptop.

No VIN, no tokens, no passwords here. The VPS is referred to by its
WireGuard address `10.77.0.1`; its public IP is in the owner's notes.

## 1. Host

Raspberry Pi 4B, Debian-based, Python **3.13.5**, 1.8 GB RAM, 23 GB free.
`net-tools` present, so `find_link_local_ip()` (which shells out to
`ifconfig`, live.py:479) works unmodified — the portability caveat in
`RUN_IN_CAR.md` applies to Android, not to this Pi.

The whole runtime is stdlib-only, so there was nothing to install:
`live.py`, `bmwdiag/`, `analysis/` and `infra/sync/agent.py` all run as-is.
294 tests pass on the Pi with no car and no network.

## 2. The ENET link — the one real blocker

**Symptom.** Cable plugged in, `carrier=1`, `operstate=up`, but
`nmcli` stuck at `connecting (getting IP configuration)` and **no IPv4
address**. Discovery could not run.

**Cause.** The `netplan-eth0` NetworkManager profile was `ipv4.method:
auto` — DHCP only, with `ipv4.link-local: 0 (default)`. The BMW ZGW does
not serve DHCP; ENET expects the host to self-assign an IPv4 link-local
address. So NM waited for a DHCP lease that never comes.

**Fix** (persistent, survives reboot, scoped to eth0 only):

```bash
sudo nmcli connection modify netplan-eth0 \
    ipv4.method link-local ipv4.may-fail yes ipv6.method ignore
sudo nmcli connection up netplan-eth0
```

Result: `eth0  169.254.22.202/16`, gateway discovered at
`169.254.65.67`. `wlan0` and `wg0` untouched; the default route stays on
the mobile uplink.

To revert: `ipv4.method auto`.

> `RUN_IN_CAR.md` claims the Pi "self-assigns a 169.254.x.x link-local
> address ... works natively". That is **not** true under
> NetworkManager with a DHCP-method profile. Corrected there.

## 3. Uplink

WiFi to an iPhone personal hotspot (`172.20.10.x/28`, the standard iOS
hotspot subnet), with a WireGuard tunnel `wg0` (10.77.0.10) to the VPS.

Measured over cellular, parked:

| path | result |
|---|---|
| ping VPS over wg | 0% loss, ~97 ms |
| SSH over wg | connects in 1.8 s |
| ingest `/health` over wg | HTTP 200 in 0.25 s |
| upload throughput | **6.1 Mbit/s** (2 MB in 2.6 s) |

Far more than sync needs: the wire format measured **2.4 bytes/sample**
in practice (713 rows in 1709 bytes), so a full drive is a few MB.

## 4. Sync agent

`infra/sync/config.json` is gitignored and had never been copied to this
host, which is the only reason sync was off. Regenerated locally on the
Pi (mode `0600`) with the token read from the VPS `.env` over SSH.

**`server_url` points at `http://10.77.0.1:8090` — the ingest server over
WireGuard, not the public IP**, so the bearer token never crosses the
open internet. Keep it that way.

Verified end to end: 324,977 rows shipped, `synced_rowid == max_rowid`,
0 pending, no errors, ~5 s steady-state lag.

## 5. Public dashboard tunnel

The dashboard UI (and only the dashboard UI) is exposed publicly, by
request, for viewing from a phone while driving.

**On the Pi** — `/etc/systemd/system/f10-tunnel.service`, enabled:

```
ssh -NT -i <key> -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
    -R 0.0.0.0:8080:127.0.0.1:8080 root@10.77.0.1
Restart=always
```

`Restart=always` plus the keepalives mean it reconnects itself when the
hotspot blips — which matters in a moving car.

**On the VPS** — two changes:

- `/etc/ssh/sshd_config`: added `GatewayPorts clientspecified` (backup at
  `sshd_config.bak.<epoch>`), validated with `sshd -t`, `systemctl reload
  ssh`. Without it a remote forward can only bind loopback.
- `ufw allow 8080/tcp` ("f10 pi dashboard tunnel").

To undo: `sudo systemctl disable --now f10-tunnel` on the Pi, and
`ufw delete allow 8080/tcp` on the VPS.

### Scope boundary — read this before adding ports

**Only 8080 is forwarded. Only 8080 is open.** The ingest server (8090)
and the sync agent's control endpoint (8091) stay behind auth / off the
public internet.

During this session 8091 was briefly added to the tunnel to make the
dashboard's sync indicator work remotely, and **that was a mistake** —
8091 exposes unauthenticated `/sync/pause` and `/sync/resume`, so
anyone reaching it could silently stop uploads. It was reverted the same
session. It was never actually reachable (ufw `INPUT DROP` never had an
8091 rule), but do not re-add it.

The correct fix for the indicator is in §7 below.

## 6. VPS state observed

Stack healthy: `infra-clickhouse-1` (24.8.14.39) healthy, `infra-ingest-1`
and `infra-grafana-1` (11.2.0) up. Database is **`telemetry`**, not
`f10`. Disk 15% used.

Grafana on `:3000` is restricted to one IP by a DOCKER-USER rule; from
anywhere else use an SSH tunnel:

```bash
ssh -i <key> -N -L 3000:127.0.0.1:3000 root@10.77.0.1
```

### Open issue — ClickHouse is OOMing on background merges

The VPS has 1.9 GB RAM and ClickHouse is capped at 840 MiB
(`max_server_memory_usage_to_ram_ratio: 0.6`). A background merge died
with `MEMORY_LIMIT_EXCEEDED` on 2026-08-28 18:00 UTC, and an ad-hoc query
against `system.text_log` was killed by the same limit.

Cause: **ClickHouse's own logging dwarfs the telemetry.**

| table | disk | rows |
|---|---|---|
| `system.text_log` | 30.90 MiB | 152,566 |
| `system.metric_log` | 30.66 MiB | 183,112 |
| `system.asynchronous_metric_log` | 20.75 MiB | 34,085,336 |
| **`telemetry.samples`** | **6.18 MiB** | 1,049,619 |

~88 MiB of self-instrumentation against 6 MiB of actual car data, 421 MB
total in `/var/lib/clickhouse`. Fix is a config drop-in: TTL the system
log tables to ~7 days and disable `asynchronous_metric_log` /
`trace_log`. **Not yet done.**

### Open issue — sessions never closed

32 of 58 `sessions` rows in ClickHouse have `ended IS NULL`. Same root
cause as the run fragmentation in §7: only a clean SIGINT shutdown writes
`ended_at`; a transport reconnect starts a new run and leaves the old one
open. Cosmetic now, but it will skew any session-duration analytics.

## 7. Known issues to pick up

- **Dashboard sync indicator reads "off" when viewed remotely.** The page
  hardcodes `http://<location.hostname>:8091/sync/status` (live.py:1964).
  Through the tunnel that resolves to the VPS, where 8091 is not exposed
  (deliberately, §5), the fetch times out and the `catch` paints the dot
  red (live.py:1978). Sync itself is unaffected.
  **Fix:** add a same-origin, read-only `/api/sync` endpoint to live.py
  that proxies status from `127.0.0.1:8091`, and point the page at it.
  One tunneled port, no second firewall hole, no pause/resume exposed.
- **The public dashboard serves the VIN.** `/api/snapshot` includes a
  `vin` field and the tunnel has no auth, so the VIN is currently
  reachable by anyone who finds port 8080. This is contrary to the
  project's "no VIN leaves the box" rule. Fix is a one-line omission in
  the snapshot payload — the dashboard does not need it to render — and/or
  an IP allowlist on 8080.
- **ClickHouse system-log TTL** (§6).
- The sync agent's control endpoint is `/sync/status`, not `/status`.

## 8. Ops quick reference

```bash
./run_car.sh                                              # logger + dashboard
python3 infra/sync/agent.py --config infra/sync/config.json   # ship to lake
systemctl status f10-tunnel                               # public dashboard
python3 -m analysis.session_report --db local/sessions/<db> --run N --out drive-sessions
```

Stop `live.py` before any validation tool — the ZGW serves one HSFZ
client at a time. The sync agent reads SQLite read-only and is safe to
leave running alongside.
