# Recovery

What to do when the Pi is unreachable or misbehaving. Ordered from
least- to most-invasive. The last section (SD-card-only) needs no network
at all.

## Current verified state

What is actually proven on the car vs still open, as of the first Pi
in-car drive (see `docs/PI_COMMISSIONING.md`):

| item | state |
|---|---|
| Pi runs the stdlib-only runtime; 294 tests pass on the Pi | ✅ verified |
| `eth0` link-local + gateway discovery on the car | ✅ verified (needed `ipv4.method link-local`) |
| Wi-Fi (phone hotspot) uplink | ✅ verified |
| `wg0` tunnel to server; SSH + ingest over it | ✅ verified |
| Sync agent ships a full drive, 0 pending | ✅ verified |
| Full drive logged from the Pi (drive 6) | ✅ verified |
| App autostart via systemd on cold boot | ⏳ scripted here, not yet field-proven |
| Key-only SSH hardening | ⏳ optional, off by default |
| Unattended recovery after power loss mid-drive | ⏳ relies on `Restart=` + retries; not stress-tested |
| Multi-network Wi-Fi priority roaming | ⏳ scripted, single-network proven |

Treat the ⏳ rows as the reason to keep console/SD-card recovery handy.

## Incorrect Wi-Fi config (Pi won't get online)

Symptoms: `verify.sh` shows `wlan0` down or no Internet; the Pi never
appears on the network.

1. **Reach it another way first:** LAN cable to a normal network on a
   *different* interface is not an option here (eth0 is the BMW link), so
   use a **monitor + USB keyboard** on the Pi's HDMI, or the serial
   console.
2. Re-run Wi-Fi setup with corrected `config/wifi.env`:
   ```bash
   sudo ./scripts/configure-wifi.sh
   ```
3. Or fix live:
   ```bash
   nmcli device wifi list
   nmcli connection up wifi-<SSID>
   nmcli -f NAME,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show
   ```
4. Wrong PSK: `nmcli connection modify wifi-<SSID> wifi-sec.psk '<new>'`.

**Pre-stage before a drive:** define the in-car network in `wifi.env` at
home, so the Pi already knows it. A Pi with no known network in range is
the classic "why is it offline" case.

## WireGuard failure

Symptoms: `verify.sh` shows no handshake, or the server can't reach the
Pi.

1. Is the service up?
   ```bash
   systemctl status wg-quick@wg0
   sudo systemctl restart wg-quick@wg0
   sudo wg show wg0 latest-handshakes   # want a recent, nonzero value
   ```
2. No handshake at all → the Pi can't reach the server **endpoint**.
   That usually means `wlan0` has no Internet (fix that first) or the
   endpoint IP/port in `config/wireguard.conf` is wrong.
3. Handshake OK but no traffic → check `AllowedIPs` covers the management
   subnet, and that `wg0` did **not** become the default route:
   ```bash
   ip route show default        # must be dev wlan0
   ```
4. Config placeholder left in → `configure-wireguard.sh` refuses to run if
   `<PLACEHOLDER>` values remain; fill in the real keys.

WireGuard is a management convenience — a WG outage does **not** stop
logging or the dashboard, only remote admin. You can still recover on the
LAN via `f10pi.local`.

## SSH failure / SSH lockout

**Can't SSH in:**

1. Is sshd up? Check from console: `systemctl status ssh`. Start it:
   `sudo systemctl enable --now ssh`.
2. mDNS not resolving `f10pi.local`? Use the Pi's `wg0` or `wlan0` IP
   directly, or check `avahi-daemon`.
3. "Too many authentication failures" → pin the key:
   `ssh -o IdentitiesOnly=yes -i <key> <PI_USER>@f10pi.local`.

**Locked out by hardening (key-only, key doesn't work):**

1. Get to the Pi via **monitor + keyboard** (local console still uses your
   password).
2. Re-enable password auth temporarily:
   ```bash
   sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' \
       /etc/ssh/sshd_config.d/10-f10pi.conf
   sudo systemctl reload ssh
   ```
3. Fix `~/.ssh/authorized_keys`, confirm key login in a second session,
   then re-harden. **Never** set `SSH_DISABLE_PASSWORD_AUTH=1` until you've
   logged in with your key.

## Direct cable: laptop ↔ Pi over Ethernet

Useful for first setup and as a fallback when Wi-Fi is wrong and you have no
monitor. Note this uses **`eth0`, the BMW ENET port**, so the car cable comes
out while you do it.

**On a provisioned Pi** it just works: `configure-eth0-bmw.sh` gives `eth0` a
static `169.254.10.10/16`, and a directly-connected laptop self-assigns its
own `169.254.x.x`, so both sit on the same `/16`:

```bash
ssh <PI_USER>@169.254.10.10      # or f10pi.local, via mDNS
```

A Pi 4 has auto-MDIX, so an ordinary patch cable is fine — no crossover.

**On a fresh, un-provisioned Pi this often fails**, for the same reason the
BMW link did before it was fixed: the stock `eth0` profile is DHCP, a direct
cable has no DHCP server, and NetworkManager then takes *no IPv4 address at
all* rather than self-assigning a link-local one. There is nothing to `ssh`
to. Either:

- try **`ssh <user>@raspberrypi.local`** — mDNS over IPv6 link-local usually
  still resolves, because IPv6 link-local is automatic even with no IPv4; or
- **preconfigure the SD card** with Raspberry Pi Imager (user, SSH, Wi-Fi),
  which is the reliable route and usually removes the need for the cable.

If you want the direct link to work on a stock image, set the profile the
same way the BMW link is set:

```bash
sudo nmcli connection modify <eth0-profile> ipv4.method link-local
sudo nmcli connection up <eth0-profile>
```

## SD-card-only recovery (no network, no console)

When the Pi is bricked/headless and unreachable, pull the microSD and
mount it in another computer (two partitions: `bootfs` FAT32, `rootfs`
ext4 — ext4 needs Linux or a driver on macOS/Windows).

You can, without booting the Pi:

- **Re-enable SSH:** create an empty file named `ssh` on `bootfs`.
- **Fix Wi-Fi:** on Raspberry Pi OS, drop a `custom.toml` / cloud-init
  `network-config` on `bootfs`, or edit the NetworkManager profile under
  `rootfs:/etc/NetworkManager/system-connections/*` (files are `0600`;
  the PSK is in cleartext there — handle on a trusted machine).
- **Undo a bad hardening change:** delete/edit
  `rootfs:/etc/ssh/sshd_config.d/10-f10pi.conf`.
- **Undo a bad `eth0`/route change:** edit the `bmw-enet` profile under
  `rootfs:/etc/NetworkManager/system-connections/`.
- **Last resort:** re-flash Raspberry Pi OS Lite (64-bit) and re-run
  `bootstrap.sh` — everything device-specific is in the gitignored
  `config/*` files, so keep a backup of those off the card.

**Keep an off-card backup** of the filled-in `config/*` files (they hold
your real Wi-Fi/WG/SSH values) somewhere secure and *not* in git, so a
re-flash is a 10-minute recovery.
