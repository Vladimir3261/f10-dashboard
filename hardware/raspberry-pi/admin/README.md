# f10-admin — the Pi's control panel

A phone-sized web page for the things you would otherwise SSH in to do,
from the driver's seat: see whether the runtime is alive, read why it
isn't, pull a fix, restart it, and shut the box down cleanly before
cutting the power.

```
http://<pi-lan-ip>:8088/
```

## Install

On the Pi, once:

```bash
cd ~/f10-dashboard/hardware/raspberry-pi/admin && sudo ./install.sh
```

It generates `config.json` with a random password and the Pi's detected
LAN address, installs the sudoers allowlist (validating it with
`visudo -c` first), installs and starts the systemd unit, and prints the
credentials. **Save them — they are printed once.**

Re-run it after a `git pull` to pick up changes; it is idempotent and
leaves an existing `config.json` alone.

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

**This session** — needs the link. The ECU that answered and at what address, the SGBD
variants confirmed *by probe*, how many PIDs it advertises, and the full
`id@version` fingerprint of every versioned file that shaped the run,
mode table included. That string is what two drives are compared on.

**Mappings loaded** — each file with its version, request count, source
type and verification status, and an `--extra` badge for the ones loaded
only because `--extra-mappings` named them. That flag is the repo's "no
proprietary data in the production set" line, made visible per run.

**Requests**, failing first — where each goes (`0x12 pid 0x0C`,
`0x18 did 0xDA2E`), its real interval, and sent / ok / failed with a
success rate and the last error. **`sent` with no `ok` is a channel the
car is not answering**, which in the sample table is indistinguishable
from one nobody asked for: both are simply absent rows.

Staggered classes report their *per-channel* interval. The DDE reads
declare 0.5 s, but that is the gap between firings of the class and one
member goes out per firing — so the honest number is ~11 s, not 0.5.

**Not being read** — the answer to *"why is this channel missing?"*
Resolution filters silently by design: a mapping for another ECU variant
is skipped, not an error. This is that decision written down, grouped by
reason — the ECU does not advertise the PID, the file is for a different
variant, a derived channel lost an input. Identifiers render in hex, so
they are greppable against a mapping file.

**Channels** — every channel with its unit, the request it came from (or
*derived*), the mapping version that decoded it, and whether it is
stored. A channel marked not-stored is `log: false` — read and displayed
on purpose, never written.

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

- **Binds to one address, never `0.0.0.0`** — refused at startup, not
  warned about. The Pi joins hotspots and car-park APs; a wildcard bind
  would offer reboot-and-run-code to that whole segment.
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

## If the phone cannot connect

Check `bind` in `config.json` matches the address the Pi actually has on
the network the phone is on — the Pi's address changes between your home
network and a hotspot. `ip -4 -o addr show scope global` on the Pi shows
the current one. `curl http://<ip>:8088/healthz` needs no credentials and
answers `ok` if the panel itself is up.
