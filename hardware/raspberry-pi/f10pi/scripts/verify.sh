#!/usr/bin/env bash
# f10pi health check. Reports, without ever printing a secret:
#   hostname, wlan0 state, active Wi-Fi profile, Internet access,
#   eth0 state, wg0 state, WireGuard handshake, VPN-server reachability,
#   SSH service state, application service state.
#
# By default output is already safe (no PSKs/keys). Pass --public to also
# redact public IPs, MAC addresses, SSIDs, and usernames so the log can be
# pasted into an issue or shared publicly.
#
#   ./scripts/verify.sh            # local use
#   ./scripts/verify.sh --public   # sanitized for sharing
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
set +e  # a failed check must not abort the whole report

PUBLIC=0; [[ "${1:-}" == "--public" ]] && PUBLIC=1
load_config local.env 2>/dev/null || true
: "${WLAN_IF:=wlan0}"; : "${ETH_IF:=eth0}"; : "${WG_IF:=wg0}"

# --- sanitizer -------------------------------------------------------------
# Redacts things that identify infrastructure when --public is set. Always
# redacts obvious secrets regardless.
san() {
  if [[ ${PUBLIC} -eq 1 ]]; then
    sed -E \
      -e 's/([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}/<mac>/g' \
      -e 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/<ip>/g' \
      -e 's/([0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F]{0,4}/<ipv6>/g' \
      -e "s/\b${PI_USER:-__nouser__}\b/<user>/g"
  else
    # even in local mode, never surface MACs or v6 privacy addresses
    sed -E -e 's/([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}/<mac>/g'
  fi
}

ok()   { printf '  \033[1;32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; }
info() { printf '  ..   %s\n' "$*"; }
hdr()  { printf '\n\033[1;34m%s\033[0m\n' "$*"; }

# --- identity --------------------------------------------------------------
hdr "host"
info "hostname: $(hostname | san)"
info "uptime:   $(uptime -p 2>/dev/null || true)"

# --- wlan0 (Internet) ------------------------------------------------------
hdr "wlan0 (Internet)"
if ip link show "${WLAN_IF}" >/dev/null 2>&1; then
  state=$(cat "/sys/class/net/${WLAN_IF}/operstate" 2>/dev/null)
  [[ "${state}" == "up" ]] && ok "${WLAN_IF} ${state}" || bad "${WLAN_IF} ${state}"
  # active Wi-Fi profile name only — NEVER the PSK
  if have nmcli; then
    active=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v d="${WLAN_IF}" '$2==d{print $1}')
    [[ ${PUBLIC} -eq 1 && -n "${active}" ]] && active="<wifi-profile>"
    info "active profile: ${active:-none}"
  fi
  ip -4 addr show "${WLAN_IF}" | awk '/inet /{print "  ..   addr: "$2}' | san
else
  bad "${WLAN_IF} not present"
fi
if ping -c1 -W2 -I "${WLAN_IF}" 1.1.1.1 >/dev/null 2>&1; then
  ok "Internet reachable via ${WLAN_IF}"
else
  bad "no Internet via ${WLAN_IF}"
fi

# --- eth0 (BMW ENET) -------------------------------------------------------
hdr "eth0 (BMW ENET link)"
if ip link show "${ETH_IF}" >/dev/null 2>&1; then
  carrier=$(cat "/sys/class/net/${ETH_IF}/carrier" 2>/dev/null)
  [[ "${carrier}" == "1" ]] && ok "${ETH_IF} cable connected" || info "${ETH_IF} no cable (carrier=${carrier:-?})"
  ip -4 addr show "${ETH_IF}" | awk '/inet /{print "  ..   addr: "$2}'
  if ip route show default | grep -q "dev ${ETH_IF}"; then
    bad "${ETH_IF} HAS a default route — it must never be the default route!"
  else
    ok "${ETH_IF} has no default route (correct)"
  fi
else
  bad "${ETH_IF} not present"
fi

# --- wg0 (management VPN) --------------------------------------------------
hdr "wg0 (management VPN)"
if systemctl is-active --quiet "wg-quick@${WG_IF}"; then
  ok "wg-quick@${WG_IF} active"
else
  bad "wg-quick@${WG_IF} not active"
fi
if have wg && ip link show "${WG_IF}" >/dev/null 2>&1; then
  # `wg show` prints keys — strip them; report only handshake + transfer.
  hs=$(wg show "${WG_IF}" latest-handshakes 2>/dev/null | awk '{print $2}' | head -n1)
  if [[ -n "${hs}" && "${hs}" != "0" ]]; then
    now=$(date +%s); ago=$(( now - hs ))
    (( ago < 180 )) && ok "recent handshake (${ago}s ago)" || info "last handshake ${ago}s ago"
  else
    bad "no WireGuard handshake yet"
  fi
  wg show "${WG_IF}" transfer 2>/dev/null | awk '{print "  ..   transfer: rx="$2" tx="$3}'
else
  info "${WG_IF} interface not up"
fi
# VPN-server reachability over the tunnel (uses WG_SERVER_IP if provided)
if [[ -n "${WG_SERVER_IP:-}" ]]; then
  if ping -c1 -W2 "${WG_SERVER_IP}" >/dev/null 2>&1; then
    ok "VPN server reachable over ${WG_IF}"
  else
    bad "VPN server not reachable over ${WG_IF}"
  fi
else
  info "WG_SERVER_IP not set — skipping server ping (set it in local.env to test)"
fi

# --- ssh -------------------------------------------------------------------
hdr "ssh"
if systemctl is-active --quiet ssh || systemctl is-active --quiet sshd; then
  ok "sshd running"
  pw=$(sshd -T 2>/dev/null | awk '/^passwordauthentication/{print $2}')
  info "password auth: ${pw:-unknown}"
else
  bad "sshd not running"
fi

# --- application services --------------------------------------------------
hdr "application"
for svc in f10-dashboard.service f10-sync.service; do
  if systemctl list-unit-files | grep -q "^${svc}"; then
    if systemctl is-active --quiet "${svc}"; then
      ok "${svc} active"
    else
      bad "${svc} installed but not active"
    fi
  else
    info "${svc} not installed"
  fi
done

# dashboard port
if have ss && ss -ltn 2>/dev/null | grep -q ':8080'; then
  ok "dashboard listening on :8080"
else
  info "dashboard not listening on :8080"
fi

hdr "done"
[[ ${PUBLIC} -eq 1 ]] && echo "  (output sanitized for public sharing)"
