# Raspberry Pi 4 Wi-Fi channel 13 cold-boot failure

This records a real failure seen on the `f10pi` Raspberry Pi 4 deployment and
how it was isolated. It is intentionally sanitized: no real SSIDs, BSSIDs,
MACs, IPs, or credentials are required to reproduce or understand it.

## Symptom

NetworkManager had three valid Wi-Fi profiles with autoconnect enabled:

```text
preferred network   priority 200
secondary network   priority 100
fallback hotspot    priority 10
```

The preferred network had worked normally and had the highest priority, but
after a reboot the Pi repeatedly connected to the low-priority fallback
hotspot instead.

The profile itself looked correct:

```text
connection.autoconnect:          yes
connection.autoconnect-priority: 200
connection.interface-name:       wlan0
802-11-wireless.bssid:            --
802-11-wireless.hidden:           no
```

Manually running `nmcli connection up <preferred-profile>` succeeded once the
network was visible, so this was not a PSK, profile, or priority problem.

## Investigation

### 1. The preferred AP was missing from the initial scan

Immediately after the bad boot, `nmcli device wifi list` showed the fallback
hotspot but not the preferred AP. The radio itself was working and saw many
other 2.4 and 5 GHz networks.

A laptop connected to the preferred AP showed that it was operating on:

```text
2.4 GHz
channel 13
2472 MHz
```

That moved the investigation below NetworkManager: a profile cannot win a
priority comparison if the driver does not report the AP at all.

### 2. Regulatory state was suspicious

`iw reg get` reported a configured global country, but the Broadcom PHY
remained:

```text
phy#0
country 99: DFS-UNSET
```

The kernel still listed channel 13 as not disabled, which made the failure
less obvious. A raw scan was the decisive test:

```bash
sudo iw dev wlan0 scan freq 2472 | grep -E 'SSID:|signal:|freq:'
```

Before the regulatory state was refreshed, the preferred channel-13 AP was
missing. After a runtime regulatory refresh it appeared immediately.

### 3. The installed BCM43455 firmware rejected country changes

The boot log contained repeated lines like:

```text
brcmfmac: Firmware: BCM4345/6 ... version 7.45.265
brcmf_cfg80211_reg_notifier: Firmware rejected country setting
```

The package itself was current for the configured Raspberry Pi OS repository,
but the `cyfmac43455-sdio-standard.bin` selected through Debian alternatives
still contained Broadcom/Cypress firmware `7.45.265` from 2023.

The matching firmware files were:

```text
/usr/lib/firmware/cypress/cyfmac43455-sdio.bin
  -> /etc/alternatives/cyfmac43455-sdio.bin
  -> /usr/lib/firmware/cypress/cyfmac43455-sdio-standard.bin

/usr/lib/firmware/cypress/cyfmac43455-sdio.clm_blob
```

The newer Infineon release contained BCM43455 firmware `7.45.286` dated
2024-10-28. The firmware and its CLM regulatory blob are a pair; replacing one
without the other was deliberately avoided.

Pinned source used by provisioning:

```text
Infineon/ifx-linux-firmware
commit fde0d5a819bf37aeee6c911099ec85bdbf2bb28d
release: 2024_1115
```

Expected SHA-256 values:

```text
eaff8d2b6d2501bb5c477ba343900c7487af915898eac13bc91b33b1285dadce  cyfmac43455-sdio.bin
8fbe9fc2952e2fbab062a142c1ea3e261cd74604761e12f304781b911df4a328  cyfmac43455-sdio.clm_blob
```

After loading `7.45.286`, the `Firmware rejected country setting` messages
disappeared.

### 4. Firmware alone was not enough

With the new firmware but no explicit boot regdomain, the Pi could still boot
onto the fallback network. The important timing detail was visible in the
NetworkManager journal:

```text
wlan0 becomes available
~60 s with no preferred-network activation
NetworkManager auto-activates fallback hotspot
fallback association completes
wpa_supplicant receives a Country IE
channel 13 becomes visible afterwards
```

By then it was too late. NetworkManager does not abandon a healthy connection
just because a higher-priority profile becomes visible later.

The final missing piece was to make the correct regulatory domain available
*before the first boot scan*:

```text
cfg80211.ieee80211_regdom=<country>
```

in `/boot/firmware/cmdline.txt`.

With `7.45.286` plus the boot regdomain in place, the next cold boot showed:

```text
policy: auto-activating connection '<preferred-profile>'
Activation: starting connection '<preferred-profile>'
Connected to wireless network '<preferred-ssid>'
```

The preferred channel-13 network was selected about one second after wlan0
became ready. The fallback hotspot was never activated.

## Reusable fix

The provisioning path now runs:

```text
configure-wifi-regulatory.sh
    -> patch only known-bad BCM43455 7.45.265
    -> verify pinned SHA-256 for firmware + CLM
    -> back up replaced files
    -> set Raspberry Pi OS Wi-Fi country
    -> ensure cfg80211.ieee80211_regdom=<country> in kernel cmdline

configure-wifi.sh
    -> create/update NetworkManager profiles and priorities
```

Configuration lives in gitignored `config/local.env`:

```bash
WIFI_COUNTRY=UA                  # change to the installation country
PATCH_BRCM43455_FIRMWARE=1
DO_WIFI_REGULATORY=1
```

Run either the full bootstrap or only the regulatory step:

```bash
sudo ./scripts/bootstrap.sh
# or
sudo ./scripts/configure-wifi-regulatory.sh
```

A reboot is required after firmware or kernel-command-line changes.

## Verification

Useful checks after a cold boot, before manually changing regulatory state or
forcing a connection:

```bash
nmcli -f NAME,TYPE,DEVICE,AUTOCONNECT-PRIORITY connection show --active

nmcli -f IN-USE,SSID,FREQ,CHAN,SIGNAL device wifi list

iw reg get

sudo iw dev wlan0 scan freq 2472 | grep -E 'SSID:|signal:|freq:'

sudo dmesg | grep -Ei 'brcmfmac.*Firmware|reg_notifier|country setting'

journalctl -b -u NetworkManager --no-pager \
  | grep -Ei 'auto-activating|Activation: starting|Connected to wireless'

journalctl -b -u wpa_supplicant --no-pager \
  | grep -Ei 'Trying to associate|Associated|CONNECTED|REGDOM'

cat /proc/cmdline
```

`./scripts/verify-wifi-regulatory.sh` packages the useful checks into one
read-only report.

## A separate AP configuration issue discovered during the test

After the successful boot, `wpa_supplicant` received a Country IE advertising
a different country from the AP, and the global regulatory domain changed to
that advertised country. This did not prevent channel 13 operation, but it is
an AP/router configuration issue and should be corrected on the AP itself.

Do not treat `phy#0 country 99` alone as proof that Wi-Fi is broken. On the
working final boot it still appeared as `99`; the useful tests were the actual
channel scan, absence of the firmware rejection, and NetworkManager choosing
the preferred profile during the initial boot scan.

## Rollback and package upgrades

The script backs up replaced firmware under:

```text
/var/backups/f10pi-brcm43455/<timestamp>/
```

It also creates a timestamped backup of `/boot/firmware/cmdline.txt` whenever
it changes the boot regdomain.

The firmware files belong to the distribution package, so a future
`firmware-brcm80211` upgrade may overwrite the manually provisioned version.
This is why the workaround lives in the idempotent provisioning script rather
than as an undocumented one-off machine edit. Re-running bootstrap will patch
`7.45.265` again if it reappears, but it will not overwrite unknown or newer
firmware versions.
