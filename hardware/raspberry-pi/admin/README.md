# f10-admin — the Pi's front door

One page, one address, one login: the car's telemetry views and the
things you would otherwise SSH in to do, from the driver's seat.

```
http://<pi-lan-ip>:8088/          (and http://10.77.0.10:8088/ over WireGuard)
```

Six tabs. **Drive · Detail · All data** are the telemetry UI — the
`dashboard/` files that `live.py` serves, shown here unchanged inside a
frame, with their API reverse-proxied to the runtime behind this panel's
authentication. **System · Car link · Claude** are the box: whether the
runtime is alive, why it isn't, pull a fix, restart it, shut down cleanly
before cutting the power. The active tab is the URL fragment
(`#drive`, `#system`, …), so a bookmark lands on a tab.

The runtime itself listens on the loopback only
(`f10-dashboard.service` passes `--host 127.0.0.1` through `run_car.sh`),
so `:8080` is not a second, unauthenticated way in; this panel is the
only one. Share links (`/s/?t=…`) pass straight through to `live.py`,
which stays the authority on its own tokens — a shared link never sees
this panel's login and can never reach anything of it.

## When live.py is down

The page still loads and every management action still works — that is
what a separate unit is for. The telemetry tabs show a *runtime not
running* banner (the proxy answers `503` with a JSON body the page
understands rather than a broken frame), the System tab's **restart**
brings it back, and the views resync on their own once it answers.

## Install

On the Pi, once:

```bash
cd ~/f10-dashboard/hardware/raspberry-pi/admin && sudo ./install.sh
```

It generates `config.json` with a random password and the Pi's detected
addresses (the LAN one, and `wg0`'s if the tunnel is configured),
installs the sudoers allowlist (validating it with `visudo -c` first),
installs and starts the systemd unit, and prints the credentials.
**Save them — they are printed once.**

Re-run it after a `git pull` to pick up changes; it is idempotent and
leaves an existing `config.json`'s values alone (keys added since are
merged in with their defaults).

### `config.json` — the two keys that matter here

- **`bind`** — a list of addresses, one listener each: the LAN address
  the phone uses and the WireGuard one (`10.77.0.10`). A plain string
  still works. `0.0.0.0` / `::` are refused, in a list too. An address
  the Pi does not have yet — `wg0` comes up after the panel — is retried
  every 15 s and added when it can be bound; the LAN listener does not
  wait for it.
- **`dashboard_url`** — where `live.py` is, `http://127.0.0.1:8080` by
  default. Everything the telemetry tabs fetch is proxied there.
- **`trusted_proxies`** — empty by default, and stays empty until #41
  puts nginx in front of the panel. See *What is proxied* below.

`install.sh` only writes the `wg0` address if the interface exists when
it runs: on a Pi where WireGuard is configured later, run it again, or
add `10.77.0.10` to `bind` by hand. An address that is *listed* but not
yet assigned is retried; one that is not listed is not.

## What is proxied, and what is not

| path | who answers | login |
|---|---|---|
| `/`, `/dashboard/*` | the panel (the page; the telemetry files from `dashboard/`) | panel |
| `/api/snapshot`, `/api/stream`, `/api/meta`, `/api/runs`, `/api/history`, `/api/sync`, `/api/diagnostics`, `/api/modes`, `/api/share` and `POST /api/mode`, `/api/share`, `/api/share/revoke` | `live.py`, through the panel | panel |
| `/s/*` (share links, whole prefix) | `live.py`, through the panel | **`live.py`'s token only** — the panel asks for nothing |
| `/api/status`, `/api/action/*` | the panel | panel |
| `/healthz` | the panel | none |

