# Optional: a live coding agent on the Pi

**Not part of the telemetry system.** Nothing here is required to log drives,
sync them, or serve the dashboard — `bootstrap.sh` never touches any of it.
This is a convenience for people who want to work *on* the Pi from elsewhere:
a Claude Code session that is always running in `tmux`, survives reboots, and
can be driven from a phone.

Skip this page entirely if you do not want an agent living on the box.

## The problem

A `tmux` session started by hand dies with the next reboot, and reattaching
means SSHing in first. What you usually want instead is a session that is
simply *always there*, created at boot, ready to attach or drive remotely.

## The recipe

A **systemd user service plus linger** is the right tool: per-user, no root
configuration, and no repo changes.

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/claude-tmux.service <<'EOF'
[Unit]
Description=Claude Code in tmux, with Remote Control
After=default.target

[Service]
Type=forking
WorkingDirectory=%h/f10-dashboard
# --continue resumes the most recent conversation in that directory, so a
# reboot does not start from scratch. The || fallback matters: on a box with
# no previous conversation --continue has nothing to resume, and without it
# the loop would crash every 5 seconds. The loop itself keeps the tmux
# session alive when the agent exits.
ExecStart=/usr/bin/tmux new-session -d -s claude -c %h/f10-dashboard \
  'while true; do "$HOME/.local/bin/claude" --continue --remote-control \
     || "$HOME/.local/bin/claude" --remote-control; sleep 5; done'
ExecStop=/usr/bin/tmux kill-session -t claude
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
```

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now claude-tmux
```

Check the path first — `command -v claude` — and adjust `ExecStart` if the
binary lives somewhere else.

### Why each part matters

- **`loginctl enable-linger`** is what actually makes this survive. Without
  it your user's systemd manager stops when you log out and takes the session
  with it; the unit alone is not enough.
- **`--remote-control`** starts the session with Remote Control enabled, so
  it can be driven from a phone. The session name defaults to the hostname
  (`f10pi`); pass a name to override it.
- **`--continue`** resumes the most recent conversation *in the working
  directory*, so context carries across reboots instead of starting cold.
  Note what this does not do: the Remote Control connection itself cannot
  survive a power cycle, because the process died — the phone gets a fresh
  connection under the same name. If you want a fixed identity rather than
  "most recent", `--session-id <uuid>` / `--resume <id>` pin one explicitly,
  at the cost of breaking if that session is ever pruned.
- **It must be interactive**, which is why it runs inside `tmux` — a bare
  systemd service has no pty and the agent will not start properly.
- **The `while` loop** means `/exit` brings the agent back after 5 seconds.
  That is usually what you want for an always-available session; drop the
  wrapper if you would rather `/exit` end it.

## Using it

Attach over SSH from anywhere, through the server as a jump host:

```bash
ssh -J <admin>@<server> <PI_USER>@<PI_WG_IP> -t 'tmux attach -t claude'
```

Or drive it from a phone via Remote Control, where it appears under the
session name above.

## Verifying it survives a reboot

Do not assume — the failure only shows up later, when you are away from the
car:

```bash
sudo reboot
# then, once it is back:
systemctl --user status claude-tmux
tmux ls
```

If the session is missing, check in this order: `loginctl show-user $USER |
grep Linger`, then whether the agent is still authenticated (below), then
`journalctl --user -u claude-tmux`.

To confirm the agent itself is alive rather than just the session:

```bash
systemctl --user is-active claude-tmux
pgrep -af 'claude --continue --remote-control'
```

`tmux list-panes` is misleading here — it reports `bash`, because the `while`
loop is the pane's foreground process, not the agent. If the loop is running
but no agent PID exists, it is crash-looping every 5 seconds; the usual cause
is lost authentication, and the output goes to the tmux pane rather than the
journal, so attach and look.

## Things worth knowing before you enable it

- **Authentication has to already exist.** The agent must be logged in as
  that user before you rely on a boot-time start — a service starting at boot
  cannot complete a login prompt. Sign in once interactively first.
- **This is a shell on the box, always on.** The Pi holds a WireGuard private
  key and sits on the diagnostic link to the car. An always-running agent
  with shell access there is a real increase in blast radius: anyone who
  reaches that session reaches the car link and the VPN. That is a reasonable
  trade for a personal project, but it should be a decision rather than an
  accident.
- **Do not expose it over HTTP.** A web terminal (`ttyd`, `gotty` and
  friends) on the public dashboard domain would put an authenticated shell on
  the internet, on the box with the keys. SSH from a phone client is the
  smaller surface, and you already have it.
- **It competes for the same Pi.** The telemetry runtime is what matters
  during a drive; an agent doing heavy work on a 4-core Pi while logging at
  ~9 Hz is worth keeping in mind if you see dropped samples.

## Removing it

```bash
systemctl --user disable --now claude-tmux
rm ~/.config/systemd/user/claude-tmux.service
sudo loginctl disable-linger "$USER"     # only if nothing else needs it
```
