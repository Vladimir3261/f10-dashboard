#!/usr/bin/env bash
# Dedicate eth0 to the BMW ENET link: a fixed link-local 169.254.x.x
# address, NO default route, NO DNS. The BMW gateway is discovered by UDP
# broadcast on 169.254.255.255:6811 (the runtime does that itself); this
# script just makes eth0 a stable, isolated link-local interface.
# Idempotent.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root
load_config local.env || die "config/local.env is required"

: "${ETH_IF:=eth0}"
: "${ETH_LINK_LOCAL:=169.254.10.10/16}"
have nmcli || die "nmcli not found — this script needs NetworkManager"

name="bmw-enet"
if nmcli -g NAME connection show | grep -Fxq "${name}"; then
  log "updating profile ${name}"
else
  log "creating profile ${name} on ${ETH_IF}"
  nmcli connection add type ethernet con-name "${name}" ifname "${ETH_IF}"
fi

# Manual link-local address, and critically: never a default route, never
# DNS. never-default yes keeps eth0 off the default route even if something
# hands it one. ignore-auto-dns avoids the BMW link polluting resolv.conf.
nmcli connection modify "${name}" \
  ipv4.method manual \
  ipv4.addresses "${ETH_LINK_LOCAL}" \
  ipv4.gateway "" \
  ipv4.never-default yes \
  ipv4.ignore-auto-dns yes \
  ipv6.method link-local \
  connection.autoconnect yes \
  connection.autoconnect-priority 50

nmcli connection up "${name}" || warn "could not bring up ${name} (cable connected?)"

# Guard rail: eth0 must not carry the default route.
if ip route show default | grep -q "dev ${ETH_IF}"; then
  die "default route is on ${ETH_IF}! The BMW link must never be the default route."
fi

log "eth0 dedicated to BMW ENET (${ETH_LINK_LOCAL}, no default route)"
