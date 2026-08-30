#!/usr/bin/env python3
"""
f10-admin - the Pi's control panel, for a phone on the same network.

Everything here is something the owner would otherwise do over SSH while
sitting in the car: check whether the runtime is alive, read why it
isn't, pull a fix, restart it, and shut the box down cleanly before
pulling the power. It exists because a phone in a driver's seat is a bad
SSH client.

RUNS AS ITS OWN PROCESS, deliberately. It has to be able to restart
live.py, which it could not do from inside live.py - and when live.py has
crashed is exactly when this needs to still answer. Separate systemd
unit, separate port.

SECURITY, stated plainly
------------------------
This is a privileged surface. `pull` fetches from a git remote and the
runtime then executes it, so anyone who can reach this panel and
authenticate can run code on the Pi. That is the intended feature, not a
flaw, but it sets the bar for everything else:

  * **Binds to a specific address, never 0.0.0.0.** The Pi joins Wi-Fi
    networks the owner does not control (a hotspot, a car park). The
    listen address comes from config and the default is the loopback,
    so a misconfigured deployment is unreachable rather than exposed.
  * **HTTP Basic auth**, credentials from a gitignored config file,
    compared with `hmac.compare_digest`. Over plain HTTP on a LAN the
    credentials are base64 on every request - readable by anyone
    sniffing that network. That is an accepted trade for a device on a
    network the owner mostly controls; it is not a secret worth reusing
    anywhere else.
  * **A custom header is required on every mutating request.** Browsers
    attach cached Basic credentials automatically, so without this a
    malicious page open on the phone could POST here cross-origin. A
    custom header cannot be set cross-origin without a preflight, which
    is refused.
  * **No shell, ever.** Every command is a fixed argv list; nothing from
    a request is ever interpolated into one. The set of runnable
    commands is closed and defined below.
  * **Not root.** Runs as the app user; the three commands needing
    privilege go through a sudoers allowlist naming them exactly.
  * **The git remote is pinned.** `pull` verifies origin still points at
    the configured URL and refuses otherwise, so the update channel
    cannot be repointed at another repository.

Stdlib only, like the rest of the runtime.
"""

import argparse
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))

#: Sent by the page on every mutating request. Its only job is to be a
#: header a cross-origin form post cannot set. See the note above.
CSRF_HEADER = "X-F10-Admin"

DEFAULTS: Dict[str, Any] = {
    #: Loopback by default: a deployment that forgets to set this is
    #: useless rather than exposed. setup writes the LAN address here.
    "bind": "127.0.0.1",
    "port": 8088,
    "username": "",
    "password": "",
    "repo_dir": "/home/f10/f10-dashboard",
    #: `pull` refuses unless origin still matches this exactly.
    "git_remote": "",
    "git_branch": "master",
    #: Units this panel may act on. A name not in here is refused, so the
    #: request can never name an arbitrary unit.
    "services": ["f10-dashboard", "f10-sync"],
    #: Where the sync agent's read-only status lives.
    "sync_status_url": "http://127.0.0.1:8091/sync/status",
    "log_lines": 200,
}


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)

    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))

    for key in list(cfg):
        env = os.environ.get("F10_ADMIN_" + key.upper())

        if env is None:
            continue

        current = cfg[key]
        cfg[key] = env if isinstance(current, (list, dict)) else type(current)(env)

    return cfg


# ----------------------------------------------------------- commands