The list of proxied owner paths is closed: a path not on it is the
panel's own or a `404`. `/api/stream` is a server-sent event stream that
is open for a whole drive; the proxy relays it chunk by chunk with no
read deadline, and closes the upstream side when the phone goes. Every
other proxied request has a 15 s read deadline, so a runtime that
accepts and never answers (the process is there, its loop is stuck)
costs a thread for seconds and yields the same `503`, not a thread for
good. The panel's `Authorization` header is stripped before
forwarding; `Host`, `X-Forwarded-For` and `X-Forwarded-Proto` are
**set by the panel, replacing anything the client sent** — `live.py`
takes the first value and builds share links from it, so a client must
not get to choose the scheme or the public name. When nginx is in front
(#41) it sets them itself; list its address, as the panel sees it, in
`trusted_proxies` and the panel passes *that hop's* values through
unchanged. Nothing else ever goes in that list.

Nothing of the panel is dispatched under `/s/`: a share viewer asking
for `/s/api/status`, `/s/api/action/reboot` or the Claude tab gets
whatever `live.py` says about that path (its denied page, or a `404`),
never the panel. A test enumerates every panel route and asserts it.

## What it shows

| | why it is there |
|---|---|
| **Recording** | samples written in the last 60 s, channels, car link, Hz, drive mode, run. **"Service active" is not "data landing"** — a green dot is equally green with the ENET cable out |
| **CPU temp** | a Pi in a hot parked car throttles, then dies |
| **Clock** | the Pi has **no RTC**. A run recorded against an undisciplined clock has wrong timestamps and every trend built on it is wrong too |
| **Disk free** | session DBs fill the card; that is how recording stops silently |
| **Wi-Fi** | tells you whether the sync agent can ship at all |
| **Sync** | pending backlog. Green means *caught up*, not merely alive |
| **Throttle flags** | latched since boot, so last week's under-voltage still shows |
| **Services** | `f10-dashboard` and `f10-sync`, with start / stop / restart |
| **Deployed code** | revision, subject, and whether the checkout is dirty |
| **Drive files** | every session database with size and whether the lake has it, and a Delete for the ones already shipped |
| **Logs** | last 200 journal lines per unit; tick *previous boot* to read the log from before an unexplained reboot |

## The Car link tab

The verification view: what this session decided to ask the car, what
answered, and what resolution threw away. Fetched only while the tab is
open — it is a much larger payload than the status poll and nothing in it
changes second to second.

**Loaded from disk** — shown **with or without a car**: which mapping
files loaded, their versions, which came via `--extra-mappings`, how many
channels each declares, and the rates the classes declare. All of that is
settled at boot, so this answers *"did my extra mappings actually load?"*
in the driveway rather than on the motorway. A disconnect clears the
session picture but keeps this, because the mapping set is a property of
how the process was started, not of the link.

**This session** — needs the link. The ECU that answered and at what
address, which diagnostic profiles it proved *compatible* by probe, how
many PIDs it advertises, and the full `id@version` fingerprint of every
versioned file that shaped the run, mode table included. That string is
what two drives are compared on.

**What the ECU proved** — compatibility and identity, kept apart. Each
profile the loaded mappings require, with every nominated probe that was
sent and how it went (`answered`, `negative_response (NRC 0x31 …)`,
`transport_timeout`, `wrong_prefix`, `short_response`), and the SGBD
tables the rows were derived from — provenance, not identity. Then the
**exact SGBD** line, which is `unknown` on this car: its DDE refuses the
ident DIDs, and a read the ECU accepts proves it speaks the family, not
which revision it is. A mapping activates on compatibility; nothing here
claims more than the evidence shows.

**Mappings loaded** — each file with its version, request count, source
type and verification status, and an `--extra` badge for the ones loaded
only because `--extra-mappings` named them. That flag is the repo's "no
proprietary data in the production set" line, made visible per run.

**Requests**, failing first — one summary row per request: where it
goes (`0x12 pid 0x0C`, `0x18 did 0xDA2E`), its interval as *declared →
measured*, and asked / ok / failed with a success rate, the state and
the last error. **`asked` is how many times the request was put on the
wire** — not how often it was scheduled (a resting or retired request is
scheduled and skipped) and not a frame count (six OBD PIDs go in one
frame). A request that was asked but never `ok` is a channel the car is
not answering, which in the sample table is indistinguishable from one
nobody asked for: both are simply absent rows. A **retired** one (an OBD
PID that struck out three times) is no longer asked at all, and says
so rather than freezing its counters.

Tap a row for the whole pipeline behind it, one stage per line:
`scheduled → submitted → wire` (exchanges, tx / rx frames, with the
F303 setup frames counted apart), the outcomes (positive, NRC, timeout,
NACK, no answer in the batch, late, decode failed), `decoded → accepted
→ stored` signals with a flag when an answer had *every* signal
rejected by quality, latency (session average, p95 over the last 32,
last tx / rx ages) and the **measured** refresh interval beside the
declared one. Everything on the row is in the JSON
(`/api/diagnostics`, `requests[].stages`); the row only picks.

Staggered classes are where declared and measured differ by design.
The DDE reads declare 0.5 s, but that is the gap between firings of the
class and one member goes out per firing — so the measured refresh is
~11 s, not 0.5, and that is the number a dataset actually has.
`sampling` mode shows as a median of the polling cadence with the
ten-minute pause as `max` of the window (the last 16 refreshes) — and
as `last` only until the next decode: the pause is not hidden in the
median, and it ages out of the window rather than being averaged away.

The session line above carries the physical wire count (once per
frame; the per-request rows attribute a shared OBD batch to every
member, so they add up to more) and what the recorder committed.

**Not being read** — the answer to *"why is this channel missing?"*
Resolution filters silently by design: a mapping for another ECU variant
is skipped, not an error. This is that decision written down, grouped by
reason — the ECU does not advertise the PID, the file is for a different
variant, a derived channel lost an input. Identifiers render in hex, so
they are greppable against a mapping file.

**Channels** — every channel with its unit, the request it came from (or
*derived*), the mapping version that decoded it, its measured refresh,
whether it is stored and how many rows actually reached SQLite this
process. A channel marked not-stored is `log: false` — read and displayed
on purpose, never written; `rows` shows a dash when nothing is recording.

## The Claude tab

Only appears if the optional agent session is installed
([docs/claude-code.md](../f10pi/docs/claude-code.md)); the panel returns
`null` where the unit does not exist and the tab hides itself. Set
`claude_enabled: false` to hide it anyway.

It shows three states, not two. **"Active" alone is a lie** here: the
systemd unit wraps the agent in a `while` loop, so systemd reports
active while the agent inside restarts every five seconds — usually
because authentication expired. That is the failure the setup doc calls
invisible, since `tmux list-panes` reports `bash` and the journal stays
empty. The panel separates *unit active* from *agent process alive* and
calls the combination **crash-looping**, then shows the last lines of
the tmux pane, which is the only place the reason is ever printed.

Also shown: the Remote Control name to look for in the Claude app, and
the tmux session name for attaching over SSH. Buttons restart or stop
the session — a systemd **user** service, so no sudo and no allowlist
entry.

**There is deliberately no terminal and no prompt box.** That would be
an interactive shell behind a web form, on the box holding the WireGuard
private key and the diagnostic link to the car — the exact thing
`docs/claude-code.md` warns against. A test asserts no action can reach
`tmux send-keys`. Attach over SSH, or drive it from the app.

## What it can do

`Pull latest` fetches and **fast-forwards only**, after verifying that
`origin` still matches the pinned URL. It deliberately does **not**
restart afterwards — pulling and restarting are two decisions, and you
may want the code staged while the current drive keeps recording.

`Delete` only appears on a drive file that is **confirmed in the lake**
and is not the one being written. A database that has not shipped exists
in exactly one place, and losing it loses that drive.

`Pause sync` / `Resume sync` control the agent. There is deliberately no
"flush now": the agent already polls every few seconds once caught up,
so forcing one would save seconds and add an endpoint for nothing.

`Reboot` and `Shut down` arm on the first tap and fire on the second, so
a phone in a pocket cannot do either by accident.

**Shut down before cutting the powerbank.** Pulling power from a running
system risks corrupting the SD card. This button is the main reason the
panel is worth having.

## Security

This is the most privileged surface in the repository. `pull` makes the
Pi fetch code that the runtime then executes, so anyone who can reach
the panel and authenticate can run code on it. That is the intended
feature; everything below is what keeps it bounded.

- **Binds to named addresses, never `0.0.0.0`** — refused at startup,
  not warned about, and one wildcard in the list is the same refusal.
  The Pi joins hotspots and car-park APs; a wildcard bind would offer
  reboot-and-run-code to that whole segment.
- **The runtime is behind it, not beside it.** `live.py` listens on the
  loopback and is reached only through this panel's login — except the
  share prefix, which is `live.py`'s own token-gated surface and is
  passed through untouched, credentials and all: the panel neither adds
  its login there nor forwards it upstream anywhere.
- **Proxied POSTs must be `application/json`.** The browser attaches the
  panel's cached credentials to any request here; a cross-origin form
  cannot send that content type, and a script that does is preflighted.
  It is the same line the custom header draws for the panel's own
  actions, for the runtime's controls (mode, share).
- **HTTP Basic auth**, compared with `hmac.compare_digest`, from a
  gitignored `config.json`. A panel with no password configured refuses
  everyone rather than letting everyone in.
- **Over plain HTTP the password is base64 on every request** — readable
  by anyone sniffing that Wi-Fi. That is an accepted trade for a LAN you
  mostly control. Do not reuse this password anywhere.
- **A custom header is required on every mutating request.** Browsers
  attach cached Basic credentials automatically, so without it a page
  the phone has open could POST here cross-origin.
- **No shell, ever.** Every command is a fixed argv list; nothing from a
  request is interpolated into one, and the set of runnable commands is
  closed.
- **Not root.** The commands needing privilege go through
  `/etc/sudoers.d/f10-admin`, which names each in full — no wildcards.
  A wildcard on `systemctl` would let any unit be started, and a unit can
  run anything.
- **The unit list is an allowlist.** A request can never name an
  arbitrary systemd unit, and the boot offset for logs is a bounded
  integer rather than a string on a `journalctl` command line.
- **Deletion is fenced four ways** — a bare filename only (resolved and
  checked to be inside the sessions directory, so no symlink or `..`
  escapes it), `.db` only, never the file being written, and never one
  that is not confirmed shipped.
- **The git remote is pinned.** `pull` refuses if `origin` has been
  repointed, so the update channel cannot be swapped.

Deliberately absent from the sudoers grant: `daemon-reload`, `enable`,
`disable`, anything touching apt, and any shell. Changing what runs at
boot is a provisioning decision, not something a phone does mid-drive.
The proxy added no grant and no action: the sudoers file and the action
table are what they were.

### Exposure

Today the panel is reachable on the Pi's LAN address and, over
WireGuard, on `10.77.0.10` — both networks you are on. Once #41
publishes the panel through the VPS (TLS in front, this Basic auth
behind it), **the management surface — restart, pull, reboot, shut
down, the Claude session — becomes reachable from the internet behind
that TLS + password.** That is the point of the front door, and it is
why the password must be strong and used nowhere else, why the share
prefix is the only thing here that answers without it, and why the
runtime's own port must stay on the loopback.

## If the phone cannot connect

Check the `bind` list in `config.json` holds the address the Pi actually
has on the network the phone is on — the Pi's address changes between
your home network and a hotspot. `ip -4 -o addr show scope global` on
the Pi shows the current ones. `curl http://<ip>:8088/healthz` needs no
credentials and answers `ok` if the panel itself is up; the journal
(`journalctl -u f10-admin`) says which addresses it bound and which it
is still retrying.

If the page loads but the telemetry tabs say *runtime not running*, the
panel is fine and `live.py` is not: the System tab has the journal and
the restart button.
