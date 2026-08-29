#!/usr/bin/env python3
"""
Generate a one-shot provisioning script for the Raspberry Pi.

Reads the CURRENT infrastructure state - the droplet's address from Terraform,
secrets from infra/.env, Wi-Fi networks from the Pi's wifi.env - and writes a
self-contained bash script you copy to the Pi and run once.

    cd hardware && make pi-setup

WIREGUARD KEYS ARE GENERATED ON THE SERVER, never here. The laptop has no
WireGuard tooling and never holds a long-lived key: this script SSHes to the
droplet, has it mint the Pi's keypair (once - it is reused on re-runs), and
copies the values into the generated script. The peer is also written to
infra/ansible/group_vars/all/peers.yml so `make deploy` keeps it, instead of
re-rendering wg0.conf without it.

Intended flow:

    cd hardware && make pi-setup   # here: writes local/pi-setup.sh
    cd infra && make deploy        # server learns the peer
    scp local/pi-setup.sh <pi>:    # over your LAN
    ssh <pi> 'sudo ./pi-setup.sh'  # Pi joins the new VPN
    ssh -J root@<droplet> f10@<pi-vpn-ip>     # from now on, via the VPS

The generated script contains a WireGuard private key and Wi-Fi passwords in
plain text. It is written to local/ and gitignored. Delete it when done.
"""

import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))       # hardware/scripts
ROOT = os.path.dirname(os.path.dirname(HERE))           # repo root
INFRA = os.path.join(ROOT, "infra")
TF_DIR = os.path.join(INFRA, "terraform")
PI_CFG = os.path.join(HERE, "..", "raspberry-pi", "f10pi", "config")
PI_CFG = os.path.normpath(PI_CFG)
OUT = os.path.join(ROOT, "local", "pi-setup.sh")
PEERS = os.path.join(INFRA, "ansible", "group_vars", "all", "peers.yml")

#: Where the server keeps the keypairs it mints for peers.
SERVER_PEER_DIR = "/etc/wireguard/peers"


def die(msg):
    sys.exit(f"error: {msg}")


def dotenv(path):
    """Parse KEY=value literally - no shell, so $ ! & are all safe."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
                key, _, value = line.partition("=")
                out[key.strip()] = value
    return out


def tf_output():
    try:
        raw = subprocess.run(
            ["terraform", f"-chdir={TF_DIR}", "output", "-json"],
            check=True, capture_output=True, text=True,
        ).stdout
    except FileNotFoundError:
        die("terraform not found on PATH")
    except subprocess.CalledProcessError as exc:
        die(f"terraform output failed - has the droplet been created?\n{exc.stderr}")
    doc = json.loads(raw)
    if not doc:
        die("no terraform outputs yet - run `make provision` first")
    return {k: v.get("value") for k, v in doc.items()}


def ssh(host, command):
    """Run a command on the droplet, returning stdout."""
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, command],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        die(f"ssh to {host} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def server_keys(host, name):
    """
    Mint (once) and read back the Pi's keypair ON THE SERVER.

    `wg genkey` runs there, not here - the laptop never needs wireguard-tools
    and never stores a key. Re-running reuses the existing pair, so the peer
    identity is stable and `make deploy` does not churn.
    """
    ssh(host, f"install -d -m 700 {SERVER_PEER_DIR}")
    ssh(host, (
        f"test -f {SERVER_PEER_DIR}/{name}.key || "
        f"(umask 077 && wg genkey > {SERVER_PEER_DIR}/{name}.key)"
    ))
    priv = ssh(host, f"cat {SERVER_PEER_DIR}/{name}.key")
    pub = ssh(host, f"wg pubkey < {SERVER_PEER_DIR}/{name}.key")
    srv_pub = ssh(host, "cat /etc/wireguard/publickey")
    return priv, pub, srv_pub


def wifi_networks(cfg):
    """Ordered [(ssid, psk, priority)] from the Pi's wifi.env."""
    count = int(cfg.get("WIFI_COUNT", "0") or 0)
    nets = []
    for i in range(1, count + 1):
        ssid = cfg.get(f"WIFI_{i}_SSID", "")
        psk = cfg.get(f"WIFI_{i}_PSK", "")
        prio = cfg.get(f"WIFI_{i}_PRIORITY", "50")
        if not ssid or ssid.startswith("<"):
            continue
        nets.append((ssid, psk, prio))
    return nets


