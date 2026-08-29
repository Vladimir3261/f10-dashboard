# Raspberry Pi in-car host — on-car findings

What was learned putting a bare Pi 4B in the car as the telemetry host,
recorded because these are facts about *this* hardware and *this* vehicle
link that are not derivable from the code.

This is a **findings record, not a setup guide.** The reproducible setup is:

- **the Pi** — [`hardware/raspberry-pi/f10pi/`](../hardware/raspberry-pi/f10pi/README.md)
- **the server** — [`infra/PROVISIONING.md`](../infra/PROVISIONING.md)
- **the network** — [`infra/NETWORK.md`](../infra/NETWORK.md)

No VIN, no tokens, no passwords here, and no public IPs.

## 1. Host

Raspberry Pi 4B, Debian-based, Python **3.13.5**, 1.8 GB RAM, 23 GB free.
`net-tools` present, so `find_link_local_ip()` (which shells out to
`ifconfig`) works unmodified.

The whole runtime is stdlib-only, so there was nothing to install:
`live.py`, `bmwdiag/`, `analysis/` and `infra/sync/agent.py` all run as-is,
and the test suite passes on the Pi with no car and no network. That
portability is what makes the Pi viable as an embedded host at all.

## 2. The ENET link — the one real blocker

The single genuine obstacle to running in the car, and the reason
`configure-eth0-bmw.sh` sets `ipv4.method link-local` explicitly.

**Symptom.** Cable plugged in, `carrier=1`, `operstate=up`, but `nmcli`
stuck at `connecting (getting IP configuration)` and **no IPv4 address**.
Discovery could not run.

**Cause.** The NetworkManager profile was `ipv4.method: auto` — DHCP only.
The BMW ZGW does not serve DHCP; ENET expects the host to self-assign an
IPv4 **link-local** address. NetworkManager therefore waited for a lease
that never comes.

**Fix** (persistent, survives reboot, scoped to `eth0` only):

```bash
sudo nmcli connection modify <eth0-profile> \
    ipv4.method link-local ipv4.may-fail yes ipv6.method ignore
sudo nmcli connection up <eth0-profile>
```

Result: `eth0` takes a `169.254.x.x/16` address and the gateway is
discovered on the same subnet. `wlan0` and `wg0` are untouched, and the
default route stays on the mobile uplink.

> A Pi does **not** self-assign a link-local address out of the box under
> NetworkManager with a DHCP-method profile. This has to be set explicitly.

## 3. Uplink, measured

WiFi to a phone hotspot with a WireGuard tunnel to the server. Measured over
cellular, parked:

| path | result |
|---|---|
| ping server over `wg0` | 0% loss, ~97 ms |
| SSH over `wg0` | connects in 1.8 s |
| ingest `/health` over `wg0` | HTTP 200 in 0.25 s |
| upload throughput | **6.1 Mbit/s** |

Far more than sync needs: the wire format measured **2.4 bytes/sample** in
practice, so a full drive is a few MB.

## 4. Cable reliability — the recurring failure

A drive fragmented into 7 runs because the ENET cable lost carrier for
2m46s mid-drive (`carrier-changed` in the NetworkManager journal),
producing 82 × `Errno 99` as socket binds failed with no address on the
interface.

**This is physical, not software.** Repeated short runs or bursts of
`Errno 99` mean the connector, not the code. `live.py` re-detects the
link-local interface on every reconnect attempt, so it recovers by itself
once the cable seats again.

## 5. Decode finding — OBD MAP saturates, the DDE does not

Standard OBD MAP saturates at 255 kPa while the DDE reads true manifold
pressure beyond it. The recurring boost cross-check warning in earlier
reports is **generic-sensor saturation, not a decode error**, and
`n47d_boost_act` is the accurate channel above 250 kPa.

## 6. Open issues

- **ClickHouse self-logging dwarfs the telemetry.** On a small box the
  `system.*` log tables (~88 MiB) far exceeded the actual telemetry
  (~6 MiB) and background merges hit the memory limit. Fix is a config
  drop-in: TTL the system log tables to ~7 days and disable
  `asynchronous_metric_log` / `trace_log`. **Not yet done.** Less urgent on
  a larger droplet, but it will creep back.
- **Sessions are never closed.** Many `sessions` rows have `ended IS NULL`:
  only a clean SIGINT shutdown writes `ended_at`, while a transport
  reconnect starts a new run and leaves the old one open. Cosmetic now, but
  it will skew any session-duration analytics.
- ~~**The dashboard's sync indicator is wrong when proxied.**~~ **Fixed.**
  The page hardcoded `http://<location.hostname>:8091/sync/status`, which
  only resolves when the dashboard is opened on the Pi itself; through the
  reverse proxy the fetch failed and the chip read "off" while sync was
  healthy. `live.py` now serves a same-origin, read-only `/api/sync` that
  proxies status from `127.0.0.1:8091`. Pause/resume are deliberately NOT
  proxied — the agent exposes them unauthenticated, so they stay reachable
  only from the Pi itself.
