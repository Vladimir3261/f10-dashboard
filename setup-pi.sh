#!/usr/bin/env bash
#
# Interactive end-to-end provisioning for the in-car Raspberry Pi.
#
#     ./setup-pi.sh
#
# Asks for your Wi-Fi networks, the Pi's login and where to reach it on your
# LAN, then does the whole sequence: generate the Pi's setup script from the
# live infrastructure, register the Pi as a WireGuard peer on the server, copy
# the script over and run it.
#
# Everything it does is also available as individual steps - see
# infra/PROVISIONING.md - this just drives them in the right order, which is
# the part that is easy to get wrong (the server has to know the peer before
# the Pi tries to connect).
#
# Safe to re-run: every underlying step is idempotent.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WIFI_ENV="$ROOT/hardware/raspberry-pi/f10pi/config/wifi.env"
PI_SCRIPT="$ROOT/local/pi-setup.sh"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
log()   { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[setup] WARN:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m[setup] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

ask() {  # prompt default -> echoes the answer
  local prompt="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " reply </dev/tty
    printf '%s' "${reply:-$default}"
  else
    while :; do
      read -r -p "$prompt: " reply </dev/tty
      [[ -n "$reply" ]] && { printf '%s' "$reply"; return; }
      echo "  (required)" >&2
    done
  fi
}

ask_secret() {  # prompt -> echoes the answer, not shown while typing
  local prompt="$1" reply
  while :; do
    read -r -s -p "$prompt: " reply </dev/tty; echo >&2
    [[ -n "$reply" ]] && { printf '%s' "$reply"; return; }
    echo "  (required)" >&2
  done
}

confirm() {  # prompt -> 0 if yes
  local reply
  read -r -p "$1 [y/N]: " reply </dev/tty
  [[ "$reply" =~ ^[Yy] ]]
}

# ------------------------------------------------------------- preflight

command -v terraform >/dev/null || die "terraform not found on PATH"
command -v ansible-playbook >/dev/null || die "ansible not found on PATH"
[[ -f "$ROOT/infra/.env" ]] || die "infra/.env is missing - copy infra/.env.example and fill it in"

terraform -chdir="$ROOT/infra/terraform" output -raw droplet_ip >/dev/null 2>&1 \
  || die "no droplet in the Terraform state - run 'cd infra && make provision' first"
DROPLET_IP="$(terraform -chdir="$ROOT/infra/terraform" output -raw droplet_ip)"

bold ""
bold "  Raspberry Pi provisioning"
bold "  ========================="
echo "  Analytics server: $DROPLET_IP"
echo
echo "  This will configure the Pi's Wi-Fi, join it to the WireGuard VPN,"
echo "  set up the BMW ENET link and start the telemetry services."
echo

# ------------------------------------------------------------------ Wi-Fi

bold "1. Wi-Fi networks"
echo "   The Pi joins the highest-priority network in range, so list your"
echo "   home network first and the in-car hotspot after it."
echo

declare -a SSIDS PSKS PRIOS
priority=100
while :; do
  n=$(( ${#SSIDS[@]} + 1 ))
  echo
  ssid="$(ask "   Wi-Fi $n  SSID")"
  psk="$(ask_secret "   Wi-Fi $n  password")"
  this_prio=$priority
  SSIDS+=("$ssid"); PSKS+=("$psk"); PRIOS+=("$this_prio")
  priority=$(( priority - 10 ))
  # Deliberately not ${PRIOS[-1]}: negative array indices are bash 4+, and
  # macOS still ships bash 3.2.
  echo "   added '$ssid' (priority $this_prio)"
  echo
  confirm "   Add another Wi-Fi network?" || break
done

# ------------------------------------------------------------------ the Pi

echo
bold "2. The Raspberry Pi"
PI_USER="$(ask "   Login user on the Pi" "f10")"
PI_HOST="$(ask "   Where to reach it on your LAN (hostname or IP)" "f10pi.local")"

# --------------------------------------------------- look at the Pi first

echo
bold "3. Checking the Pi"
PI_REPO_DIR=""
if ssh -o BatchMode=yes -o ConnectTimeout=10 "$PI_USER@$PI_HOST" true 2>/dev/null; then
  echo "   reachable, key login works"
  # Find an existing checkout so we reuse it instead of cloning a second
  # copy beside it. Checks the default path first, then anywhere obvious.
  PI_REPO_DIR="$(ssh -o BatchMode=yes "$PI_USER@$PI_HOST" '
      for d in "$HOME/f10-dashboard" /opt/f10-dashboard /srv/f10-dashboard; do
        [ -d "$d/.git" ] && { echo "$d"; exit 0; }
      done
      find "$HOME" -maxdepth 3 -type d -name .git 2>/dev/null \
        | head -20 | while read -r g; do
            r="$(dirname "$g")"
            [ -d "$r/bmwdiag" ] && { echo "$r"; exit 0; }
          done
      exit 0' 2>/dev/null | head -1)"
  if [[ -n "$PI_REPO_DIR" ]]; then
    echo "   repo already cloned at $PI_REPO_DIR - will reuse it"
  else
    PI_REPO_DIR="/home/$PI_USER/f10-dashboard"
    echo "   no checkout found - will clone into $PI_REPO_DIR"
  fi
else
  PI_REPO_DIR="/home/$PI_USER/f10-dashboard"
  warn "cannot log in without a password yet - you will be prompted later"
  echo "   assuming the repo path $PI_REPO_DIR"
fi
export PI_REPO_DIR

# ----------------------------------------------------------------- confirm

echo
bold "4. Review"
echo "   Wi-Fi networks:"
for i in "${!SSIDS[@]}"; do
  printf '     %d. %-28s priority %s\n' "$(( i + 1 ))" "${SSIDS[$i]}" "${PRIOS[$i]}"
done
echo "   Pi login:        $PI_USER@$PI_HOST"
echo "   Repo on the Pi:  $PI_REPO_DIR"
echo "   Server:          $DROPLET_IP"
echo
echo "   Will now:"
echo "     - write the Wi-Fi list to hardware/.../config/wifi.env (gitignored)"
echo "     - generate local/pi-setup.sh from the live infrastructure"
echo "     - run 'make deploy' so the server registers the Pi as a peer"
echo "     - copy the script to the Pi and run it there (needs sudo on the Pi)"
echo "     - reuse the existing checkout if there is one; clone only if not"
echo
confirm "   Proceed?" || { echo "   aborted."; exit 0; }

# -------------------------------------------------------------- do the work

echo
log "writing $(basename "$WIFI_ENV")"
install -d -m 700 "$(dirname "$WIFI_ENV")"
: > "$WIFI_ENV"
chmod 600 "$WIFI_ENV"
for i in "${!SSIDS[@]}"; do
  n=$(( i + 1 ))
  {
    printf 'WIFI_%d_SSID=%s\n'     "$n" "${SSIDS[$i]}"
    printf 'WIFI_%d_PSK=%s\n'      "$n" "${PSKS[$i]}"
    printf 'WIFI_%d_PRIORITY=%s\n' "$n" "${PRIOS[$i]}"
  } >> "$WIFI_ENV"
done
printf 'WIFI_COUNT=%d\n' "${#SSIDS[@]}" >> "$WIFI_ENV"

log "generating the Pi setup script"
make -C "$ROOT/hardware" pi-setup

log "registering the peer on the server (make deploy)"
make -C "$ROOT/infra" deploy

[[ -f "$PI_SCRIPT" ]] || die "expected $PI_SCRIPT to exist after pi-setup"

log "copying the setup script to the Pi"
scp -q "$PI_SCRIPT" "$PI_USER@$PI_HOST:~/pi-setup.sh"

log "running it on the Pi (sudo may ask for a password)"
ssh -t "$PI_USER@$PI_HOST" 'chmod 700 ~/pi-setup.sh && sudo ~/pi-setup.sh'

# ---------------------------------------------------------------- verify

echo
log "checking the tunnel from the server"
if ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$DROPLET_IP" \
     "wg show wg0 latest-handshakes" 2>/dev/null | awk '$2 != 0 {found=1} END {exit !found}'; then
  echo "   handshake seen - the Pi is on the VPN"
else
  warn "no WireGuard handshake yet. Give it a moment, then check:"
  warn "  ssh root@$DROPLET_IP 'wg show wg0'"
  warn "  and on the Pi: sudo wg show wg0"
fi

PI_WG_IP="$(sed -n 's/^PI_WG_IP=//p' "$ROOT/infra/.env" | head -1)"
PI_WG_IP="${PI_WG_IP:-10.77.0.10}"

echo
bold "Done."
echo "  Reach the Pi from anywhere, through the server:"
echo "      ssh -J root@$DROPLET_IP $PI_USER@$PI_WG_IP"
echo
echo "  The generated script holds a WireGuard private key and your Wi-Fi"
echo "  passwords. Remove the copies when you are happy it works:"
echo "      rm $PI_SCRIPT"
echo "      ssh $PI_USER@$PI_HOST 'rm ~/pi-setup.sh'"
