#!/usr/bin/env bash
#
# Install the f10-admin panel on the Pi. Run ON the Pi, once:
#
#     sudo ./install.sh
#
# Idempotent: safe to re-run after a git pull to pick up changes.
#
# What it does:
#   * validates and installs the sudoers allowlist (visudo -c first, so a
#     bad file can never lock you out of sudo)
#   * installs and enables the systemd unit
#   * creates config.json from the example if absent, with a generated
#     password and the detected LAN + WireGuard addresses, and prints
#     the credentials
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$HERE/../../.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

# The user that owns the checkout is the user the services run as.
PI_USER="$(stat -c '%U' "$REPO_DIR")"
echo "[+] repo:  $REPO_DIR"
echo "[+] user:  $PI_USER"

# ---------------------------------------------------------------- config
CONFIG="$HERE/config.json"

if [[ ! -f "$CONFIG" ]]; then
  # The panel listens on a list of addresses, one listener each. The
  # LAN address is the first non-loopback IPv4 that is not the tunnel's
  # (the phone reaches the Pi on it); the WireGuard address is wg0's,
  # if the tunnel is configured - it is what the VPS-side reverse proxy
  # will reach the panel on (#41). An interface that is down right now
  # is not a problem: the panel retries an address it cannot bind.
  WG_IP="$(ip -4 -o addr show dev wg0 scope global 2>/dev/null \
           | awk '{print $4}' | cut -d/ -f1 | head -1)"
  LAN_IP="$(ip -4 -o addr show scope global 2>/dev/null \
            | awk '$2 != "wg0" {print $4}' | cut -d/ -f1 | head -1)"
  LAN_IP="${LAN_IP:-127.0.0.1}"
  PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  REMOTE="$(sudo -u "$PI_USER" git -C "$REPO_DIR" remote get-url origin \
            2>/dev/null || echo '')"

  python3 - "$CONFIG" "$LAN_IP" "$WG_IP" "$PASSWORD" "$REPO_DIR" "$REMOTE" <<'PY'
import json, sys
path, lan, wg, password, repo, remote = sys.argv[1:7]
json.dump({
    "bind": [lan] + ([wg] if wg and wg != lan else []),
    "port": 8088,
    "username": "f10",
    "password": password,
    "repo_dir": repo,
    "git_remote": remote,
    "git_branch": "master",
    "services": ["f10-dashboard", "f10-sync"],
    "sync_status_url": "http://127.0.0.1:8091/sync/status",
    "dashboard_url": "http://127.0.0.1:8080",
    "log_lines": 200,
}, open(path, "w"), indent=2)
PY

  chown "$PI_USER:$PI_USER" "$CONFIG"
  # Contains the password. Nobody else on the box needs to read it.
  chmod 600 "$CONFIG"
  echo "[+] wrote $CONFIG"
  NEW_CONFIG=1
else
  #
  # Merge, do not skip. Leaving an existing config completely alone was
  # right for the password and wrong for everything else: a config
  # written by an earlier version silently lacks every key added since,
  # and the runtime then falls back to defaults that may not fit this
  # box. Existing values always win; only absent keys are added.
  #
  python3 - "$CONFIG" <<'MERGE'
import json, sys

path = sys.argv[1]

with open(path) as fh:
    cfg = json.load(fh)

added = []

for key, value in (
    ("sync_control_url", "http://127.0.0.1:8091"),
    ("dashboard_status_url", "http://127.0.0.1:8080/api/snapshot"),
    ("dashboard_url", "http://127.0.0.1:8080"),
    ("trusted_proxies", []),
    ("recording_window_s", 60),
):
    if key not in cfg:
        cfg[key] = value
        added.append(key)

if added:
    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print("[=] added to config.json: " + ", ".join(added))
else:
    print("[=] config.json already has every key")
MERGE
  NEW_CONFIG=0
fi

# --------------------------------------------------------------- sudoers
#
# Validate BEFORE installing. A syntactically invalid file in
# /etc/sudoers.d breaks sudo entirely, which on a headless Pi in a car
# means a reinstall.
#
SUDOERS_TMP="$(mktemp)"
trap 'rm -f "$SUDOERS_TMP"' EXIT
sed "s|@PI_USER@|$PI_USER|g" "$HERE/f10-admin.sudoers" > "$SUDOERS_TMP"

if ! visudo -cf "$SUDOERS_TMP" >/dev/null; then
  echo "[!] generated sudoers file is invalid - not installing" >&2
  exit 1
fi

install -m 0440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/f10-admin
echo "[+] installed /etc/sudoers.d/f10-admin"

# --------------------------------------------------------------- systemd
sed -e "s|@PI_USER@|$PI_USER|g" -e "s|@REPO_DIR@|$REPO_DIR|g" \
    "$HERE/f10-admin.service" > /etc/systemd/system/f10-admin.service

systemctl daemon-reload
systemctl enable f10-admin >/dev/null
systemctl restart f10-admin
echo "[+] installed and started f10-admin.service"

sleep 1
systemctl is-active --quiet f10-admin \
  || { echo "[!] service did not start:"; journalctl -u f10-admin -n 20 --no-pager; exit 1; }

# One line per listener; `bind` is a list, or a string in an older config.
echo
python3 - "$CONFIG" <<'URLS'
import json, sys
cfg = json.load(open(sys.argv[1]))
bind = cfg["bind"]
addresses = bind if isinstance(bind, list) else [a.strip() for a in str(bind).split(",")]
for address in addresses:
    if address:
        print(f"    http://{address}:{cfg['port']}/")
URLS

if [[ "$NEW_CONFIG" == "1" ]]; then
  echo "    user: f10"
  echo "    pass: $(python3 -c "import json;print(json.load(open('$CONFIG'))['password'])")"
  echo
  echo "    Save these now - they are not printed again."
fi

echo
echo "Reachable only on the addresses listed in \`bind\`. If the phone"
echo "cannot connect, check that list in $CONFIG holds the address the"
echo "Pi actually has on the network the phone is on."
