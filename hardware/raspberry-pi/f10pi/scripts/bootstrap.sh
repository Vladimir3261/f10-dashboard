#!/usr/bin/env bash
# f10pi bootstrap — run the configure-* steps in order. Idempotent:
# safe to re-run. Each step is gated by a toggle in config/local.env.
#
#   sudo ./scripts/bootstrap.sh
#
# Steps: hostname -> Wi-Fi regulatory -> Wi-Fi -> WireGuard -> SSH ->
# BMW eth0 -> app services.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root
load_config local.env || die "config/local.env is required"

run_step() {
  local toggle="$1" script="$2"
  if [[ "${!toggle:-0}" == "1" ]]; then
    log "=== ${script} ==="
    "${SCRIPT_DIR}/${script}"
  else
    log "--- skip ${script} (${toggle}=0) ---"
  fi
}

run_step DO_HOSTNAME        configure-hostname.sh
run_step DO_WIFI_REGULATORY configure-wifi-regulatory.sh
run_step DO_WIFI            configure-wifi.sh
run_step DO_WIREGUARD       configure-wireguard.sh
run_step DO_SSH             configure-ssh.sh
run_step DO_ETH0_BMW        configure-eth0-bmw.sh
run_step DO_APP_SERVICES    configure-app-services.sh

log "bootstrap complete — reboot if Wi-Fi regulatory provisioning changed firmware/cmdline, then run ./scripts/verify.sh"
