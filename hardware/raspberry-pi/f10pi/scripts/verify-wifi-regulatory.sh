#!/usr/bin/env bash
# Focused, read-only Wi-Fi regulatory/firmware check.
# Safe to run after a cold boot before manually forcing scans/connections.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
set +e
load_config local.env 2>/dev/null || true

: "${WLAN_IF:=wlan0}"
: "${WIFI_COUNTRY:=}"

ok()   { printf 'OK   %s\n' "$*"; }
warnx(){ printf 'WARN %s\n' "$*"; }
info() { printf '..   %s\n' "$*"; }

printf '== active Wi-Fi ==\n'
if have nmcli; then
  nmcli -f NAME,TYPE,DEVICE,AUTOCONNECT-PRIORITY connection show --active
else
  warnx "nmcli not found"
fi

printf '\n== configured boot regdomain ==\n'
if [[ -n "${WIFI_COUNTRY}" ]]; then
  if grep -Eq "(^| )cfg80211\.ieee80211_regdom=${WIFI_COUNTRY}( |$)" /proc/cmdline; then
    ok "kernel booted with cfg80211.ieee80211_regdom=${WIFI_COUNTRY}"
  else
    warnx "kernel did not boot with cfg80211.ieee80211_regdom=${WIFI_COUNTRY}"
  fi
else
  info "WIFI_COUNTRY not set in config/local.env"
fi

printf '\n== current regulatory state ==\n'
if have iw; then
  iw reg get
else
  warnx "iw not found"
fi

printf '\n== BCM43455 firmware ==\n'
fw_link=/usr/lib/firmware/cypress/cyfmac43455-sdio.bin
if [[ -e "${fw_link}" ]]; then
  fw_target="$(readlink -f "${fw_link}")"
  fw_version="$(strings "${fw_target}" 2>/dev/null | grep -aoE '7\.45\.[0-9]+' | head -n1)"
  info "path: ${fw_target}"
  info "version: ${fw_version:-unknown}"
  if [[ "${fw_version}" == "7.45.265" ]]; then
    warnx "known-bad 7.45.265 is installed; run configure-wifi-regulatory.sh"
  elif [[ "${fw_version}" == "7.45.286" ]]; then
    ok "validated 7.45.286 firmware installed"
  fi
else
  info "BCM43455 firmware path not present on this host"
fi

printf '\n== boot firmware/regulatory messages ==\n'
if dmesg | grep -q 'Firmware rejected country setting'; then
  warnx "brcmfmac rejected a country setting during this boot"
  dmesg | grep -E 'brcmfmac.*Firmware|Firmware rejected country setting'
else
  ok "no 'Firmware rejected country setting' message in dmesg"
  dmesg | grep -E 'brcmfmac.*Firmware' || true
fi

printf '\n== channels 12/13/14 ==\n'
if have iw; then
  iw phy phy0 channels 2>/dev/null | grep -A3 -B1 -E '2467 MHz|2472 MHz|2484 MHz' || true
fi

printf '\n== NetworkManager scan ==\n'
if have nmcli; then
  nmcli -f IN-USE,SSID,FREQ,CHAN,SIGNAL device wifi list
fi
