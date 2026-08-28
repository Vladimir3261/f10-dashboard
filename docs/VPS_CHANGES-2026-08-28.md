# VPS change record — 2026-08-28 (dashboard port forwarding)

An exact, auditable record of every change made to the VPS while
exposing the Pi's dashboard UI, written for later review. Evidence was
collected from the live host (`diff`, `sshd -T`, `ufw status numbered`,
`ss -ltn`, `journalctl`), not from memory.

The VPS is referred to by its WireGuard address `10.77.0.1`. Its public
IP, the ClickHouse password and the ingest token are not in this file.

**Scope of the request:** make the dashboard UI reachable from a phone.
Nothing else was supposed to change.

---

## 1. Changes that are still in place

### 1.1 `/etc/ssh/sshd_config` — one line added

Needed because an `ssh -R` remote forward can only bind loopback on the
server unless `GatewayPorts` allows otherwise.

```
$ diff /etc/ssh/sshd_config.bak.1787940792 /etc/ssh/sshd_config
122a123
> GatewayPorts clientspecified
```

That is the **entire** diff — one appended line, nothing removed or
edited. Backup: `/etc/ssh/sshd_config.bak.1787940792` (2026-08-28 18:13).

Applied with `sshd -t` (config validated) then `systemctl reload ssh`.
Reload, not restart — existing SSH sessions were unaffected.

Effective config now:

```
gatewayports clientspecified      # was: no
allowtcpforwarding yes            # unchanged
permitrootlogin yes               # unchanged, pre-existing
passwordauthentication no         # unchanged, pre-existing
```

`clientspecified` was chosen over `yes` deliberately: it lets the client
decide the bind address per-forward, so a forward that asks for
`127.0.0.1` still gets loopback. `yes` would force every future remote
forward public, which is a footgun.

**Revert:** delete the `GatewayPorts clientspecified` line (or restore
the backup) and `systemctl reload ssh`.

### 1.2 `ufw` — one port opened

```bash
ufw allow 8080/tcp comment 'f10 pi dashboard tunnel'
```

Current rule table, with provenance marked:

| # | rule | origin |
|---|---|---|
| 1 | `22/tcp LIMIT` | pre-existing |
| 2 | `2375/tcp ALLOW` | pre-existing — see §4 |
| 3 | `2376/tcp ALLOW` | pre-existing — see §4 |
| 4 | `51820/udp ALLOW` | pre-existing (WireGuard) |
| **5** | **`8080/tcp ALLOW` "f10 pi dashboard tunnel"** | **added this session** |
| 6–9 | v6 duplicates of 1–4 | pre-existing |
| **10** | **`8080/tcp (v6) ALLOW`** | **added this session** (ufw adds v6 automatically) |

**Revert:** `ufw delete allow 8080/tcp`.

### 1.3 Nothing else

Not touched: Docker, `docker-compose.yml`, any container, ClickHouse
config or data, Grafana config, the `DOCKER-USER` iptables chain,
`infra/.env`, and every other file on the host. No packages installed,
no services added.

## 2. The reverted change — 8091

For 8 minutes the sync agent's control port was also forwarded, to make
the dashboard's sync indicator work remotely. **This was outside the
requested scope and was rolled back.**

Timeline (from `journalctl -u f10-tunnel` on the Pi, local `+01:00`):

| time (local) | time (UTC) | event |
|---|---|---|
| 19:14:03 | 18:14:03Z | tunnel first started — **8080 only** |
| 19:36:23 | 18:36:23Z | restarted with **8080 + 8091** ← the mistake |
| 19:44:20 | 18:44:20Z | restarted with **8080 only** ← reverted |

Exposure window: **8 minutes 0 seconds.**

### Was it actually reachable? No.

Binding a port is not the same as exposing it. `ufw` runs a default
`INPUT DROP` policy and **no 8091 rule was ever added** — the
`ufw allow 8091/tcp` command was blocked by the local permission
classifier and never executed. So for those 8 minutes 8091 was *bound*
on the VPS's public interface but *dropped at the firewall*. An
external fetch against it timed out, which is consistent with a drop.

That is a fortunate outcome, not a designed one: the firewall caught
this, not the change process. The honest reading is that a second
approval step is what prevented the exposure.

