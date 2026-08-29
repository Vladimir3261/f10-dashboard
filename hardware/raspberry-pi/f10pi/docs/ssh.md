# SSH

Administering the Pi, from the LAN or from anywhere. Reaching it remotely is
the whole reason the WireGuard tunnel exists — see
[`wireguard.md`](wireguard.md).

The Pi needs no outbound SSH of its own: telemetry reaches the server over
HTTP through the tunnel, not over SSH.

## Into the Pi

- On the LAN: `ssh <PI_USER>@f10pi.local` (mDNS via avahi).
- **From anywhere:** through the VPS as a jump host. Your laptop does NOT
  join the VPN — it reaches the VPS over public SSH, and the VPS reaches the
  Pi over `wg0`:
  ```bash
  ssh -J root@<VPS_HOST> <PI_USER>@<WG_PI_IP>     # e.g. 10.77.0.10
  ```
  Or as two plain hops: `ssh root@<VPS_HOST>`, then
  `ssh <PI_USER>@<WG_PI_IP>`. Add a `ProxyJump` entry to `~/.ssh/config` to
  reduce it to `ssh f10pi`.

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

## The "Too many authentication failures" gotcha

If your agent offers many keys, a server may cut you off before it reaches
the right one — and you then fall through to a password prompt. Pin the
identity:

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/<your-key> <PI_USER>@<WG_PI_IP>
```

Or set `IdentityFile` + `IdentitiesOnly yes` in the `~/.ssh/config` entry.

## Secrets rule

Private keys stay on the device that generated them and never enter git.
Only your **public** key goes onto the Pi, and it is placed there by the
provisioning scripts from `admin_ssh_public_keys` / your local `.pub` files.
