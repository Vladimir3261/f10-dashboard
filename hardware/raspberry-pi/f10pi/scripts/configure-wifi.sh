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

  #
  # Remove any OTHER profile for the same SSID before writing ours.
  #
  # A Pi flashed with Wi-Fi preconfigured (or set up by hand) already has a
  # profile for that network under a different name. Left in place, two
  # profiles compete for one SSID with independent priorities - and if they
  # happen to tie, which one NetworkManager picks at boot is not
  # deterministic. Observed in the field: a leftover profile tied with the
  # in-car LTE router, so the Pi could come up on either.
  #
  local other
  while read -r other; do
    [[ -z "${other}" || "${other}" == "${name}" ]] && continue
    if [[ "$(nmcli -g 802-11-wireless.ssid connection show "${other}" 2>/dev/null)" == "${ssid}" ]]; then
      if [[ "$(nmcli -t -f NAME,DEVICE connection show --active \
               | awk -F: -v d="${WLAN_IF}" '$2==d{print $1}')" == "${other}" ]]; then
        warn "leaving ${other} in place: it is the ACTIVE connection"
      else
        log "removing duplicate profile ${other} (same SSID as ${name})"
        nmcli connection delete "${other}" >/dev/null 2>&1 || true
      fi
    fi
  done < <(nmcli -t -g NAME connection show)

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
