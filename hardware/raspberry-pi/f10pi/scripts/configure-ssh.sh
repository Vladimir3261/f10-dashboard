#!/usr/bin/env bash
# Enable and harden the SSH server on the Pi. Idempotent.
#
# You reach the Pi through the server as a jump host:
#   ssh -J <admin>@<server> <PI_USER>@<PI_WG_IP>
# so the Pi needs no outbound SSH config of its own.
#
# SSH_DISABLE_PASSWORD_AUTH=1 disables password login — ONLY set that after
# you've confirmed key auth works, or you can lock yourself out.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root
load_config local.env || die "config/local.env is required"

: "${PI_USER:?PI_USER not set}"
: "${SSH_DISABLE_PASSWORD_AUTH:=0}"

# --- server: sshd on the Pi ------------------------------------------------
systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || \
  warn "could not enable ssh service — is openssh-server installed?"

harden_line() {  # key value -> ensure `key value` in a drop-in
  local key="$1" val="$2" f=/etc/ssh/sshd_config.d/10-f10pi.conf
  install -d -m 0755 /etc/ssh/sshd_config.d
  touch "${f}"
  if grep -qE "^\s*${key}\b" "${f}"; then
    sed -i -E "s|^\s*${key}\b.*|${key} ${val}|" "${f}"
  else
    printf '%s %s\n' "${key}" "${val}" >> "${f}"
  fi
}

if [[ "${SSH_DISABLE_PASSWORD_AUTH}" == "1" ]]; then
  log "hardening sshd: key-only auth"
  harden_line PasswordAuthentication no
  harden_line PubkeyAuthentication yes
  harden_line PermitRootLogin no
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
else
  log "leaving password auth enabled (set SSH_DISABLE_PASSWORD_AUTH=1 after keys work)"
fi

log "ssh configured"