def main():
    env = dotenv(os.path.join(INFRA, ".env"))
    wifi_cfg = dotenv(os.path.join(PI_CFG, "wifi.env"))
    out = tf_output()

    droplet_ip = out.get("droplet_ip") or die("terraform output has no droplet_ip")
    wg_port = out.get("wireguard_port") or 51820
    ssh_user = out.get("ssh_user") or "root"
    host = f"{ssh_user}@{droplet_ip}"

    pi_wg_ip = env.get("PI_WG_IP", "10.77.0.10")
    ingest_token = env.get("INGEST_TOKEN", "")
    dash_port = env.get("PI_DASHBOARD_PORT", "8080")
    wg_server_ip = re.sub(r"/\d+$", "", env.get("WG_SERVER_IP", "10.77.0.1"))

    if not ingest_token:
        die("INGEST_TOKEN is not set in infra/.env")

    # Wi-Fi is OPTIONAL. A Pi that already connects to its networks needs no
    # Wi-Fi configuration at all, and rewriting its NetworkManager profiles
    # would be a good way to lose contact with it. With no networks listed,
    # the generated script leaves Wi-Fi completely alone (DO_WIFI=0).
    nets = wifi_networks(wifi_cfg)

    # setup-pi.sh asks for these interactively and exports them, so the
    # environment must win over infra/.env - otherwise the answers you typed
    # are silently ignored and the generated script targets the wrong user.
    pi_user = os.environ.get("PI_USER") or env.get("PI_USER", "pi")
    # Where the repo lives on the Pi. Detected by setup-pi.sh when there is
    # already a clone (it may not be under the default path), so an existing
    # checkout is reused instead of a second copy being cloned beside it.
    repo_dir = os.environ.get("PI_REPO_DIR", "").strip()
    hostname = os.environ.get("PI_HOSTNAME") or env.get("PI_HOSTNAME", "f10pi")
    eth_ll = env.get("ETH_LINK_LOCAL", "169.254.10.10/16")

    try:
        repo_branch = subprocess.run(
            ["git", "-C", ROOT, "rev-parse", "--abbrev-ref", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip() or "master"
    except Exception:
        repo_branch = "master"

    try:
        repo_url = subprocess.run(
            ["git", "-C", ROOT, "remote", "get-url", "origin"],
            check=True, capture_output=True, text=True).stdout.strip()
        if repo_url.startswith("git@") and ":" in repo_url:
            h, _, p = repo_url[4:].partition(":")
            repo_url = f"https://{h}/{p}"
    except Exception:
        repo_url = ""

    print(f"[gen] droplet      {droplet_ip}:{wg_port}")
    print(f"[gen] pi user      {pi_user}")
    print(f"[gen] repo on pi   {repo_dir or f'/home/{pi_user}/f10-dashboard'}")
    print(f"[gen] minting the Pi's WireGuard keypair on the server ...")
    priv, pub, srv_pub = server_keys(host, hostname)
    print(f"[gen] Pi public key {pub}")

    # Persist the peer so `make deploy` keeps it in wg0.conf.
    with open(PEERS, "w", encoding="utf-8") as fh:
        fh.write(
            "---\n"
            "# Generated by infra/scripts/gen_pi_setup.py - do not edit by hand.\n"
            "# Gitignored: device public keys identify your hardware.\n"
            "wireguard_peers:\n"
            f"  - name: {hostname}\n"
            f"    public_key: \"{pub}\"\n"
            f"    allowed_ips: \"{pi_wg_ip}/32\"\n"
        )
    print(f"[gen] wrote {os.path.relpath(PEERS, ROOT)}")

    if nets:
        wifi_block = "\n".join(
            f"add_wifi {shlex.quote(s)} {shlex.quote(p)} {shlex.quote(str(pr))}"
            for s, p, pr in nets
        )
        print(f"[gen] {len(nets)} Wi-Fi network(s) will be configured")
    else:
        wifi_block = ('log "no Wi-Fi networks given - leaving the Pi\'s '
                      'existing Wi-Fi untouched"')
        print("[gen] no Wi-Fi networks given - the Pi's Wi-Fi is left alone")

    script = TEMPLATE.format(
        hostname=hostname, pi_user=pi_user, repo_url=repo_url,
        repo_dir=repo_dir or f"/home/{pi_user}/f10-dashboard",
        repo_branch=repo_branch,
        eth_ll=eth_ll, wifi_block=wifi_block,
        do_wifi=1 if nets else 0,
        pi_wg_ip=pi_wg_ip, wg_priv=priv, wg_srv_pub=srv_pub,
        endpoint=f"{droplet_ip}:{wg_port}",
        wg_subnet=env.get("WG_SUBNET", "10.77.0.0/24"),
        wg_server_ip=wg_server_ip, ingest_token=ingest_token,
        dash_port=dash_port,
    )

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(script)
    os.chmod(OUT, 0o700)

    rel = os.path.relpath(OUT, ROOT)
    print(f"[gen] wrote {rel} (contains secrets, gitignored, mode 700)")
    print()
    print("next:")
    print("  1. cd infra && make deploy            # server registers the peer")
    print(f"  2. scp {rel} <pi-on-your-lan>:")
    print("  3. ssh <pi> 'sudo ./pi-setup.sh'")
    print(f"  4. ssh -J {host} {pi_user}@{pi_wg_ip}")
    return 0


TEMPLATE = r"""#!/usr/bin/env bash
#
# GENERATED - do not commit. Contains a WireGuard private key and Wi-Fi
# passwords in plain text. Run once on the Raspberry Pi, as root:
#
#     sudo ./pi-setup.sh
#
# Re-running is safe: every step is idempotent.
#
# What it does: writes the Pi's config files from the current infrastructure
# state, then hands over to the repo's own bootstrap.sh, so the provisioning
# logic lives in one tested place rather than being duplicated here.
set -euo pipefail

HOSTNAME_WANTED="{hostname}"
PI_USER="{pi_user}"
REPO_URL="{repo_url}"
REPO_DIR="{repo_dir}"
F10PI_DIR="$REPO_DIR/hardware/raspberry-pi/f10pi"

log()  {{ printf '\033[1;34m[pi-setup]\033[0m %s\n' "$*"; }}
die()  {{ printf '\033[1;31m[pi-setup] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }}

[[ $EUID -eq 0 ]] || die "run as root:  sudo ./pi-setup.sh"

# ---------------------------------------------------------------- the repo
BRANCH="{repo_branch}"
asuser() {{ sudo -u "$PI_USER" git -C "$REPO_DIR" "$@"; }}

if [[ -d "$REPO_DIR/.git" ]]; then
  log "repo already cloned at $REPO_DIR - skipping clone"
  if asuser pull --ff-only 2>&1 | sed 's/^/  /'; then
    log "  now at $(asuser rev-parse --short HEAD)"
  else
    log "  fast-forward failed (see above) - will recover below if needed"
  fi
else
  [[ -n "$REPO_URL" ]] || die "no repo at $REPO_DIR and REPO_URL is empty"
  log "cloning $REPO_URL -> $REPO_DIR"
  install -d -o "$PI_USER" -g "$PI_USER" "$(dirname "$REPO_DIR")"
  sudo -u "$PI_USER" git clone "$REPO_URL" "$REPO_DIR"
fi

# A checkout that predates the current layout has no hardware/ directory, and
# a failed fast-forward leaves it that way. Recover instead of dying: stash
# anything local (recoverable with `git stash list`) and reset to the branch.
if [[ ! -d "$F10PI_DIR" ]]; then
  log "checkout is missing hardware/ - it predates the current layout"
  log "  stashing any local changes, then resetting to origin/$BRANCH"
  asuser stash push -u -m "pi-setup $(date -u +%Y-%m-%dT%H:%M:%SZ)" 2>&1 | sed 's/^/    /' || true
  asuser fetch origin 2>&1 | sed 's/^/    /' || die "git fetch failed - is the Pi online?"
  asuser checkout "$BRANCH" 2>&1 | sed 's/^/    /' || true
  asuser reset --hard "origin/$BRANCH" 2>&1 | sed 's/^/    /' \
    || die "could not reset to origin/$BRANCH"
  log "  now at $(asuser rev-parse --short HEAD)"
fi

[[ -d "$F10PI_DIR" ]] || die "$F10PI_DIR still missing.
  The checkout at $REPO_DIR does not contain hardware/raspberry-pi/f10pi.
  Check it is this project and on the right branch:
    git -C $REPO_DIR remote -v
    git -C $REPO_DIR log --oneline -1"
install -d -m 700 "$F10PI_DIR/config"

# ------------------------------------------------------------ local.env
log "writing config/local.env"
cat > "$F10PI_DIR/config/local.env" <<LOCALENV
PI_HOSTNAME={hostname}
PI_USER={pi_user}
REPO_DIR=$REPO_DIR
WLAN_IF=wlan0
ETH_IF=eth0
WG_IF=wg0
ETH_LINK_LOCAL={eth_ll}
WG_SERVER_IP={wg_server_ip}
DO_HOSTNAME=1
DO_WIFI={do_wifi}
DO_WIREGUARD=1
DO_SSH=1
DO_ETH0_BMW=1
DO_APP_SERVICES=1
SSH_DISABLE_PASSWORD_AUTH=0
LOCALENV
chmod 600 "$F10PI_DIR/config/local.env"

# -------------------------------------------------------------- wifi.env
# Only rewritten when networks were supplied; otherwise the Pi's existing
# Wi-Fi configuration is left exactly as it is (DO_WIFI above is then 0, so
# bootstrap.sh skips the Wi-Fi step entirely).
WIFI_N=0
if [[ {do_wifi} -eq 1 ]]; then
log "writing config/wifi.env"
: > "$F10PI_DIR/config/wifi.env"
chmod 600 "$F10PI_DIR/config/wifi.env"
add_wifi() {{   # ssid psk priority
  WIFI_N=$((WIFI_N + 1))
  {{
    printf 'WIFI_%d_SSID=%s\n'     "$WIFI_N" "$1"
    printf 'WIFI_%d_PSK=%s\n'      "$WIFI_N" "$2"
    printf 'WIFI_%d_PRIORITY=%s\n' "$WIFI_N" "$3"
  }} >> "$F10PI_DIR/config/wifi.env"
  log "  wifi $WIFI_N: priority $3"
}}

fi

{wifi_block}

if [[ {do_wifi} -eq 1 ]]; then
  printf 'WIFI_COUNT=%d\n' "$WIFI_N" >> "$F10PI_DIR/config/wifi.env"
fi

# --------------------------------------------------------- wireguard.conf
log "writing config/wireguard.conf"
cat > "$F10PI_DIR/config/wireguard.conf" <<WGCONF
[Interface]
Address = {pi_wg_ip}/32
PrivateKey = {wg_priv}

[Peer]
PublicKey = {wg_srv_pub}
Endpoint = {endpoint}
# The whole VPN subnet, so the server (and any future peer) is reachable.
AllowedIPs = {wg_subnet}
# The Pi dials out; the keepalive holds the NAT mapping open so the server
# can reach back into the tunnel.
PersistentKeepalive = 25
WGCONF
chmod 600 "$F10PI_DIR/config/wireguard.conf"

# ------------------------------------------------------- sync agent config
log "writing infra/sync/config.json"
install -d -o "$PI_USER" -g "$PI_USER" "$REPO_DIR/infra/sync"
cat > "$REPO_DIR/infra/sync/config.json" <<SYNCCFG
{{
  "server_url": "http://{wg_server_ip}:8090",
  "token": "{ingest_token}",
  "databases": ["local/sessions/*.db"],
  "state_file": "local/sync-state.json",
  "batch_rows": 5000,
  "idle_interval": 5.0,
  "control_port": 8091,
  "mapping_ver": "",
  "connect_timeout": 10.0,
  "read_timeout": 60.0,
  "max_backoff": 60.0,
  "enabled": true
}}
SYNCCFG
chown "$PI_USER:$PI_USER" "$REPO_DIR/infra/sync/config.json"
chmod 600 "$REPO_DIR/infra/sync/config.json"

# ------------------------------------------------------------- provision
log "handing over to bootstrap.sh"
cd "$F10PI_DIR"
./scripts/bootstrap.sh

# -------------------------------------------------------------- verify
log "verifying"
./scripts/verify.sh || true

cat <<'DONE'

------------------------------------------------------------------
Done. From now on reach this Pi through the server:

    ssh -J <admin>@<droplet> {pi_user}@{pi_wg_ip}

Check the tunnel came up:  sudo wg show wg0     (want a recent handshake)
If there is no handshake, confirm the peer is registered on the server
(`make deploy` on your laptop) and that this Pi has internet on wlan0.

Delete this script when you are done - it holds a private key.
------------------------------------------------------------------
DONE
"""


if __name__ == "__main__":
    sys.exit(main())