def run(argv: List[str], timeout: float = 30.0) -> Tuple[int, str]:
    """
    Run a fixed argv list. No shell, no interpolation, ever.

    Returns (returncode, combined output) rather than raising: every
    caller here wants to show the failure on the page, not 500.
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g}s"
    except OSError as exc:
        return 1, str(exc)

    return proc.returncode, (proc.stdout + proc.stderr).strip()


def first_line(argv: List[str]) -> str:
    code, out = run(argv, timeout=5.0)

    return out.splitlines()[0].strip() if code == 0 and out else ""


# ----------------------------------------------------------- readings


def read_uptime() -> Optional[float]:
    try:
        with open("/proc/uptime", encoding="ascii") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_cpu_temp() -> Optional[float]:
    """
    Degrees C from the thermal zone.

    Read from sysfs rather than `vcgencmd` so it also works off a Pi
    (a laptop, a test box) and needs no video group membership.
    """
    try:
        with open(
            "/sys/class/thermal/thermal_zone0/temp", encoding="ascii"
        ) as fh:
            return int(fh.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


#: Bits of `vcgencmd get_throttled`. The low bits are live conditions,
#: the high bits latch since boot - a car that cooked the Pi last week
#: still shows there, which is the interesting part.
THROTTLE_BITS = (
    (0, "under-voltage"),
    (1, "arm frequency capped"),
    (2, "currently throttled"),
    (3, "soft temperature limit"),
    (16, "under-voltage occurred"),
    (17, "arm frequency capping occurred"),
    (18, "throttling occurred"),
    (19, "soft temperature limit occurred"),
)


def read_throttled() -> Optional[Dict[str, Any]]:
    """
    Power/thermal health. Pi-only; None elsewhere.

    Worth surfacing above almost everything else: a Pi on a powerbank in
    a hot car under-volts and throttles, and the symptom is 'recording
    randomly stopped', which looks like a software bug for weeks.
    """
    out = first_line(["vcgencmd", "get_throttled"])

    if not out.startswith("throttled="):
        return None

    try:
        value = int(out.split("=", 1)[1], 16)
    except ValueError:
        return None

    return {
        "raw": f"0x{value:X}",
        "ok": value == 0,
        "flags": [label for bit, label in THROTTLE_BITS if value & (1 << bit)],
    }


def read_clock() -> Dict[str, Any]:
    """
    Whether the host clock is NTP-disciplined.

    The Pi has no RTC, and a run recorded against a stale clock has
    wrong timestamps - on 2026-08-29 one was stretched 76 minutes by a
    correction landing mid-drive. This is the panel's answer to "is it
    safe to be recording right now?".
    """
    synced = os.path.exists("/run/systemd/timesync/synchronized")

    if not synced:
        synced = first_line([
            "timedatectl", "show", "-p", "NTPSynchronized", "--value",
        ]) == "yes"

    return {
        "synced": synced,
        "utc": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
    }


def read_wifi() -> Dict[str, Any]:
    """
    SSID and signal. Tells you whether the sync agent can ship at all.
    """
    ssid = first_line(["iwgetid", "-r"])
    signal = None

    try:
        with open("/proc/net/wireless", encoding="ascii") as fh:
            for line in fh.readlines()[2:]:
                parts = line.split()

                if len(parts) > 3:
                    #: column 3 is link quality, trailing '.' and all
                    signal = float(parts[2].rstrip("."))
                    break
    except (OSError, ValueError, IndexError):
        pass

    return {"ssid": ssid, "quality": signal}


def read_disk(path: str) -> Dict[str, Any]:
    """
    Free space where the session databases land.

    This is how recording stops silently: the card fills, SQLite starts
    failing writes, and nothing on the dashboard says so.
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {}

    return {
        "total_gb": round(usage.total / 1e9, 1),
        "free_gb": round(usage.free / 1e9, 1),
        "used_pct": round(100.0 * (usage.total - usage.free) / usage.total, 1),
    }


def read_service(unit: str) -> Dict[str, Any]:
    code, state = run(
        ["systemctl", "is-active", unit], timeout=5.0
    )
    _, enabled = run(["systemctl", "is-enabled", unit], timeout=5.0)
    since = first_line([
        "systemctl", "show", unit, "--property=ActiveEnterTimestamp",
        "--value",
    ])

    return {
        "unit": unit,
        "active": code == 0,
        "state": state or "unknown",
        "enabled": enabled or "unknown",
        "since": since,
    }


