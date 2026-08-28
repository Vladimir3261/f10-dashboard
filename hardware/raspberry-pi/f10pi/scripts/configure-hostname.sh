#!/usr/bin/env bash
# Set the Pi hostname (mDNS name f10pi.local via avahi). Idempotent.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root
load_config local.env || die "config/local.env is required"

: "${PI_HOSTNAME:?PI_HOSTNAME not set}"

current="$(hostnamectl --static 2>/dev/null || cat /etc/hostname)"
if [[ "${current}" == "${PI_HOSTNAME}" ]]; then
  log "hostname already ${PI_HOSTNAME}"
else
  log "setting hostname ${current} -> ${PI_HOSTNAME}"
  hostnamectl set-hostname "${PI_HOSTNAME}"
fi

# Keep /etc/hosts 127.0.1.1 line in sync so sudo doesn't warn.
if grep -qE '^\s*127\.0\.1\.1' /etc/hosts; then
  sed -i -E "s/^(\s*127\.0\.1\.1\s+).*/\1${PI_HOSTNAME}/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "${PI_HOSTNAME}" >> /etc/hosts
fi

# mDNS so `ssh pi@f10pi.local` works on the LAN.
if have apt-get && ! dpkg -s avahi-daemon >/dev/null 2>&1; then
  log "installing avahi-daemon for mDNS"
  apt-get update -qq && apt-get install -y -qq avahi-daemon
fi
systemctl enable --now avahi-daemon 2>/dev/null || true

log "hostname configured"
