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
| **CPU temp** | a Pi in a hot parked car throttles, then dies |
| **Clock** | the Pi has **no RTC**. A run recorded against an undisciplined clock has wrong timestamps and every trend built on it is wrong too |
| **Disk free** | session DBs fill the card; that is how recording stops silently |
| **Wi-Fi** | tells you whether the sync agent can ship at all |
| **Sync** | pending backlog. Green means *caught up*, not merely alive |
| **Throttle flags** | latched since boot, so last week's under-voltage still shows |
| **Services** | `f10-dashboard` and `f10-sync`, with start / stop / restart |
| **Deployed code** | revision, subject, and whether the checkout is dirty |
| **Logs** | last 200 journal lines per unit |

## What it can do

`Pull latest` fetches and **fast-forwards only**, after verifying that
`origin` still matches the pinned URL. It deliberately does **not**
restart afterwards — pulling and restarting are two decisions, and you
may want the code staged while the current drive keeps recording.

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
  arbitrary systemd unit.
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
