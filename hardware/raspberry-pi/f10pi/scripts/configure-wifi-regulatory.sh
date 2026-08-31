#!/usr/bin/env bash
# Make Raspberry Pi Wi-Fi regulatory state available early in boot.
#
# On Pi 4 / BCM43455, firmware 7.45.265 was observed to reject the configured
# country and miss 2.4 GHz channel 13 during the initial boot scan. That made
# NetworkManager choose a lower-priority fallback network even though the
# preferred profile was correct. This step:
#   1) records the installation country in Raspberry Pi OS,
#   2) injects cfg80211.ieee80211_regdom=<country> into the kernel cmdline so
#      channels 12/13 are available before NetworkManager's first scan,
#   3) optionally replaces only the known-bad BCM43455 7.45.265 firmware with
#      Infineon 7.45.286 plus its matching CLM blob, both SHA-256 pinned.
#
# Idempotent and intentionally conservative: unknown firmware versions are
# never replaced automatically.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root
load_config local.env || die "config/local.env is required"

: "${WIFI_COUNTRY:=}"
: "${PATCH_BRCM43455_FIRMWARE:=1}"

[[ "${WIFI_COUNTRY}" =~ ^[A-Z]{2}$ ]] \
  || die "WIFI_COUNTRY must be a two-letter uppercase ISO country code (for example UA)"

BOOT_CMDLINE=/boot/firmware/cmdline.txt
BRCM_LINK=/usr/lib/firmware/cypress/cyfmac43455-sdio.bin
BRCM_CLM=/usr/lib/firmware/cypress/cyfmac43455-sdio.clm_blob

# Infineon 2024_1115 release. Pin the source commit and expected bytes so a
# future upstream change cannot silently alter what provisioning installs.
IFX_COMMIT=fde0d5a819bf37aeee6c911099ec85bdbf2bb28d
IFX_BASE="https://raw.githubusercontent.com/Infineon/ifx-linux-firmware/${IFX_COMMIT}/firmware"
BRCM_FW_SHA256=eaff8d2b6d2501bb5c477ba343900c7487af915898eac13bc91b33b1285dadce
BRCM_CLM_SHA256=8fbe9fc2952e2fbab062a142c1ea3e261cd74604761e12f304781b911df4a328

firmware_version() {
  local path="$1"
  strings "${path}" 2>/dev/null | grep -aoE '7\.45\.[0-9]+' | head -n1 || true
}

download() {
  local url="$1" out="$2"
  if have curl; then
    curl -fsSL "${url}" -o "${out}"
  elif have wget; then
    wget -qO "${out}" "${url}"
  else
    die "need curl or wget to fetch the pinned BCM43455 firmware"
  fi
}

patch_brcm43455_if_needed() {
  [[ "${PATCH_BRCM43455_FIRMWARE}" == "1" ]] || {
    log "BCM43455 firmware patch disabled (PATCH_BRCM43455_FIRMWARE=${PATCH_BRCM43455_FIRMWARE})"
    return
  }

  [[ -e "${BRCM_LINK}" && -f "${BRCM_CLM}" ]] || {
    log "BCM43455 firmware pair not present; nothing to patch"
    return
  }

  local target version
  target="$(readlink -f "${BRCM_LINK}")"
  version="$(firmware_version "${target}")"

  if [[ "${version}" == "7.45.286" ]]; then
    log "BCM43455 firmware already 7.45.286"
    return
  fi

  if [[ -n "${version}" ]] && printf '%s\n%s\n' '7.45.286' "${version}" | sort -V -C 2>/dev/null; then
    log "BCM43455 firmware ${version} is newer than or equal to 7.45.286; leaving it alone"
    return
  fi

  if [[ "${version}" != "7.45.265" ]]; then
    warn "BCM43455 firmware is '${version:-unknown}', not the known-bad 7.45.265; refusing automatic replacement"
    return
  fi

  log "known-bad BCM43455 firmware 7.45.265 detected; installing pinned 7.45.286 + matching CLM"

  local tmp backup
  tmp="$(mktemp -d)"

  download "${IFX_BASE}/cyfmac43455-sdio.bin" "${tmp}/cyfmac43455-sdio.bin"
  download "${IFX_BASE}/cyfmac43455-sdio.clm_blob" "${tmp}/cyfmac43455-sdio.clm_blob"

  printf '%s  %s\n' "${BRCM_FW_SHA256}" "${tmp}/cyfmac43455-sdio.bin" | sha256sum -c -
  printf '%s  %s\n' "${BRCM_CLM_SHA256}" "${tmp}/cyfmac43455-sdio.clm_blob" | sha256sum -c -

  backup="/var/backups/f10pi-brcm43455/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "${backup}"
  cp -a "${target}" "${backup}/"
  cp -a "${BRCM_CLM}" "${backup}/"
  log "backed up existing firmware to ${backup}"

  install -m 0644 "${tmp}/cyfmac43455-sdio.bin" "${target}"
  install -m 0644 "${tmp}/cyfmac43455-sdio.clm_blob" "${BRCM_CLM}"

  version="$(firmware_version "${target}")"
  [[ "${version}" == "7.45.286" ]] || die "firmware replacement verification failed (got ${version:-unknown})"
  rm -rf "${tmp}"
  log "BCM43455 firmware is now ${version}; reboot required to load it"
}

set_pi_wifi_country() {
  if have raspi-config; then
    local current
    current="$(raspi-config nonint get_wifi_country 2>/dev/null || true)"
    if [[ "${current}" != "${WIFI_COUNTRY}" ]]; then
      log "setting Raspberry Pi Wi-Fi country: ${current:-unset} -> ${WIFI_COUNTRY}"
      raspi-config nonint do_wifi_country "${WIFI_COUNTRY}" \
        || warn "raspi-config could not set Wi-Fi country; boot regdomain will still be configured"
    else
      log "Raspberry Pi Wi-Fi country already ${WIFI_COUNTRY}"
    fi
  else
    warn "raspi-config not found; relying on the boot regdomain setting"
  fi
}

set_boot_regdomain() {
  [[ -f "${BOOT_CMDLINE}" ]] || die "${BOOT_CMDLINE} not found"

  local old stripped desired backup
  old="$(tr -d '\n' < "${BOOT_CMDLINE}")"
  stripped="$(printf '%s' "${old}" | sed -E 's/[[:space:]]+cfg80211\.ieee80211_regdom=[A-Za-z0-9]+//g')"
  desired="${stripped} cfg80211.ieee80211_regdom=${WIFI_COUNTRY}"

  if [[ "${old}" == "${desired}" ]]; then
    log "boot regdomain already ${WIFI_COUNTRY}"
    return
  fi

  backup="${BOOT_CMDLINE}.f10pi.bak.$(date +%Y%m%d-%H%M%S)"
  cp -a "${BOOT_CMDLINE}" "${backup}"
  printf '%s\n' "${desired}" > "${BOOT_CMDLINE}"
  log "boot regdomain set to ${WIFI_COUNTRY} (backup: ${backup})"
  log "reboot required before the new boot-time regulatory setting is active"
}

# Patch first while the current uplink is still known-good; the country
# change may reconfigure the radio on some Raspberry Pi OS releases.
patch_brcm43455_if_needed
set_pi_wifi_country
set_boot_regdomain
sync

log "Wi-Fi regulatory provisioning complete"
