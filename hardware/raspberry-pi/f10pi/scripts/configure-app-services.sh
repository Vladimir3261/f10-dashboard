#!/usr/bin/env bash
# Install and enable the application systemd services so the telemetry
# runtime + sync agent autostart on boot. Idempotent. The unit files use
# @PI_USER@ / @REPO_DIR@ placeholders filled in from config/local.env.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root
load_config local.env || die "config/local.env is required"

: "${PI_USER:?PI_USER not set}"
: "${REPO_DIR:?REPO_DIR not set}"

[[ -d "${REPO_DIR}" ]] || warn "REPO_DIR ${REPO_DIR} does not exist yet — clone the repo there"

systemd_src="${F10PI_DIR}/systemd"
install_unit() {
  local unit="$1"
  sed -e "s|@PI_USER@|${PI_USER}|g" \
      -e "s|@REPO_DIR@|${REPO_DIR}|g" \
      "${systemd_src}/${unit}" > "/etc/systemd/system/${unit}"
  log "installed /etc/systemd/system/${unit}"
}

install_unit f10-dashboard.service
install_unit f10-sync.service

systemctl daemon-reload
systemctl enable --now f10-dashboard.service || warn "f10-dashboard failed to start (car connected?)"
systemctl enable --now f10-sync.service      || warn "f10-sync failed to start (config present?)"

log "app services installed. Status:"
systemctl --no-pager --lines=0 status f10-dashboard.service f10-sync.service || true
