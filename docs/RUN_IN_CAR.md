# Running in the car + operations runbook

Everything you need to run a drive session, sync it, analyse it, and host
the whole thing in the car. This is the operational companion to
`CLAUDE.md` (what the project is) and `docs/ROADMAP.md` (where it's going).

Nothing here contains the VIN, secrets, or the VPS IP — those live in the
owner's notes and gitignored files (see "Secrets to bring", below).

---

## 1. Run a drive session (the workflow we actually use)

Three processes on the in-car host (laptop / Pi), each read-only on the car:

```bash
# 1) the logger + dashboard — ALWAYS via run_car.sh so every verified
#    channel (gear, DPF, gearbox, temps...) is loaded. A bare `live.py`
#    omits them and the dashboard shows "N" for gear.
./run_car.sh                         # logs to local/sessions/drive-<ts>.db, dashboard :8080

# 2) the sync agent — ships the session to the ClickHouse lake over mobile
python3 infra/sync/agent.py --config infra/sync/config.json
```

- Open the **Drive view** at `http://<host>:8080/` for the M-Performance
  cluster (rev bar, tach + speedo, big gear, tiles). Detail = history
  graphs; All-data = every channel.
- The **ZGW serves ONE HSFZ client at a time.** Never run `live.py` and a
  validation tool (`tools/validate_candidate.py`, `tools/egs.py`) at once
  — stop the logger first. The sync agent does NOT touch the car (it
  reads the SQLite file read-only), so it runs alongside the logger fine.
- Ignition on; engine running for anything load/flow/temperature-related.

After the drive, analyse it:

```bash
python3 -m analysis.session_report --db local/sessions/drive-<ts>.db --out drive-sessions
#   -> drive-sessions/<ts>-session/report.md + summary.json + curves.html
#   (warm-up, DDE-vs-OBD cross-checks, load/gear behaviour, DPF, data quality)
```

Reports are VIN-redacted and safe to commit; the raw session DB stays
gitignored under `local/sessions/`.

## 2. Reaching the data (ClickHouse + Grafana)

The lake is **deployed and running on a VPS** (`infra/`, docker compose:
ClickHouse + the ingest server + Grafana). It accumulates every drive.

- **Grafana** — the `f10-health` dashboard (DPF ΔP-vs-flow baseline, soot
  accumulation, boost/rail tracking, decode cross-check, data quality).
  Reach it per [`infra/NETWORK.md`](../infra/NETWORK.md) — an SSH tunnel,
  an IP-allowlisted port, or an HTTPS hostname, depending on how you
  configured it. Pick the car with the `Vehicle` (VIN) variable.
- **Ad-hoc SQL** — `analysis/clickhouse/insights.sql` is a ready battery;
  run it with `clickhouse-client --param_vin=<VIN> --multiquery < …`.
- The **ingest server is the only writer** into ClickHouse; the sync
  client never talks to CH directly. Channel names are normalised
  server-side (`infra/ingest/channel_map.json`), and `channel_raw` is
  always kept so the map can change without re-uploading.

The server is provisioned as code — Terraform for the droplet, Ansible for
everything on it. Deploy or redeploy with `cd infra && make deploy`; the
full procedure is [`infra/PROVISIONING.md`](../infra/PROVISIONING.md).
Do not configure the VPS by hand: a `make deploy` will overwrite it.

## 3. Hosting it in the car

The runtime is **stdlib-only** (no `pip install`), which is what makes a
constrained host viable. The catch is always the **ENET link**, not the
Python.

### Raspberry Pi (the in-car host — commissioned 2026-08-28)
Full Linux, so everything except the IP assignment is trouble-free.
See `docs/PI_COMMISSIONING.md` for the full build record.
- Pi USB/Ethernet → the RJ45 ENET/OBD cable → car gateway.
- **The Pi does NOT self-assign a `169.254.x.x` address out of the box.**
  A default NetworkManager profile is `ipv4.method: auto` (DHCP), and the
  ZGW serves no DHCP — so the interface sits at "getting IP
  configuration" forever with no IPv4 and discovery finds nothing. Set
  the profile to link-local once, and it is then permanent:

      sudo nmcli connection modify <eth-profile> \
          ipv4.method link-local ipv4.may-fail yes ipv6.method ignore

  After that, discovery + `run_car.sh` work as on a laptop.
- `find_link_local_ip()` shells out to `ifconfig`; `net-tools` is present
  on the Pi image in use, so no code change was needed.
- Give it mobile data (USB dongle / phone tether) for the sync agent.
  An iPhone hotspot + a WireGuard tunnel to the VPS is what is in use;
  measured 6.1 Mbit/s up, far more than sync needs.
- **Watch the cable.** On the first Pi drive the ENET cable lost carrier
  for 2m46s over a bump, splitting the session into 7 runs. A Pi in a
  moving car stresses the connector in a way a parked laptop never did.
- Auto-start on boot with systemd units; the dashboard is reachable on
  the car's local network / a phone over WiFi.

### Android phone (interim option — feasible, fiddlier)
- **Termux** (from F-Droid) → `pkg install python git` → clone → run.
  The stdlib runtime runs nearly as-is.
- Hardware: a **USB-OTG → Ethernet adapter** (AX88179 / RTL8153 chipsets
  are the safe bets) → the ENET cable.
- **The hard part:** Android often won't auto-assign a `169.254.x.x`
  address to a USB-Ethernet interface. Rooted → one-liner
  (`ip addr add 169.254.10.10/16 dev eth0`); unrooted → hit-or-miss.
  **Test that single thing first** before anything else.
- Advantages: the phone *is* the display (M-cluster on its screen) and
  *is* the mobile uplink for sync.

### Portability gotchas (both hosts)
- `find_link_local_ip()` in `live.py` shells out to **`ifconfig`** — fine
  on a Pi, but replace with pure-Python interface enumeration / `ip addr`
  for hosts without net-tools (Android). Small, low-risk change; not yet
  done.
- If UDP-broadcast discovery is blocked, pass `--ip <gateway>` and
  `--local-ip <host-ll-ip>` to `run_car.sh` (it forwards extra flags) to
  skip discovery. The gateway is link-local and can move, so prefer
  discovery where it works.

## 4. Validating a NEW channel (the discipline)

New candidate channels are `verification.status: candidate` until proven
ON THIS CAR. Never invent identifiers/scales — source them (SGBD table,
capture, community) with provenance. Validate read-only:

```bash
# stop live.py first (one HSFZ client)
python3 tools/validate_candidate.py run  <candidate.yaml> --all --step   # one-shot reads
python3 tools/validate_candidate.py sweep <candidate.yaml> <req...> --seconds 30  # while driving
```

Confirm the values are physically right (cross-check vs OBD, vs known
state, vs physics), then flip `verification.status: verified` with the
evidence in `method:`. Every run writes VIN-redacted artifacts to
`validation-runs/`. A community DID that doesn't pan out gets `rejected`
with the reason (see `f10_gear.yaml`, `f10_transmission.yaml` history).

## 5. Open threads (pick up here)

- **P/R/N/D selector** — the engaged gear (1..8) is found (EGS `DA2E`
  byte 1); a clean selector is NOT. Needs `tools/egs.py scan --ecu 0x18`
  (parked, engine on) then correlate on a drive. See
  `research/reports/n47-next-session.md`.
- **Analytics layer** — the biggest unbuilt piece. The lake + Grafana are
  live and accumulating; next is condition-normalised baselines + drift
  detection (`docs/ROADMAP.md` Stages 3–5). ASOF-JOIN operating points
  are already demonstrated in `insights.sql`.
- **Android portability** — the `ifconfig` fix + a static-IP profile if
  the phone route is pursued.
- **Verified mappings live in `mappings/candidates/`** with
  `status: verified` (not moved to `mappings/verified/`, left in place on
  purpose). If you ever move them, update `run_car.sh`, the tests, and the
  docs that reference the paths.

## 6. Secrets to bring (NOT in git)

Copy these to the in-car host manually (e.g. `scp` from the laptop):

- `infra/sync/config.json` — ingest server URL + bearer token (so the
  agent can sync).
- `local/VEHICLES.md` — the VIN↔label table (only if needed locally).
- The VPS IP + Grafana admin password + ClickHouse password live in the
  VPS `.env` and the owner's notes — never commit them.
