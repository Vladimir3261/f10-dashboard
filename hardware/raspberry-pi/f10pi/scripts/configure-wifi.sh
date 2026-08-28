#!/usr/bin/env bash
# Configure Wi-Fi networks via NetworkManager from config/wifi.env.
# Each network becomes an autoconnect profile with a priority; the Pi picks
# the highest-priority network in range. Real SSIDs/PSKs live only in
# config/wifi.env (gitignored). Idempotent: re-adding updates in place.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root
load_config local.env || die "config/local.env is required"
load_config wifi.env  || die "config/wifi.env is required"

have nmcli || die "nmcli not found — this script needs NetworkManager"

: "${WLAN_IF:=wlan0}"
: "${WIFI_COUNT:=0}"

# Make sure Wi-Fi radio is on.
nmcli radio wifi on || true

upsert_wifi() {
  local ssid="$1" psk="$2" prio="$3" name="wifi-${1}"
  [[ -z "${ssid}" || "${ssid}" == "<"* ]] && { warn "skip unset SSID ('${ssid}')"; return; }

  if nmcli -g NAME connection show | grep -Fxq "${name}"; then
    log "updating profile ${name}"
  else
    log "creating profile ${name} (ssid=${ssid})"
    nmcli connection add type wifi con-name "${name}" ifname "${WLAN_IF}" ssid "${ssid}"
  fi
  nmcli connection modify "${name}" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "${psk}" \
    connection.autoconnect yes \
    connection.autoconnect-priority "${prio}"
}

for i in $(seq 1 "${WIFI_COUNT}"); do
  ssid_var="WIFI_${i}_SSID"; psk_var="WIFI_${i}_PSK"; prio_var="WIFI_${i}_PRIORITY"
  upsert_wifi "${!ssid_var:-}" "${!psk_var:-}" "${!prio_var:-50}"
done

log "wifi profiles configured (${WIFI_COUNT} defined). Active networks:"
nmcli -f NAME,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show | grep -i wifi || true
