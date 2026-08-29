# SSH

Two directions:

1. **Into the Pi** — you administer the Pi, over `wg0` from anywhere or via
   `f10pi.local` on the LAN. Reaching it remotely is the whole reason the
   WireGuard tunnel exists; see [`wireguard.md`](wireguard.md).
2. **Pi → server** — the Pi opens outbound SSH to the telemetry server when
   you need it (a shell, a file copy), reached over `wg0`.

## Into the Pi

- On the LAN: `ssh <PI_USER>@f10pi.local` (mDNS via avahi).
- **From anywhere:** over `wg0` at the Pi's VPN address:
  ```bash
  ssh <PI_USER>@<WG_PI_IP>        # e.g. 10.77.0.10
  ```
  This needs **your laptop to be a WireGuard peer too** — the tunnel relays
  laptop↔Pi through the server, so a tunnel with only the Pi in it gives you
  nothing to connect from. Add both to `wireguard_peers` in
  `infra/ansible/group_vars/all.yml` and re-run `make deploy`, and make sure
  the laptop's client config sets `AllowedIPs` to the whole VPN subnet.

There is deliberately **no public SSH path to the Pi** — no port forwarding,
no reverse tunnel. If the VPN is down, the fallback is physical access
(monitor + keyboard) or the LAN; see [`recovery.md`](recovery.md).

### Hardening (do it only after keys work)

`configure-ssh.sh` can switch sshd to key-only auth, but leave it off for
first provisioning:

```bash
# in config/local.env, ONLY after you've confirmed key login works:
SSH_DISABLE_PASSWORD_AUTH=1
sudo ./scripts/configure-ssh.sh
```

That drops in `/etc/ssh/sshd_config.d/10-f10pi.conf` with
`PasswordAuthentication no`, `PubkeyAuthentication yes`,
`PermitRootLogin no`. **Confirm you can log in with your key in a second
session before closing your current one** — otherwise you can lock
yourself out (recovery: [`recovery.md`](recovery.md#ssh-lockout)).

## Pi → server

Uses a **dedicated key** for this hop and a generic host alias, so no real
server hostname/IP is committed. Template: `config/ssh_config.example` →
`config/ssh_config` (gitignored), installed by `configure-ssh.sh` into
`~/.ssh/config.d/telemetry-server`.

```bash
# generate the dedicated key on the Pi
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_f10pi -C f10pi
# add id_ed25519_f10pi.pub to the server's authorized_keys, then:
ssh telemetry-server        # resolves to <WG_SERVER_IP> over wg0
```

### The "Too many authentication failures" gotcha

If your agent offers many keys, the server may cut you off before trying
the right one. The alias pins the identity:

```
IdentityFile ~/.ssh/id_ed25519_f10pi
IdentitiesOnly yes
```

For a one-off manual connection you can force it directly:

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519_f10pi telemetry-server
```

## Secrets rule

Private keys stay on the device that generated them and never enter git.
The `ssh_config` template references only `<WG_SERVER_IP>` and
`<SERVER_USER>` placeholders; the real file is gitignored.