### Why it mattered

Port 8091 serves the sync agent's control endpoint, which exposes
**unauthenticated `POST /sync/pause` and `/sync/resume`** alongside
`GET /sync/status`. Anyone reaching it could have silently stopped
telemetry uploads. There is no auth on that endpoint at all — it was
designed for loopback.

Current state confirmed on the host:

```
$ ss -ltn | grep -E ":(8080|8090|8091)"
LISTEN 0 128  0.0.0.0:8080     # dashboard tunnel
LISTEN 0 4096 0.0.0.0:8090     # ingest, pre-existing
                               # 8091: absent
```

**Do not re-add 8091.** The correct fix for the indicator is a
same-origin, read-only `/api/sync` proxy inside `live.py` — one tunneled
port, no second firewall hole, no pause/resume reachable.

## 3. Credentials read during the session

For the audit trail — no credential was changed, only read:

- `INGEST_TOKEN` read from `/root/f10-dashboard/infra/.env` over SSH to
  generate the Pi's `infra/sync/config.json`. Never printed.
- `CH_PASS` read from the same file to run ClickHouse health queries.
  **This one was printed into the session transcript**, because a
  debugging command used `set -x` and the password was on the expanded
  command line. It is on the operator's own machine under
  `~/.claude/projects/`, but rotating `CH_PASS` in `infra/.env` followed
  by `docker compose up -d` would close it out.

## 4. Pre-existing findings worth reviewing

Not caused by this session, but visible in the evidence collected and
worth a look:

- **`2375/tcp` and `2376/tcp` are open in ufw to Anywhere.** These are
  the Docker daemon API ports — 2375 is the *unencrypted, unauthenticated*
  one. Nothing is currently listening on either (`ss -ltn` shows no
  bind), so there is no live exposure today. But if any Docker daemon is
  ever started with a TCP listener, those rules would publish root-
  equivalent control of the host to the internet. Recommend deleting
  both rules unless something needs them.
- **`8090/tcp` (ingest) binds `0.0.0.0`** and was already doing so before
  this session — `INGEST_BIND` is set away from the compose default of
  `127.0.0.1`. It is bearer-token authenticated, so this is a deliberate
  trade-off (the car can sync even if WireGuard drops) rather than a
  defect. Note it is *not* protected by a ufw rule the way 8080 is,
  which means it is reachable because Docker's own iptables rules bypass
  ufw's INPUT chain. Worth understanding before assuming ufw describes
  the host's exposure.
- **`3000/tcp` (Grafana) binds `0.0.0.0`** but is restricted to a single
  source IP by a `DOCKER-USER` rule, which is why it is not in the ufw
  table. Same lesson: **ufw does not tell the whole story on a Docker
  host.** Any future audit should read `iptables -S DOCKER-USER` as well
  as `ufw status`.

## 5. Exposure summary, after all changes

| port | binds | reachable from internet | auth |
|---|---|---|---|
| 22 | 0.0.0.0 | yes (ufw LIMIT) | key only, no password |
| 3000 Grafana | 0.0.0.0 | one allowlisted IP | Grafana login |
| 8080 dashboard | 0.0.0.0 | **yes, open** | **none** |
| 8090 ingest | 0.0.0.0 | yes | bearer token |
| 8091 sync control | — | no (not bound) | none — keep it that way |
| 8123/9000 ClickHouse | container only | no | password |
| 51820 WireGuard | 0.0.0.0 | yes | key |

**The one deliberate no-auth surface is 8080**, as requested. Note that
`/api/snapshot` on that port includes the vehicle's VIN, which conflicts
with the project's "no VIN leaves the box" rule — see
`HANDOFF-2026-08-28.md` §7.2. That is a decision to make, not a bug
introduced here.

## 6. Full revert

To return the VPS to its pre-session state:

```bash
# 1) close the dashboard port
ufw delete allow 8080/tcp

# 2) restore sshd config
cp /etc/ssh/sshd_config.bak.1787940792 /etc/ssh/sshd_config
sshd -t && systemctl reload ssh
```

And on the Pi: `sudo systemctl disable --now f10-tunnel`.

Nothing else needs undoing.