def read_git(repo: str, expected_remote: str, branch: str) -> Dict[str, Any]:
    """
    What revision is deployed, and how far behind the remote it is.

    `behind` is only meaningful after a fetch, which `status` does not do
    (it would make every page load hit the network over a mobile link).
    The Pull action fetches first and reports the real number.
    """
    def git(*args: str) -> str:
        return first_line(["git", "-C", repo, *args])

    remote = git("remote", "get-url", "origin")

    return {
        "revision": git("rev-parse", "--short", "HEAD"),
        "subject": git("log", "-1", "--pretty=%s"),
        "committed": git("log", "-1", "--pretty=%cr"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "remote": remote,
        #: The pin. A mismatch means the update channel was repointed,
        #: and `pull` refuses rather than running someone else's code.
        "remote_ok": bool(expected_remote) and remote == expected_remote,
        "expected_branch": branch,
    }


def read_sync(url: str) -> Dict[str, Any]:
    """The sync agent's own status, proxied read-only."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:                      # agent down is normal
        return {"reachable": False, "detail": str(exc)}

    pending = sum(
        int(db.get("pending") or 0)
        for db in (data.get("databases") or {}).values()
    )

    return {
        "reachable": True,
        "enabled": bool(data.get("enabled")),
        "state": data.get("state") or "unknown",
        "pending": pending,
        "last_error": data.get("last_error") or "",
    }


def status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the panel shows without being asked to change anything."""
    repo = cfg["repo_dir"]

    return {
        "host": socket.gethostname(),
        "now": time.time(),
        "uptime_s": read_uptime(),
        "cpu_temp_c": read_cpu_temp(),
        "throttled": read_throttled(),
        "clock": read_clock(),
        "wifi": read_wifi(),
        "disk": read_disk(repo),
        "services": [read_service(u) for u in cfg["services"]],
        "git": read_git(repo, cfg["git_remote"], cfg["git_branch"]),
        "sync": read_sync(cfg["sync_status_url"]),
    }


# ------------------------------------------------------------ actions


class ActionError(Exception):
    """A refusal the user should see, not a crash."""


def action_logs(cfg: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    unit = body.get("unit")

    if unit not in cfg["services"]:
        raise ActionError(f"unknown unit {unit!r}")

    lines = int(cfg["log_lines"])
    code, out = run([
        "journalctl", "-u", unit, "-n", str(lines),
        "--no-pager", "--output=short-iso",
    ], timeout=20.0)

    return {"unit": unit, "lines": out, "ok": code == 0}


def action_restart(cfg: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    unit = body.get("unit")

    if unit not in cfg["services"]:
        raise ActionError(f"unknown unit {unit!r}")

    code, out = run(["sudo", "-n", "/usr/bin/systemctl", "restart", unit],
                    timeout=60.0)

    if code != 0:
        raise ActionError(out or f"restart {unit} failed")

    return {"restarted": unit}


def action_service(cfg: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """Start or stop a unit - recording on/off without a reboot."""
    unit = body.get("unit")
    verb = body.get("verb")

    if unit not in cfg["services"]:
        raise ActionError(f"unknown unit {unit!r}")

    if verb not in ("start", "stop"):
        raise ActionError(f"unknown verb {verb!r}")

    code, out = run(["sudo", "-n", "/usr/bin/systemctl", verb, unit],
                    timeout=60.0)

    if code != 0:
        raise ActionError(out or f"{verb} {unit} failed")

    return {verb: unit}


def action_pull(cfg: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fetch and fast-forward the deployed checkout.

    Fast-forward only, and only after the remote pin is verified. A
    non-fast-forward means the Pi has local commits, which is a state a
    phone should not be resolving - it is reported and left alone.
    """
    repo = cfg["repo_dir"]
    expected = cfg["git_remote"]

    if not expected:
        raise ActionError(
            "git_remote is not configured; refusing to pull from an "
            "unverified remote"
        )

    remote = first_line(["git", "-C", repo, "remote", "get-url", "origin"])

    if remote != expected:
        raise ActionError(
            f"origin is {remote!r}, expected {expected!r} - refusing to pull"
        )

    before = first_line(["git", "-C", repo, "rev-parse", "--short", "HEAD"])

    code, fetch_out = run(["git", "-C", repo, "fetch", "--quiet", "origin"],
                          timeout=120.0)

    if code != 0:
        raise ActionError(fetch_out or "fetch failed")

    code, out = run([
        "git", "-C", repo, "merge", "--ff-only",
        f"origin/{cfg['git_branch']}",
    ], timeout=60.0)

    if code != 0:
        raise ActionError(
            (out or "fast-forward failed")
            + " - the checkout has diverged; fix it over SSH"
        )

    after = first_line(["git", "-C", repo, "rev-parse", "--short", "HEAD"])

    return {
        "before": before,
        "after": after,
        "changed": before != after,
        "detail": out,
        #: Deliberately does NOT restart. Pulling and restarting are two
        #: decisions: you may want the code staged and the current drive
        #: left recording until you stop.
        "note": "restart the runtime to run the new code",
    }


def action_reboot(cfg: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    code, out = run(["sudo", "-n", "/sbin/reboot"], timeout=10.0)

    if code != 0:
        raise ActionError(out or "reboot failed")

    return {"rebooting": True}


def action_shutdown(cfg: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Halt cleanly.

    The most valuable button here: the Pi runs off a powerbank that gets
    switched off by hand, and cutting power to a running system risks
    corrupting the SD card. This makes the safe path the easy one.
    """
    code, out = run(["sudo", "-n", "/sbin/poweroff"], timeout=10.0)

    if code != 0:
        raise ActionError(out or "poweroff failed")

    return {"halting": True}


ACTIONS = {
    "logs": action_logs,
    "restart": action_restart,
    "service": action_service,
    "pull": action_pull,
    "reboot": action_reboot,
    "shutdown": action_shutdown,
}

#: Actions that interrupt a drive or run new code. The page asks twice
#: for these; the server records that they were confirmed.
DESTRUCTIVE = frozenset({"reboot", "shutdown", "pull", "restart", "service"})


# ------------------------------------------------------------- server


def make_handler(cfg: Dict[str, Any]):
    expected_user = cfg["username"]
    expected_pass = cfg["password"]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "f10-admin"

        def log_message(self, *args):
            pass

        # -- plumbing -----------------------------------------------

        def _send(self, code: int, ctype: str, payload: bytes,
                  extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            #: Nothing here should ever be framed or sniffed.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'",
            )

            for key, value in (extra or {}).items():
                self.send_header(key, value)

            self.end_headers()
            self.wfile.write(payload)

        def _json(self, code: int, payload: Dict[str, Any]) -> None:
            self._send(code, "application/json",
                       json.dumps(payload).encode("utf-8"))

        def _unauthorized(self) -> None:
            self._send(
                401, "text/plain; charset=utf-8", b"authentication required\n",
                {"WWW-Authenticate": 'Basic realm="f10 admin", charset="UTF-8"'},
            )

        # -- auth ---------------------------------------------------

        def _authed(self) -> bool:
            """
            HTTP Basic, compared in constant time.

            An unconfigured username or password fails closed: a panel
            without credentials refuses everything rather than serving
            the controls to whoever asks.
            """
            if not expected_user or not expected_pass:
                return False

            header = self.headers.get("Authorization", "")

            if not header.startswith("Basic "):
                return False

            try:
                raw = b64decode(header[6:].strip(), validate=True)
                user, _, password = raw.decode("utf-8").partition(":")
            except Exception:
                return False

            #: Both compared, and both always compared, so the response
            #: time does not reveal which half was wrong.
            user_ok = hmac.compare_digest(user, expected_user)
            pass_ok = hmac.compare_digest(password, expected_pass)

            return user_ok and pass_ok

        # -- routes -------------------------------------------------

        def do_GET(self):
            path = self.path.split("?")[0]

            #: Unauthenticated liveness, so a watchdog can check the
            #: panel is up without holding credentials. Says nothing
            #: about the host.
            if path == "/healthz":
                self._send(200, "text/plain; charset=utf-8", b"ok\n")
                return

            if not self._authed():
                self._unauthorized()
                return

            if path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
                return

            if path == "/api/status":
                self._json(200, status(cfg))
                return

            self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def do_POST(self):
            if not self._authed():
                self._unauthorized()
                return

            path = self.path.split("?")[0]

            if not path.startswith("/api/action/"):
                self._send(404, "text/plain; charset=utf-8", b"not found\n")
                return

            #
            # CSRF. The browser attaches cached Basic credentials to any
            # request to this origin, including one triggered by another
            # page the phone has open. A custom header cannot be set
            # cross-origin without a preflight, and this server answers
            # no preflight - so requiring it is enough.
            #
            if self.headers.get(CSRF_HEADER) != "1":
                self._json(403, {"error": f"missing {CSRF_HEADER} header"})
                return

            name = path[len("/api/action/"):]
            handler = ACTIONS.get(name)

            if handler is None:
                self._json(404, {"error": f"unknown action {name!r}"})
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0

            raw = self.rfile.read(min(length, 8192)) if length > 0 else b"{}"

            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "bad JSON body"})
                return

            if not isinstance(body, dict):
                self._json(400, {"error": "bad JSON body"})
                return

            if name in DESTRUCTIVE and body.get("confirm") is not True:
                self._json(400, {
                    "error": f"{name} needs an explicit confirmation",
                })
                return

            try:
                result = handler(cfg, body)
            except ActionError as exc:
                self._json(409, {"error": str(exc)})
                return
            except Exception as exc:              # defensive
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return

            self._json(200, {"ok": True, "action": name, **result})

    return Handler


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--config",
                    default=os.path.join(HERE, "config.json"),
                    help="JSON config (default: config.json beside this file)")
    ap.add_argument("--bind", default=None, help="override the listen address")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)

    if args.bind:
        cfg["bind"] = args.bind

    if args.port:
        cfg["port"] = args.port

    if not cfg["username"] or not cfg["password"]:
        print("[!] username/password are not set - the panel would refuse "
              "every request. Set them in the config file.", file=sys.stderr)
        return 2

    if cfg["bind"] in ("0.0.0.0", "::"):
        #
        # Refused, not warned. This panel can reboot the host and make it
        # execute new code; on a hotspot or a car-park AP, a wildcard
        # bind offers that to everyone on the segment.
        #
        print("[!] refusing to bind 0.0.0.0 - name the LAN address "
              "explicitly (see README).", file=sys.stderr)
        return 2

    server = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), make_handler(cfg))
    server.daemon_threads = True

    print(f"[+] f10-admin on http://{cfg['bind']}:{cfg['port']}/", flush=True)
    print(f"[+] repo:     {cfg['repo_dir']}", flush=True)
    print(f"[+] services: {', '.join(cfg['services'])}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>F10 Pi</title>
<style>
  :root {
    --bg:#0b0e13; --card:#141922; --card2:#1b2230; --line:#263041;
    --text:#e6edf7; --muted:#8b97ab; --good:#199e70; --warn:#c98500;
    --bad:#e66767; --accent:#3987e5;
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    padding:env(safe-area-inset-top) 0 calc(24px + env(safe-area-inset-bottom));
  }
  .wrap { max-width:640px; margin:0 auto; padding:16px; }
  h1 { font-size:19px; margin:4px 0 2px; }
  .sub { color:var(--muted); font-size:12.5px; margin:0 0 16px;
         font-variant-numeric:tabular-nums; }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:12px; padding:14px; margin-bottom:12px; }
  .card h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
             color:var(--muted); margin:0 0 12px; font-weight:600; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
          gap:10px; }
  .stat { background:var(--card2); border-radius:9px; padding:10px 12px; }
  .stat .l { display:block; font-size:11px; color:var(--muted);
             text-transform:uppercase; letter-spacing:.05em; }
  .stat .v { display:block; font-size:19px; font-weight:600; margin-top:3px;
             font-variant-numeric:tabular-nums; }
  .stat .v.small { font-size:14px; font-weight:500; }
  .stat.good .v { color:var(--good); } .stat.warn .v { color:var(--warn); }
  .stat.bad .v  { color:var(--bad); }
  .svc { display:flex; align-items:center; gap:10px; padding:11px 0;
         border-bottom:1px solid var(--line); }
  .svc:last-child { border-bottom:0; padding-bottom:0; }
  .svc:first-of-type { padding-top:0; }
  .dot { width:9px; height:9px; border-radius:50%; flex:0 0 auto;
         background:var(--bad); }
  .dot.on { background:var(--good); }
  .svc .nm { flex:1; min-width:0; }
  .svc .nm b { display:block; font-size:14px; font-weight:600; }
  .svc .nm span { display:block; font-size:11.5px; color:var(--muted); }
  button {
    font:inherit; font-weight:600; font-size:13.5px; color:var(--text);
    background:var(--card2); border:1px solid var(--line); border-radius:9px;
    padding:9px 13px; cursor:pointer; min-height:40px;
  }
  button:active { background:var(--line); }
  button:disabled { opacity:.45; }
  button.wide { width:100%; }
  button.danger { border-color:#5a2d2d; color:#ffb4b4; }
  button.armed { background:var(--bad); border-color:var(--bad); color:#fff; }
  .row { display:flex; gap:8px; flex-wrap:wrap; }
  .row > button { flex:1 1 auto; }
  pre { background:#080b10; border:1px solid var(--line); border-radius:9px;
        padding:11px; margin:10px 0 0; font-size:11px; line-height:1.55;
        max-height:52vh; overflow:auto; white-space:pre-wrap;
        word-break:break-word; font-family:ui-monospace,Menlo,monospace; }
  .msg { margin-top:10px; padding:10px 12px; border-radius:9px; font-size:13px; }
  .msg.ok  { background:#0f2f22; color:#8fe3bd; }
  .msg.err { background:#331a1a; color:#ffb4b4; }
  .flags { margin-top:8px; font-size:12.5px; color:var(--warn); }
  .git { font-size:13px; }
  .git .rev { font-family:ui-monospace,Menlo,monospace; font-weight:600; }
  .git .sub2 { color:var(--muted); font-size:12px; margin-top:3px;
               overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .pill { display:inline-block; font-size:11px; padding:2px 7px;
          border-radius:99px; background:var(--card2); color:var(--muted);
          margin-left:6px; }
  .pill.bad { background:#3a1f1f; color:#ffb4b4; }
</style>
</head>
<body>
<div class="wrap">
  <h1 id="host">F10 Pi</h1>
  <p class="sub" id="sub">connecting…</p>

  <div class="card">
    <h2>Health</h2>
    <div class="grid" id="health"></div>
    <div class="flags" id="flags"></div>
  </div>

  <div class="card">
    <h2>Services</h2>
    <div id="services"></div>
  </div>

  <div class="card">
    <h2>Deployed code</h2>
    <div class="git" id="git"></div>
    <div class="row" style="margin-top:12px">
      <button id="btn-pull">Pull latest</button>
    </div>
  </div>

  <div class="card">
    <h2>Logs</h2>
    <div class="row" id="logbuttons"></div>
    <pre id="logs" style="display:none"></pre>
  </div>

  <div class="card">
    <h2>Power</h2>
    <div class="row">
      <button class="danger" data-act="reboot">Reboot</button>
      <button class="danger" data-act="shutdown">Shut down</button>
    </div>
    <p style="color:var(--muted);font-size:12px;margin:10px 0 0">
      Shut down before cutting the powerbank — pulling power from a running
      system risks corrupting the SD card.
    </p>
  </div>

  <div id="msg"></div>
</div>

<script>
const $ = id => document.getElementById(id);
let armed = null, armedTimer = null;

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

function post(action, extra) {
  return api("/api/action/" + action, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-F10-Admin": "1"},
    body: JSON.stringify(Object.assign({confirm: true}, extra || {})),
  });
}

function say(text, bad) {
  $("msg").innerHTML =
    `<div class="msg ${bad ? "err" : "ok"}">${escape_(text)}</div>`;
  clearTimeout(say._t);
  say._t = setTimeout(() => { $("msg").innerHTML = ""; }, 9000);
}

function escape_(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

/* Destructive buttons arm on first tap and fire on the second. A phone
   in a car pocket taps things; a reboot mid-drive costs the recording. */
function arm(btn, label, fire) {
  if (armed === btn) {
    clearTimeout(armedTimer);
    disarm(btn, label);
    fire();
    return;
  }
  if (armed) disarm(armed, armed.dataset.label);
  armed = btn;
  btn.dataset.label = label;
  btn.textContent = "Tap again to confirm";
  btn.classList.add("armed");
  armedTimer = setTimeout(() => disarm(btn, label), 5000);
}

function disarm(btn, label) {
  btn.textContent = label;
  btn.classList.remove("armed");
  if (armed === btn) armed = null;
}

function dur(s) {
  if (s == null) return "—";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600);
  const m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function stat(label, value, cls, small) {
  return `<div class="stat ${cls || ""}"><span class="l">${label}</span>` +
         `<span class="v${small ? " small" : ""}">${escape_(value)}</span></div>`;
}

function render(s) {
  $("host").textContent = s.host || "F10 Pi";
  $("sub").textContent = `up ${dur(s.uptime_s)} · ` +
    new Date(s.now * 1000).toLocaleTimeString();

  const t = s.cpu_temp_c;
  const disk = s.disk || {};
  const w = s.wifi || {};
  const sync = s.sync || {};
  const clock = s.clock || {};

  $("health").innerHTML = [
    stat("CPU temp", t == null ? "—" : t.toFixed(1) + "°C",
         t == null ? "" : t > 75 ? "bad" : t > 65 ? "warn" : "good"),
    stat("Disk free", disk.free_gb == null ? "—" : disk.free_gb + " GB",
         disk.free_gb == null ? "" :
         disk.free_gb < 1 ? "bad" : disk.free_gb < 3 ? "warn" : "good"),
    stat("Wi-Fi", w.ssid || "not connected", w.ssid ? "" : "warn", true),
    /* No RTC on this host. A run recorded against an undisciplined
       clock has wrong timestamps, and every trend built on it is
       wrong too - so this is a first-class health reading, not a
       detail. */
    stat("Clock", clock.synced ? "NTP synced" : "NOT synced",
         clock.synced ? "good" : "bad", true),
    /* Green means CAUGHT UP. A backlog is not an error - the agent
       ships continuously - but showing 1,843 pending in green reads as
       "all fine" when the honest answer is "not shipped yet". */
    stat("Sync", !sync.reachable ? "unreachable"
         : sync.pending ? sync.pending.toLocaleString() + " pending"
         : (sync.state || "idle"),
         sync.last_error ? "bad"
         : !sync.reachable || !sync.enabled ? "warn"
         : sync.pending ? "" : "good", true),
  ].join("");

  const th = s.throttled;
  $("flags").textContent =
    th && !th.ok ? "⚠ " + th.flags.join(" · ") : "";

  $("services").innerHTML = (s.services || []).map(sv => `
    <div class="svc">
      <span class="dot ${sv.active ? "on" : ""}"></span>
      <span class="nm"><b>${escape_(sv.unit)}</b>
        <span>${escape_(sv.state)}${sv.since ? " · since " + escape_(sv.since.slice(0, 16)) : ""}</span>
      </span>
      <button data-restart="${escape_(sv.unit)}">Restart</button>
      <button data-toggle="${escape_(sv.unit)}" data-verb="${sv.active ? "stop" : "start"}">
        ${sv.active ? "Stop" : "Start"}</button>
    </div>`).join("") || '<span class="sub">none configured</span>';

  const g = s.git || {};
  $("git").innerHTML =
    `<span class="rev">${escape_(g.revision || "?")}</span>` +
    `<span class="pill">${escape_(g.branch || "?")}</span>` +
    (g.dirty ? '<span class="pill bad">local changes</span>' : "") +
    (g.remote_ok ? "" : '<span class="pill bad">remote not pinned</span>') +
    `<div class="sub2">${escape_(g.subject || "")}</div>` +
    `<div class="sub2">${escape_(g.committed || "")}</div>`;

  $("logbuttons").innerHTML = (s.services || [])
    .map(sv => `<button data-log="${escape_(sv.unit)}">${escape_(sv.unit)}</button>`)
    .join("");
}

async function refresh() {
  try {
    render(await api("/api/status"));
  } catch (e) {
    $("sub").textContent = "lost contact — " + e.message;
  }
}

document.addEventListener("click", async ev => {
  const b = ev.target.closest("button");
  if (!b) return;

  try {
    if (b.dataset.log) {
      b.disabled = true;
      const r = await post("logs", {unit: b.dataset.log});
      $("logs").style.display = "block";
      $("logs").textContent = r.lines || "(empty)";
      $("logs").scrollTop = $("logs").scrollHeight;
      b.disabled = false;
      return;
    }

    if (b.dataset.restart) {
      arm(b, "Restart", async () => {
        await post("restart", {unit: b.dataset.restart});
        say("Restarted " + b.dataset.restart);
        setTimeout(refresh, 1500);
      });
      return;
    }

    if (b.dataset.toggle) {
      const verb = b.dataset.verb;
      arm(b, verb === "stop" ? "Stop" : "Start", async () => {
        await post("service", {unit: b.dataset.toggle, verb});
        say(verb + "ped " + b.dataset.toggle);
        setTimeout(refresh, 1500);
      });
      return;
    }

    if (b.id === "btn-pull") {
      arm(b, "Pull latest", async () => {
        const r = await post("pull");
        say(r.changed ? `${r.before} → ${r.after}. ${r.note}.`
                      : "Already up to date.");
        refresh();
      });
      return;
    }

    const act = b.dataset.act;
    if (act === "reboot" || act === "shutdown") {
      arm(b, act === "reboot" ? "Reboot" : "Shut down", async () => {
        await post(act);
        say(act === "reboot" ? "Rebooting…" : "Halting — wait for the LED, "
            + "then cut power.");
      });
    }
  } catch (e) {
    say(e.message, true);
    refresh();
  }
});

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
