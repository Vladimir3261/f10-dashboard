#!/usr/bin/env bash
# Enable the SSH server, install the Pi->server client config, and
# (optionally) harden to key-only auth. Idempotent.
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

# --- client: Pi -> telemetry server ---------------------------------------
src="${CONFIG_DIR}/ssh_config"
if [[ -f "${src}" ]]; then
  ssh_home="/home/${PI_USER}/.ssh"
  install -d -m 0700 -o "${PI_USER}" -g "${PI_USER}" "${ssh_home}" "${ssh_home}/config.d"
  install -m 0600 -o "${PI_USER}" -g "${PI_USER}" "${src}" "${ssh_home}/config.d/telemetry-server"
  # Ensure ~/.ssh/config includes config.d/*
  cfg="${ssh_home}/config"
  if ! grep -qs 'Include config.d/\*' "${cfg}" 2>/dev/null; then
    { echo 'Include config.d/*'; [[ -f "${cfg}" ]] && cat "${cfg}"; } > "${cfg}.new"
    mv "${cfg}.new" "${cfg}"
    chown "${PI_USER}:${PI_USER}" "${cfg}"; chmod 0600 "${cfg}"
  fi
  log "installed Pi->server SSH client config (alias telemetry-server)"
else
  warn "no config/ssh_config — skipping Pi->server client config"
fi

log "ssh configured"
