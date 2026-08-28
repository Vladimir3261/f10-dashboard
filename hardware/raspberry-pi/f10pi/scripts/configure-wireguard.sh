#!/usr/bin/env bash
# Install the WireGuard client config and enable wg-quick@wg0.
# Reads config/wireguard.conf (gitignored, holds the real keys/endpoint).
# Idempotent. Only the management subnet is routed over wg0 — Internet
# stays on wlan0 (enforce with AllowedIPs in the conf, not all-0.0.0.0/0).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root
load_config local.env || die "config/local.env is required"

: "${WG_IF:=wg0}"
src="${CONFIG_DIR}/wireguard.conf"
dst="/etc/wireguard/${WG_IF}.conf"

[[ -f "${src}" ]] || die "missing ${src} — copy wireguard.example.conf and fill in real keys"
if grep -q '<.*>' "${src}"; then
  die "${src} still has <PLACEHOLDER> values — fill in real keys/endpoint first"
fi

if have apt-get && ! dpkg -s wireguard >/dev/null 2>&1; then
  log "installing wireguard"
  apt-get update -qq && apt-get install -y -qq wireguard
fi

install -d -m 0700 /etc/wireguard
install -m 0600 "${src}" "${dst}"
log "installed ${dst} (0600)"

systemctl enable "wg-quick@${WG_IF}" >/dev/null 2>&1 || true
# Reload to pick up any config change without dropping unrelated routes.
if systemctl is-active --quiet "wg-quick@${WG_IF}"; then
  log "restarting wg-quick@${WG_IF}"
  systemctl restart "wg-quick@${WG_IF}"
else
  log "starting wg-quick@${WG_IF}"
  systemctl start "wg-quick@${WG_IF}"
fi

# Guard rail: wg0 must not have become the default route.
if ip route show default | grep -q "dev ${WG_IF}"; then
  warn "default route is on ${WG_IF}! Internet should stay on wlan0 — check AllowedIPs."
fi

log "wireguard configured"
