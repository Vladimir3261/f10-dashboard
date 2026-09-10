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

THE FRONT DOOR
--------------
Since #40 this is the one page the owner opens. The telemetry views
(Drive / Detail / All-data) are the `dashboard/` files live.py serves,
shown here unchanged in a frame, and the runtime's API is reached
through this process: an allowlist of owner paths plus the whole
share prefix are reverse-proxied to live.py (`dashboard_url`), so the
phone talks to one origin, behind one login. live.py keeps binding its
own port; the service unit now holds it on the loopback so the panel is
the only way in from the network. The panel's credentials stop here -
they are never forwarded - and under `/s/` live.py stays the authority:
the panel adds no login there and dispatches none of its own routes
for that prefix, so nothing management-shaped is reachable through it.

The odometer API (`/api/odometer`, `/api/odometer/stream`; issue #49,
docs/ODOMETER_API.md) is the second such surface: the navigation client
carries live.py's own bearer token, not the panel's login, so those two
exact paths go through without the panel's auth and WITH their
`Authorization` header - the one place it is forwarded. Everything
else keeps stripping it. No panel route is dispatched for them.

Stdlib only, like the rest of the runtime.
"""

import argparse
import hmac
import http.client
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))

#
# `systemctl --user` and tmux both need to find this user's runtime
# directory. A System service started with User= does not inherit it the
# way a login shell would, so set it if the environment lacks it -
# otherwise every user-scope call fails with "Failed to connect to bus".
#
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

#: Sent by the page on every mutating request. Its only job is to be a
#: header a cross-origin form post cannot set. See the note above.
CSRF_HEADER = "X-F10-Admin"

#
# The telemetry UI: the same three files live.py serves, read from the
# checkout this panel runs from - not a copy, not a fork. `/dashboard/`
# is the frame document the Drive / Detail / All-data tabs show; the
# relative `style.css` / `app.js` it references resolve beside it.
#
DASHBOARD_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "dashboard"))

TELEMETRY_FILES: Dict[str, Tuple[str, str]] = {
    "/dashboard/": ("index.html", "text/html; charset=utf-8"),
    "/dashboard/style.css": ("style.css", "text/css; charset=utf-8"),
    "/dashboard/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

#: What the panel's own page may do. Frames are for the telemetry
#: document, which is same-origin.
PANEL_CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
             "script-src 'unsafe-inline'; connect-src 'self'; "
             "frame-src 'self'")

#: What a share viewer's browser gets when the Pi is up and live.py is
#: not: the public prefix, so nothing of the box - no address, no
#: upstream, no reason - just that it is not running, and a retry.
#: Self-contained; the same wording as the VPS's page for the Pi
#: itself being unreachable.
SHARE_DOWN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Car unreachable</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;
justify-content:center;background:#0b0d10;color:#c9d1d9;
font:16px/1.5 system-ui,sans-serif;text-align:center}
main{max-width:22rem;padding:2rem}h1{font-size:1.3rem;margin:0 0 .5rem}
p{margin:.4rem 0;color:#8b949e}
</style></head><body><main>
<h1>The car is unreachable</h1>
<p>Its telemetry is not running right now. This is normal when the car
is parked.</p>
<p>This page retries by itself.</p>
</main></body></html>
"""

#: The framed telemetry document: its own files, its API through this
#: origin, and the sync agent's pause/resume on :8091 (app.js talks to
#: the agent directly, by design - see the comment there).
TELEMETRY_CSP = ("default-src 'none'; style-src 'self' 'unsafe-inline'; "
                 "script-src 'self'; img-src 'self' data:; "
                 "connect-src 'self' http://*:8091; frame-ancestors 'self'")

#: live.py's share surface. Everything at or below it is live.py's to
#: answer, unauthenticated - the token in the link is the credential
#: there, and live.py is the authority on it.
SHARE_PREFIX = "/s"

#
# The owner-side paths live.py answers, reached through this panel. An
# allowlist rather than "everything else under /api/": the panel's own
# /api/status and /api/action/* must never be shadowed by, or confused
# with, something the runtime serves.
#
PROXY_GET = frozenset({
    "/api/snapshot", "/api/stream", "/api/meta", "/api/runs",
    "/api/history", "/api/sync", "/api/diagnostics", "/api/modes",
    "/api/share",
})
PROXY_POST = frozenset({"/api/mode", "/api/share", "/api/share/revoke"})

#: Never relayed in either direction (RFC 7230 hop-by-hop), plus what
#: this end sets itself.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding",
    "upgrade",
})
#: Request headers that stop at the panel: its own login (live.py has no
#: use for it and must never see it), the Host it rewrites, and the
#: body length it restates.
DROPPED_REQUEST_HEADERS = HOP_BY_HOP | {"authorization", "host",
                                        "content-length"}
#: The odometer API - live.py's own bearer token is the credential, so
#: these two exact paths are proxied without the panel's login and are
#: the ONLY ones whose Authorization header reaches live.py. Exact
#: paths, not a prefix: nothing else under /api/odometer exists, and a
#: lookalike must not ride through on it.
ODOMETER_PATHS = frozenset({"/api/odometer", "/api/odometer/stream"})
#: What live.py believes about the client - the address, the scheme and
#: the public name a share link is minted against. This panel SETS
#: them; a copy the client sent is replaced, never forwarded ahead of
#: ours (live.py takes the first value). The one exception is a request
#: that arrived from a `trusted_proxies` address - the VPS-side nginx,
#: from its wg0 address - whose values are the truth about the hop
#: before it; an empty value from it counts as not sent.
FORWARDED_HEADERS = frozenset({"x-forwarded-for", "x-forwarded-proto",
                               "x-forwarded-host"})
#: Response headers this server adds itself; a second copy from
#: upstream would be a duplicate.
DROPPED_RESPONSE_HEADERS = HOP_BY_HOP | {"server", "date"}

#: Connecting to live.py on the loopback either works at once or not at
#: all. This bounds "not at all"; it is NOT a read deadline - see
#: _proxy.
CONNECT_TIMEOUT_S = 3.0
#: The read deadline for everything that is NOT a stream: a runtime
#: that accepts the connection and never answers (the process exists,
#: its loop is stuck) would otherwise pin one panel thread per phone
#: refresh, for good. A JSON answer takes milliseconds; a stream is
#: silent for as long as the car is, and gets no deadline at all.
READ_TIMEOUT_S = 15.0
#: The paths that are streams - the owner's, the share viewer's and the
#: navigation client's.
STREAM_PATHS = frozenset({"/api/stream", SHARE_PREFIX + "/api/stream",
                          "/api/odometer/stream"})
#: The largest body a proxied POST may carry. live.py reads 4 KiB.
MAX_PROXY_BODY = 16384
#: A listen address that is not there yet (wg0 comes up after the
#: panel) is retried this often.
BIND_RETRY_S = 15.0


def under_share(path: str) -> bool:
    """True for the share prefix itself and everything below it."""
    return path == SHARE_PREFIX or path.startswith(SHARE_PREFIX + "/")


def bind_refusal(address: str) -> Optional[str]:
    """
    Why this listen address is refused, or None if it may be bound.

    Semantic, not lexical: the kernel accepts `0`, `0.0`, `00.0.0.0`,
    `::0`, `0::0` and `::ffff:0.0.0.0` as "every interface" just as it
    accepts `0.0.0.0`, and a one-character F10_ADMIN_BIND typo must not
    open the panel to the segment. Anything that is not an IP literal
    is refused too - `[::]` or a host name would never bind and would
    otherwise sit in the retry loop for ever, silently.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "not an IP address literal"

    mapped = getattr(ip, "ipv4_mapped", None)

    if ip.is_unspecified or (mapped is not None and mapped.is_unspecified):
        return "a wildcard address (every interface)"

    return None

DEFAULTS: Dict[str, Any] = {
    #: Loopback by default: a deployment that forgets to set this is
    #: useless rather than exposed. setup writes the LAN address here.
    #: One address, or a list of them (the LAN address and the
    #: WireGuard one) - one listening socket each. Never a wildcard.
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
    #: The agent's control endpoints. live.py deliberately does NOT
    #: proxy these - its dashboard can be shared publicly. This panel is
    #: authenticated and LAN-only, so it is the right place for them.
    "sync_control_url": "http://127.0.0.1:8091",
    #: The runtime's own snapshot. "Is the service up?" and "is data
    #: landing?" are different questions; this answers the second.
    "dashboard_status_url": "http://127.0.0.1:8080/api/snapshot",
    #: live.py, as this panel reaches it. The telemetry views' API and
    #: the whole share prefix are forwarded here; the Car link tab's
    #: diagnostics too (an older config's `diagnostics_url` is ignored).
    "dashboard_url": "http://127.0.0.1:8080",
    #: Addresses whose X-Forwarded-* headers are believed - the reverse
    #: proxy in front of this panel (nginx on the VPS, over wg0: the
    #: server's tunnel address, 10.77.0.1, written by install.sh),
    #: by its address as this panel sees it. Empty means the panel is
    #: the edge: every client-sent X-Forwarded-* is replaced with what
    #: the panel itself knows.
    "trusted_proxies": [],
    #: Per-drive databases, and the agent's watermark file. Empty by
    #: default and DERIVED from `repo_dir` - never a hardcoded
    #: /home/<guess>/ path. A config written by an older version of the
    #: installer lacks these keys entirely (install.sh will not overwrite
    #: an existing config, so it cannot clobber the password), and a
    #: guessed username silently pointed at a directory that does not
    #: exist - which read as "no drive file yet" while the runtime was
    #: recording perfectly well.
    "sessions_dir": "",
    "sync_state_file": "",
    "log_lines": 200,
    #: How far back "is it recording?" looks, in seconds.
    "recording_window_s": 60,
    #
    # The optional Claude Code session (hardware/raspberry-pi/f10pi/
    # docs/claude-code.md). A systemd USER service, so no sudo is
    # involved - the panel already runs as that user. Set
    # claude_enabled false, or leave the unit absent, and the tab hides
    # itself.
    #
    "claude_enabled": True,
    "claude_unit": "claude-tmux",
    "claude_tmux_session": "claude",
    #: Lines of the tmux pane to show. This is the ONLY place a login
    #: prompt or a crash-loop error appears - the doc's point that
    #: `tmux list-panes` reports bash and the journal stays empty.
    "claude_pane_lines": 40,
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

    #
    # Anything path-shaped that was not given is derived from the repo,
    # which every config has carried since the first version. This is what
    # lets a config written by an older installer keep working across an
    # upgrade instead of falling back to a guess.
    #
    repo = cfg["repo_dir"]

    #: A list in the file, one string from the environment: normalised
    #: here so the proxy compares addresses, never substrings.
    cfg["trusted_proxies"] = listen_addresses(cfg.get("trusted_proxies")
                                              or [])

    if not cfg["sessions_dir"]:
        cfg["sessions_dir"] = os.path.join(repo, "local", "sessions")

    if not cfg["sync_state_file"]:
        cfg["sync_state_file"] = os.path.join(repo, "local", "sync-state.json")

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


def _fetch_json(url: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def newest_session(sessions_dir: str) -> Optional[str]:
    """The database the runtime is most likely writing to right now."""
    try:
        names = [
            os.path.join(sessions_dir, n)
            for n in os.listdir(sessions_dir) if n.endswith(".db")
        ]
    except OSError:
        return None

    if not names:
        return None

    return max(names, key=lambda p: os.path.getmtime(p))


def read_recording(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Is data actually landing?

    A green dot on the service only means the PROCESS is up. It is
    equally green with the ENET cable out, the car asleep, or the
    gateway refusing - and that is the failure worth catching in the
    driveway rather than in the lake a week later. So this counts rows
    that reached the database in the last minute, which is the only
    answer that cannot be faked by a healthy-looking process.
    """
    window = float(cfg["recording_window_s"])
    out: Dict[str, Any] = {
        "db": None, "samples": None, "channels": None, "window_s": window,
        "run": None, "mode": None, "clock_synced": None, "since": None,
    }

    #: The runtime's own view: link state, loop rate, drive mode.
    snap = _fetch_json(cfg["dashboard_status_url"])

    if snap is not None:
        out.update({
            "link": bool(snap.get("connected")),
            "status": snap.get("status") or "",
            "hz": snap.get("hz"),
            "mode": snap.get("mode"),
            "duty": snap.get("duty"),
            "clock_synced": snap.get("clock_synced"),
            "ecu": snap.get("ecu"),
        })
    else:
        out["link"] = None
        out["status"] = "runtime not answering"

    #: Distinct from `link`: the page banners the telemetry tabs on this,
    #: and reloads the frame when it flips back to true.
    out["up"] = snap is not None

    path = newest_session(cfg["sessions_dir"])

    if path is None:
        #
        # "no drive file yet" is a fine answer before the first drive and
        # a misleading one when the directory is simply wrong. Say which,
        # because the two look identical on the page and the second is a
        # configuration bug that reads as a quiet runtime.
        #
        out["sessions_dir"] = cfg["sessions_dir"]
        out["sessions_dir_exists"] = os.path.isdir(cfg["sessions_dir"])

        return out

    out["db"] = os.path.basename(path)

    try:
        #: Read-only; the runtime is writing this file. WAL allows
        #: concurrent readers, and both processes run as the same user.
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)

        try:
            cutoff = time.time() - window
            row = con.execute(
                "SELECT count(*), count(DISTINCT param_id) FROM samples "
                "WHERE ts > ?", (cutoff,),
            ).fetchone()
            out["samples"], out["channels"] = row[0], row[1]

            #
            # Only select columns this database actually has. `mode` and
            # `clock_synced` arrived on 2026-08-30; a drive recorded
            # before that has neither, and asking for them fails the
            # whole query - which would blank the recording panel for
            # every older file on the card.
            #
            have = {
                r[1] for r in con.execute("PRAGMA table_info(runs)")
            }
            optional = [c for c in ("mode", "clock_synced") if c in have]
            columns = ["id", "started_at"] + optional

            run = con.execute(
                f"SELECT {', '.join(columns)} FROM runs "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()

            if run:
                out["run"] = run[0]
                out["since"] = run[1]

                for name, value in zip(optional, run[2:]):
                    out["run_" + name] = value
        finally:
            con.close()
    except sqlite3.Error as exc:
        out["error"] = str(exc)

    return out


def _watermarks(state_file: str) -> Dict[str, int]:
    """Per-database synced rowid, from the sync agent's state file."""
    try:
        with open(state_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}

    return {
        os.path.basename(db): int(v.get("samples_rowid") or 0)
        for db, v in data.items() if isinstance(v, dict)
    }


def read_session_files(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Every per-drive database, with size and whether the lake has it.

    Disk-free tells you the card is filling; this tells you what to do
    about it. `synced` compares the agent's watermark against the
    database's own top rowid, so "safe to delete" is a fact rather than
    a guess.
    """
    sessions_dir = cfg["sessions_dir"]
    marks = _watermarks(cfg["sync_state_file"])
    active = newest_session(sessions_dir)
    out: List[Dict[str, Any]] = []

    try:
        names = sorted(
            n for n in os.listdir(sessions_dir) if n.endswith(".db")
        )
    except OSError:
        return out

    for name in names:
        path = os.path.join(sessions_dir, name)

        try:
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            continue

        top = None

        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)

            try:
                top = con.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM samples"
                ).fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error:
            pass

        synced_to = marks.get(name)
        out.append({
            "name": name,
            "size_mb": round(size / 1e6, 1),
            "mtime": mtime,
            "rows": top,
            "synced_rowid": synced_to,
            #: Unknown (None) whenever either number is missing - never
            #: guessed, because the answer gates a deletion.
            "synced": (
                None if top is None or synced_to is None
                else synced_to >= top
            ),
            "active": path == active,
        })

    out.sort(key=lambda r: r["mtime"], reverse=True)

    return out


def read_claude(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    The optional coding agent living on the Pi.

    Status and lifecycle only. Deliberately NOT a terminal and not a way
    to send it prompts: that would put an interactive shell behind an
    HTTP form on the box that holds the WireGuard key and sits on the
    diagnostic link to the car. SSH is the smaller surface and already
    exists.

    The one thing worth surfacing is the failure mode the setup doc
    calls out as invisible: the unit reports active because the `while`
    loop is alive, while the agent inside it crash-loops every five
    seconds - usually lost authentication - and the error goes to the
    tmux pane rather than the journal. `agent_running` separates those.
    """
    if not cfg.get("claude_enabled", True):
        return None

    unit = cfg["claude_unit"]
    session = cfg["claude_tmux_session"]

    #
    # Does the unit exist at all? `cat` is the direct question - it
    # fails only when there is no such unit. The feature is optional, so
    # its absence is not an error: the tab simply hides itself on a box
    # that never installed it, and on anything without systemd.
    #
    exists, _ = run(["systemctl", "--user", "cat", unit], timeout=5.0)

    if exists != 0:
        return None

    code, state = run(["systemctl", "--user", "is-active", unit], timeout=5.0)
    active = code == 0

    #: The agent process itself, not the loop that restarts it.
    agent_code, _ = run(["pgrep", "-f", "claude --continue"], timeout=5.0)
    agent_running = agent_code == 0

    has_session, _ = run(
        ["tmux", "has-session", "-t", session], timeout=5.0
    )

    pane = ""

    if has_session == 0:
        _, pane = run([
            "tmux", "capture-pane", "-p", "-t", session,
            "-S", f"-{int(cfg['claude_pane_lines'])}",
        ], timeout=5.0)

    return {
        "unit": unit,
        "active": active,
        "state": state or "unknown",
        "since": first_line([
            "systemctl", "--user", "show", unit,
            "--property=ActiveEnterTimestamp", "--value",
        ]),
        "tmux_session": session,
        "tmux_alive": has_session == 0,
        "agent_running": agent_running,
        #: active loop + no agent = crash-looping. The doc's warning,
        #: made visible.
        "crash_looping": active and not agent_running,
        #: Remote Control announces itself under the hostname unless
        #: told otherwise - this is what to look for in the phone app.
        "remote_name": socket.gethostname(),
        "pane": pane,
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
        "claude": read_claude(cfg),
        "recording": read_recording(cfg),
        "sessions": read_session_files(cfg),
        "services": [read_service(u) for u in cfg["services"]],
        "git": read_git(repo, cfg["git_remote"], cfg["git_branch"]),
        "sync": read_sync(cfg["sync_status_url"]),
    }


# ------------------------------------------------------------ actions


class ActionError(Exception):
    """A refusal the user should see, not a crash."""


#: How far back `logs` may reach. 0 is this boot, -1 the previous one.
#: After an unexplained reboot or a power cut the interesting log is the
#: one from BEFORE, which is otherwise an SSH job. Bounded so the
#: argument can never be an arbitrary string on a journalctl command
#: line, and shallow because the Pi keeps few boots.
MAX_BOOTS_BACK = 5


def action_logs(cfg: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    unit = body.get("unit")

    if unit not in cfg["services"]:
        raise ActionError(f"unknown unit {unit!r}")

    boot = body.get("boot", 0)

    if not isinstance(boot, int) or isinstance(boot, bool):
        raise ActionError("boot must be an integer")

    if not -MAX_BOOTS_BACK <= boot <= 0:
        raise ActionError(
            f"boot must be between -{MAX_BOOTS_BACK} and 0"
        )

    lines = int(cfg["log_lines"])
    code, out = run([
        "journalctl", "-u", unit, "-n", str(lines), "-b", str(boot),
        "--no-pager", "--output=short-iso",
    ], timeout=20.0)

    #: A boot that far back may simply not exist; that is an answer, not
    #: a failure.
    if code != 0 and "Data from the specified boot" in out:
        return {"unit": unit, "boot": boot, "ok": True,
                "lines": f"(no journal kept for boot {boot})"}

    return {"unit": unit, "boot": boot, "lines": out, "ok": code == 0}


def action_sync(cfg: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pause or resume the sync agent.

    live.py deliberately does not proxy these - its dashboard can be
    handed out as a public share link. This panel is authenticated and
    LAN-only, so it is where they belong.

    There is no "flush now": the agent already polls every few seconds
    once it is caught up, so forcing one would save a handful of seconds
    and add an endpoint for nothing.
    """
    import urllib.request

    verb = body.get("verb")

    if verb not in ("pause", "resume"):
        raise ActionError(f"unknown verb {verb!r}")

    url = cfg["sync_control_url"].rstrip("/") + f"/sync/{verb}"

    try:
        request = urllib.request.Request(url, data=b"", method="POST")

        with urllib.request.urlopen(request, timeout=5.0) as response:
            response.read()
    except Exception as exc:
        raise ActionError(f"sync agent did not answer: {exc}")

    return {"sync": verb}


def action_claude(cfg: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Start, stop or restart the agent's session.

    A systemd USER service, so no sudo and no allowlist entry: the panel
    already runs as that user. Restart is the useful one - it is the fix
    for a crash-loop once the underlying cause (usually re-authenticating)
    has been dealt with over SSH.

    There is deliberately no way to send the agent a prompt from here.
    That is an interactive shell behind an HTTP form, on the box holding
    the WireGuard key and the car link.
    """
    if not cfg.get("claude_enabled", True):
        raise ActionError("the Claude session is not enabled on this host")

    verb = body.get("verb")

    if verb not in ("start", "stop", "restart"):
        raise ActionError(f"unknown verb {verb!r}")

    code, out = run(
        ["systemctl", "--user", verb, cfg["claude_unit"]], timeout=45.0
    )

    if code != 0:
        raise ActionError(out or f"{verb} {cfg['claude_unit']} failed")

    return {"claude": verb, "unit": cfg["claude_unit"]}


def action_delete_session(cfg: Dict[str, Any],
                          body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Delete one per-drive database.

    The only action that removes data, so it is the most carefully
    fenced:

      * the name must be a bare filename - resolved and checked to be
        inside the sessions directory, so no path can escape it;
      * the database the runtime is currently writing is never a target;
      * and it must be fully shipped to the lake, unless the caller
        explicitly says otherwise. A drive that exists nowhere else is
        not something to lose to a mis-tap in a car park.
    """
    name = body.get("name")

    if not isinstance(name, str) or not name:
        raise ActionError("name is required")

    #: Reject anything that is not a plain filename BEFORE touching the
    #: filesystem: no separators, no traversal, no absolute paths.
    if name != os.path.basename(name) or name in (".", ".."):
        raise ActionError(f"invalid name {name!r}")

    if not name.endswith(".db"):
        raise ActionError("only .db session files can be deleted")

    sessions_dir = os.path.realpath(cfg["sessions_dir"])
    path = os.path.realpath(os.path.join(sessions_dir, name))

    #: Belt and braces: even a symlink inside the directory must not
    #: resolve to somewhere else.
    if os.path.dirname(path) != sessions_dir:
        raise ActionError(f"{name!r} is outside the sessions directory")

    if not os.path.isfile(path):
        raise ActionError(f"{name!r} does not exist")

    #: Both sides resolved: `path` is a realpath, so comparing it to a
    #: raw join silently never matches wherever the sessions directory
    #: sits behind a symlink (/var -> /private/var on macOS, and any
    #: bind-mounted or linked data directory on the Pi). That would
    #: leave the live database deletable.
    active = newest_session(cfg["sessions_dir"])

    if active and os.path.realpath(active) == path:
        raise ActionError(
            f"{name!r} is the newest database - the runtime is probably "
            f"writing it. Stop f10-dashboard first if you really mean to."
        )

    entry = next(
        (r for r in read_session_files(cfg) if r["name"] == name), None
    )

    if not body.get("force") and (entry is None or entry["synced"] is not True):
        raise ActionError(
            f"{name!r} is not confirmed synced to the lake - refusing. "
            f"Deleting it would lose that drive entirely."
        )

    freed = 0

    for suffix in ("", "-wal", "-shm"):
        target = path + suffix

        try:
            freed += os.path.getsize(target)
            os.remove(target)
        except OSError:
            pass

    return {"deleted": name, "freed_mb": round(freed / 1e6, 1)}


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
    commits = 0

    if before != after:
        count = first_line([
            "git", "-C", repo, "rev-list", "--count", f"{before}..{after}",
        ])
        commits = int(count) if count.isdigit() else 0

    return {
        "before": before,
        "after": after,
        "changed": before != after,
        "commits": commits,
        #: The subject of whatever is now checked out. "Already up to
        #: date" is a useless answer on its own - up to date AT WHAT?
        "subject": first_line(["git", "-C", repo, "log", "-1", "--pretty=%s"]),
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
    "sync": action_sync,
    "claude": action_claude,
    "delete_session": action_delete_session,
    "pull": action_pull,
    "reboot": action_reboot,
    "shutdown": action_shutdown,
}

#: Actions that interrupt a drive or run new code. The page asks twice
#: for these; the server records that they were confirmed.
DESTRUCTIVE = frozenset({
    "reboot", "shutdown", "pull", "restart", "service", "sync",
    "claude", "delete_session",
})


# ------------------------------------------------------------- server


def make_handler(cfg: Dict[str, Any]):
    expected_user = cfg["username"]
    expected_pass = cfg["password"]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "f10-admin"
        #: No interpreter version on the wire - the share prefix is
        #: public through the VPS. (The base class would still
        #: append a space after the name with sys_version empty.)
        sys_version = ""

        def version_string(self) -> str:
            return self.server_version

        def log_message(self, *args):
            pass

        # -- plumbing -----------------------------------------------

        def _send(self, code: int, ctype: str, payload: bytes,
                  extra: Optional[Dict[str, Optional[str]]] = None) -> None:
            headers: Dict[str, Optional[str]] = {
                "Content-Type": ctype,
                "Content-Length": str(len(payload)),
                "Cache-Control": "no-store",
                #: Nothing here should be sniffed, and nothing framed
                #: except the telemetry document, by this page only.
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": PANEL_CSP,
            }

            #: A caller's header replaces the default of the same name
            #: rather than being sent beside it; None drops one.
            for key, value in (extra or {}).items():
                headers[key] = value

            self.send_response(code)

            for key, value in headers.items():
                if value is not None:
                    self.send_header(key, value)

            self.end_headers()
            self.wfile.write(payload)

        def _json(self, code: int, payload: Dict[str, Any],
                  extra: Optional[Dict[str, Optional[str]]] = None) -> None:
            self._send(code, "application/json",
                       json.dumps(payload).encode("utf-8"), extra)

        # -- the telemetry UI and the runtime behind it -------------

        def _telemetry_file(self, path: str) -> None:
            """
            One of dashboard/'s three files, read from disk on every
            request - so a `pull` that changes the UI shows without a
            panel restart, and a checkout that lacks it says so instead
            of answering a stale copy.
            """
            name, ctype = TELEMETRY_FILES[path]
            full = os.path.join(DASHBOARD_DIR, name)

            try:
                with open(full, "rb") as fh:
                    payload = fh.read()
            except OSError as exc:
                self._send(500, "text/plain; charset=utf-8",
                           f"telemetry UI file missing: {full} "
                           f"({exc.strerror})\n".encode("utf-8"))
                return

            extra: Dict[str, Optional[str]] = {}

            if name == "index.html":
                #: Framed by the panel page, and only by it.
                extra = {"X-Frame-Options": "SAMEORIGIN",
                         "Content-Security-Policy": TELEMETRY_CSP}

            self._send(200, ctype, payload, extra)

        def _runtime_down(self, exc: BaseException) -> None:
            """
            live.py is not answering.

            503, with a body every consumer here understands: the panel
            page shows its banner and reloads the frame once the status
            poll sees the runtime back (that reload, not the browser's
            EventSource retry, is what resyncs the views), the Car link
            tab reads `ready` and `detail`, and a curl gets a sentence.

            The phone that asked may itself be gone by now - a wedged
            runtime is found out at the read deadline, long after a
            browser gives up - so the write is guarded: no traceback in
            the journal for every refresh that was abandoned.
            """
            why = (exc.strerror if isinstance(exc, OSError) and exc.strerror
                   else str(exc) or type(exc).__name__)
            retry = {"Retry-After": "5"}

            try:
                path = urlsplit(self.path).path

                if under_share(path) or path in ODOMETER_PATHS:
                    #: The public prefix, and the odometer paths that
                    #: are open at this hop too: a viewer who was
                    #: handed a link learns that the car is not
                    #: answering, and nothing about the box - not the
                    #: upstream, not its address, not the errno. A
                    #: browser under /s/ gets a page; anything else the
                    #: same shape as the owner body, minus the
                    #: internals.
                    if under_share(path) and "text/html" in (
                            self.headers.get("Accept") or ""):
                        self._send(503, "text/html; charset=utf-8",
                                   SHARE_DOWN_HTML.encode("utf-8"), retry)
                    else:
                        self._json(503, {
                            "error": "runtime not running",
                            "ready": False,
                            "detail": "the car's telemetry is not running",
                        }, retry)
                    return

                self._json(503, {
                    "error": "runtime not running",
                    "ready": False,
                    "detail": "the runtime is not answering on "
                              f"{cfg['dashboard_url']} ({why})",
                    "upstream": cfg["dashboard_url"],
                }, retry)
            except OSError:
                self.close_connection = True

        def _forwarded(self, name: str) -> str:
            """A non-blank X-Forwarded-* value the client sent, or ""."""
            return (self.headers.get(name) or "").strip()

        def _proxy(self, method: str) -> None:
            """
            Forward this request to live.py and relay the answer as it
            arrives.

            Byte-for-byte and chunk-by-chunk: an SSE stream reaches the
            phone as each event reaches the panel, through an unbuffered
            socket writer, with NO read deadline on the upstream socket
            once connected - a stream is silent between events, and a
            timeout there would end a drive's dashboard the first time
            the car went quiet. Everything else gets READ_TIMEOUT_S, so
            a runtime that accepts and never answers costs a thread for
            seconds, not for ever. The upstream connection is closed as
            soon as either side goes away. The panel's own credentials
            stop here: live.py has no use for them, and a request log
            on the wrong side of a share link must not carry them.
            """
            target = urlsplit(cfg["dashboard_url"])
            host = target.hostname or "127.0.0.1"
            port = target.port or 80
            body = b""
            path = urlsplit(self.path).path
            is_stream = path in STREAM_PATHS
            #: The bearer token for live.py rides through on the
            #: odometer paths only; everywhere else the header is the
            #: panel's own login and stops here.
            forward_auth = path in ODOMETER_PATHS
            peer = self.client_address[0]
            #: Through the same parser as `bind`: the environment
            #: override is one string, and a substring test on it would
            #: let "127.0.0.10,10.77.0.1" trust 127.0.0.1.
            trusted = peer in listen_addresses(cfg.get("trusted_proxies")
                                               or [])

            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0

                if length > MAX_PROXY_BODY:
                    self._json(413, {"error": "body too large"})
                    return

                body = self.rfile.read(length) if length > 0 else b""

            conn = http.client.HTTPConnection(host, port,
                                              timeout=CONNECT_TIMEOUT_S)

            try:
                conn.connect()
                #: Connected. From here a stream's socket waits as long
                #: as the stream is quiet; any other answer is due.
                conn.sock.settimeout(None if is_stream else READ_TIMEOUT_S)
                conn.putrequest(method, self.path, skip_host=True,
                                skip_accept_encoding=True)

                for name, value in self.headers.items():
                    lower = name.lower()

                    if lower == "authorization" and forward_auth:
                        conn.putheader(name, value)
                        continue

                    if lower in DROPPED_REQUEST_HEADERS:
                        continue

                    if lower in FORWARDED_HEADERS and (
                            not trusted or not value.strip()):
                        #: The client's claim about itself: replaced
                        #: below with what this panel knows. An EMPTY
                        #: value from a trusted hop is absent, not a
                        #: first value for the panel's own to follow.
                        continue

                    conn.putheader(name, value)

                conn.putheader("Host", self.headers.get("Host")
                               or f"{host}:{port}")

                if not trusted or not self._forwarded("X-Forwarded-For"):
                    conn.putheader("X-Forwarded-For", peer)

                if not trusted or not self._forwarded("X-Forwarded-Proto"):
                    conn.putheader("X-Forwarded-Proto", "http")

                conn.putheader("Connection", "close")

                if method == "POST":
                    conn.putheader("Content-Length", str(len(body)))

                conn.endheaders(body if method == "POST" else None)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                self._runtime_down(exc)
                return

            try:
                self.send_response(resp.status, resp.reason)
                #: A body of known length keeps the phone's connection;
                #: a stream ends with the connection, on both sides.
                sized = (resp.getheader("Content-Length") is not None
                         or resp.status in (204, 304))

                for name, value in resp.getheaders():
                    if name.lower() not in DROPPED_RESPONSE_HEADERS:
                        self.send_header(name, value)

                self.send_header("X-Content-Type-Options", "nosniff")

                if not sized:
                    self.send_header("Connection", "close")
                    self.close_connection = True

                self.end_headers()

                while True:
                    #: read1 returns as soon as ANY bytes arrive - one
                    #: event at a time, never "wait for 64 KiB".
                    chunk = resp.read1(65536)

                    if not chunk:
                        break

                    self.wfile.write(chunk)
            except OSError:
                #: The phone went away, or live.py did mid-stream. Either
                #: way this request is over; closing upstream tells the
                #: other side.
                self.close_connection = True
            finally:
                conn.close()

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

            #
            # The share surface, before the login: a share link works
            # exactly as it does on :8080, the token being the
            # credential and live.py the judge of it. No panel route is
            # dispatched for a path under the prefix, so nothing this
            # panel does can be reached through it whatever live.py
            # answers.
            #
            if under_share(path):
                self._proxy("GET")
                return

            #
            # The odometer API, likewise before the login: live.py's
            # bearer token is the credential and live.py judges it (a
            # request without one gets ITS 401, with the Bearer
            # challenge, not the panel's Basic one). Exact paths.
            #
            if path in ODOMETER_PATHS:
                self._proxy("GET")
                return

            if not self._authed():
                self._unauthorized()
                return

            if path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
                return

            if path in TELEMETRY_FILES:
                self._telemetry_file(path)
                return

            if path in PROXY_GET:
                self._proxy("GET")
                return

            if path == "/api/status":
                self._json(200, status(cfg))
                return

            self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def do_POST(self):
            path = self.path.split("?")[0]

            if under_share(path):
                #: live.py refuses every POST under the prefix itself.
                #: Forwarded rather than answered here so that stays its
                #: decision, and its answer.
                self._proxy("POST")
                return

            if not self._authed():
                self._unauthorized()
                return

            if path in PROXY_POST:
                #
                # These change how the car is polled or mint a public
                # link, and the browser attaches this panel's cached
                # credentials to any POST here - including one a page
                # from elsewhere triggers. The telemetry page always
                # sends JSON; a cross-origin form cannot, and a fetch
                # that does needs a preflight this server never answers.
                #
                ctype = self.headers.get("Content-Type", "")

                if ctype.split(";")[0].strip().lower() != "application/json":
                    self._json(403, {"error": "JSON body required"})
                    return

                self._proxy("POST")
                return

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


def listen_addresses(bind: Any) -> List[str]:
    """
    `bind` in every form a config may carry it: one address (the
    original form, still valid), a list of them, or a comma-separated
    string - which is what the F10_ADMIN_BIND environment override is,
    since an environment variable is always a string.
    """
    if isinstance(bind, (list, tuple)):
        parts = [str(item) for item in bind]
    else:
        parts = str(bind).split(",")

    return [part.strip() for part in parts if part and part.strip()]


class Listener(ThreadingHTTPServer):
    """One listening socket; IPv6 when the address is."""

    daemon_threads = True

    def __init__(self, address: Tuple[str, int], handler) -> None:
        self.address_family = (socket.AF_INET6 if ":" in address[0]
                               else socket.AF_INET)
        super().__init__(address, handler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"


def bind_all(addresses: List[str], port: int, handler
             ) -> Tuple[List[Listener], List[Tuple[str, OSError]]]:
    """Bind every address; report the ones that could not be, by name."""
    bound: List[Listener] = []
    failed: List[Tuple[str, OSError]] = []

    for address in addresses:
        try:
            bound.append(Listener((address, port), handler))
        except OSError as exc:
            failed.append((address, exc))

    return bound, failed


def serve(servers: List[Listener], pending: List[str], port: int, handler,
          retry_s: float = BIND_RETRY_S,
          stop: Optional[threading.Event] = None) -> int:
    """
    Run every listener in its own thread until interrupted (or `stop`
    is set - that is for tests; systemd sends SIGTERM).

    An address that could not be bound at start is retried in the
    background: the WireGuard interface comes up after this panel does,
    and a Pi that reboots out of range would otherwise never listen on
    it. The LAN listener is not held hostage to that. `servers` and
    `pending` are updated in place as addresses come up.
    """
    stop = stop if stop is not None else threading.Event()

    def start(server: Listener) -> None:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"[+] f10-admin on {server.url}", flush=True)

    for server in servers:
        start(server)

    try:
        while not stop.is_set():
            if not pending:
                stop.wait()
                break

            if stop.wait(retry_s):
                break

            still: List[str] = []

            for address in pending:
                try:
                    server = Listener((address, port), handler)
                except OSError:
                    still.append(address)
                    continue

                servers.append(server)
                start(server)

            pending[:] = still
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--config",
                    default=os.path.join(HERE, "config.json"),
                    help="JSON config (default: config.json beside this file)")
    ap.add_argument("--bind", default=None,
                    help="override the listen address(es), comma-separated")
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

    addresses = listen_addresses(cfg["bind"])

    if not addresses:
        print("[!] bind is empty - name the LAN address (see README).",
              file=sys.stderr)
        return 2

    for address in addresses:
        refusal = bind_refusal(address)

        if refusal:
            #
            # Refused, not warned. This panel can reboot the host and make
            # it execute new code; on a hotspot or a car-park AP, a
            # wildcard bind offers that to everyone on the segment. One
            # wildcard in a list is the same offer, in any spelling the
            # kernel accepts.
            #
            print(f"[!] refusing to bind {address!r}: {refusal} - name the "
                  "LAN address explicitly (see README).", file=sys.stderr)
            return 2

    port = int(cfg["port"])
    handler = make_handler(cfg)
    servers, failed = bind_all(addresses, port, handler)

    for address, exc in failed:
        print(f"[!] cannot listen on {address}:{port} - "
              f"{exc.strerror or exc}; retrying every {BIND_RETRY_S:.0f} s",
              file=sys.stderr, flush=True)

    if not servers:
        print("[!] none of the configured addresses could be bound.",
              file=sys.stderr)
        return 2

    print(f"[+] runtime:  {cfg['dashboard_url']} (proxied)", flush=True)
    print(f"[+] repo:     {cfg['repo_dir']}", flush=True)
    print(f"[+] services: {', '.join(cfg['services'])}", flush=True)

    return serve(servers, [address for address, _ in failed], port, handler)


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
  /* Fixed, not in flow. It used to sit in a div at the bottom of the
     page: on a phone every result landed below the fold, so an action
     that worked perfectly looked like it did nothing. */
  #msg { position:fixed; left:0; right:0; z-index:20;
         bottom:calc(12px + env(safe-area-inset-bottom));
         padding:0 16px; pointer-events:none; }
  .msg { max-width:640px; margin:0 auto; padding:12px 14px;
         border-radius:11px; font-size:13.5px; line-height:1.45;
         box-shadow:0 8px 28px rgba(0,0,0,.55); }
  .msg.ok  { background:#123a2a; color:#a8ecc8; border:1px solid #1d6b4c; }
  .msg.err { background:#3d1e1e; color:#ffc0c0; border:1px solid #7a3232; }
  .flags { margin-top:8px; font-size:12.5px; color:var(--warn); }
  .rec { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
  .rec .big { font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }
  .rec .big.on  { color:var(--good); }
  .rec .big.off { color:var(--bad); }
  .rec .unit { font-size:13px; color:var(--muted); }
  .recmeta { margin-top:9px; font-size:12.5px; color:var(--muted);
             display:flex; gap:6px 14px; flex-wrap:wrap;
             font-variant-numeric:tabular-nums; }
  .recmeta b { color:var(--text); font-weight:600; }
  .recwarn { margin-top:9px; font-size:13px; color:var(--bad); }
  .sess { display:flex; align-items:center; gap:10px; padding:10px 0;
          border-bottom:1px solid var(--line); }
  .sess:last-child { border-bottom:0; padding-bottom:0; }
  .sess:first-child { padding-top:0; }
  .sess .nm { flex:1; min-width:0; }
  .sess .nm b { display:block; font-size:12.5px; font-weight:600;
                font-family:ui-monospace,Menlo,monospace;
                overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .sess .nm span { display:block; font-size:11.5px; color:var(--muted); }
  .sess .tick { font-size:11px; padding:2px 7px; border-radius:99px;
                white-space:nowrap; }
  .tick.yes { background:#0f2f22; color:#8fe3bd; }
  .tick.no  { background:#3a2a12; color:#e8c489; }
  .tick.live{ background:#152b45; color:#9dc6f5; }
  .prevboot { display:inline-flex; align-items:center; gap:7px;
              margin-top:10px; font-size:12.5px; color:var(--muted); }
  .prevboot input { width:17px; height:17px; accent-color:var(--accent); }
  .syncrow { display:flex; gap:8px; margin-top:12px; }
  .syncrow > button { flex:1; }
  .tablewrap { overflow-x:auto; -webkit-overflow-scrolling:touch; }
  table { border-collapse:collapse; width:100%; font-size:12px;
          font-variant-numeric:tabular-nums; }
  th, td { text-align:left; padding:6px 9px; white-space:nowrap;
           border-bottom:1px solid var(--line); }
  th { font-size:10.5px; letter-spacing:.05em; text-transform:uppercase;
       color:var(--muted); font-weight:600; }
  td.k { font-family:ui-monospace,Menlo,monospace; }
  td.bad { color:var(--bad); } td.ok { color:var(--good); }
  td.warn { color:var(--warn); }
  tr.rowbad td.k { color:var(--bad); }
  tr.reqrow { cursor:pointer; }
  tr.reqrow td:first-child::before { content:"▸ "; color:var(--muted);
                                     font-size:10px; }
  tr.reqrow.open td:first-child::before { content:"▾ "; }
  tr.reqdetail { display:none; }
  tr.reqdetail.open { display:table-row; }
  tr.reqdetail td { white-space:normal; font-size:12px; line-height:1.6;
                    color:var(--muted); padding:8px 9px 12px 22px;
                    background:var(--card2); }
  tr.reqdetail b { color:var(--text); font-weight:600; }
  tr.reqdetail .stage { display:block; }
  tr.reqdetail .arrow { color:var(--accent); margin:0 4px; }
  .drop { padding:9px 0; border-bottom:1px solid var(--line); font-size:13px; }
  .drop:last-child { border-bottom:0; }
  .drop b { font-family:ui-monospace,Menlo,monospace; font-size:12.5px; }
  .drop span { display:block; color:var(--muted); font-size:12px; }
  .drop span.tick { display:inline-block; font-size:11px; padding:2px 7px;
                    border-radius:99px; margin-left:6px; white-space:nowrap; }
  .dropwhy { font-size:11px; text-transform:uppercase; letter-spacing:.05em;
             color:var(--muted); margin:14px 0 4px; font-weight:600; }
  .dropwhy:first-child { margin-top:0; }
  .badge { font-size:10.5px; padding:2px 6px; border-radius:99px;
           background:var(--card2); color:var(--muted); margin-left:6px; }
  .badge.extra { background:#2a2340; color:#c3b6f5; }
  .badge.rej { background:#3a1f1f; color:#ffb4b4; }
  .tabs { display:flex; gap:5px; margin:0 0 14px; overflow-x:auto; }
  .tab { flex:1 1 0; min-width:0; background:none; border:1px solid var(--line);
         color:var(--muted); font-size:12.5px; padding:8px 4px;
         white-space:nowrap; }
  .tab.on { background:var(--card2); color:var(--text);
            border-color:var(--accent); }
  /* A thin gap between the car's tabs and the box's. */
  .tabs .gap { flex:0 0 1px; background:var(--line); margin:7px 1px; }
  /* The telemetry views: the dashboard/ page itself, framed. The frame
     takes the rest of the viewport and scrolls inside (a phone's
     browser sizes a frame to its content otherwise); the panel's own
     title goes away so there is one header on screen - the car's. */
  body.tele .head h1, body.tele .head .sub { display:none; }
  body.tele .head { padding-bottom:0; }
  #pane-tele .frame { overflow:auto; -webkit-overflow-scrolling:touch;
                      background:#000; }
  #pane-tele iframe { display:block; width:100%; height:100%; border:0;
                      background:#000; }
  .telewarn { max-width:640px; margin:0 auto 10px; padding:10px 14px;
              border-radius:11px; background:#3a2a12; color:#e8c489;
              border:1px solid #7a5a1c; font-size:13.5px; line-height:1.45; }
  .telewarn a { color:inherit; font-weight:600; }
  .hint { font-size:12px; color:var(--muted); line-height:1.5;
          margin:12px 0 0; }
  .cl { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap;
        margin-bottom:10px; }
  .cl .big { font-size:22px; font-weight:700; }
  .cl .big.on { color:var(--good); }
  .cl .big.off { color:var(--bad); }
  .cl .big.warn { color:var(--warn); }
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
<div class="wrap head">
  <h1 id="host">F10 Pi</h1>
  <p class="sub" id="sub">connecting…</p>

  <!-- Six tabs, one page. The first three are the telemetry UI - the
       dashboard/ files live.py serves, framed unchanged; its own mode
       switch is hidden because these tabs are it. The agent is a
       separate concern from the car: it does not matter during a drive,
       and it is optional, so its tab hides itself where the unit is not
       installed. The active tab is the URL fragment, so a bookmark or
       the back button lands on a tab, not on "whatever was last". -->
  <nav class="tabs" id="tabs">
    <button class="tab" data-tab="drive">Drive</button>
    <button class="tab" data-tab="detail">Detail</button>
    <button class="tab" data-tab="table">All data</button>
    <span class="gap"></span>
    <button class="tab on" data-tab="system">System</button>
    <button class="tab" data-tab="car">Car link</button>
    <button class="tab" data-tab="claude" id="tab-claude" style="display:none">Claude</button>
  </nav>
</div>

<!-- Full width, outside .wrap: the Drive cluster is laid out for the
     whole screen, and the frame's page brings its own header. -->
<div id="pane-tele" style="display:none">
  <div class="telewarn" id="telewarn" style="display:none">
    <b>runtime not running</b> — live.py is not answering, so there is
    nothing to show. The views come back on their own when it does;
    the <a href="#system">System</a> tab can start it.
  </div>
  <div class="frame" id="teleframe">
    <iframe id="tele" title="telemetry"></iframe>
  </div>
</div>

<div class="wrap" id="mgmt">

  <div id="pane-system">

  <!-- First card on purpose: "is it recording?" is the question you
       actually open this page to answer. -->
  <div class="card" id="reccard">
    <h2>Recording</h2>
    <div id="recording"></div>
  </div>

  <div class="card">
    <h2>Health</h2>
    <div class="grid" id="health"></div>
    <div class="flags" id="flags"></div>
    <div class="syncrow" id="syncrow"></div>
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
    <h2>Drive files</h2>
    <div id="sessions"></div>
  </div>

  <div class="card">
    <h2>Logs</h2>
    <div class="row" id="logbuttons"></div>
    <label class="prevboot">
      <input type="checkbox" id="prevboot"> previous boot
    </label>
    <pre id="logs" style="display:none"></pre>
  </div>

  </div><!-- /pane-system -->

  <!-- The verification view: what this session decided to ask the car,
       what answered, and what resolution threw away. -->
  <div id="pane-car" style="display:none">
    <div class="card">
      <h2>This session</h2>
      <div id="carsession"></div>
    </div>

    <!-- Known from disk: true with the car absent, and the answer to
         "did --extra-mappings work?" before a drive rather than after. -->
    <div class="card">
      <h2>Loaded from disk</h2>
      <div id="carloaded"></div>
    </div>

    <!-- Two claims, never merged: what the ECU answered (a profile,
         proven by its own nominated read) and what it IS (an SGBD,
         proven only by identity evidence - which this car's DDE does
         not give, so it reads "unknown"). -->
    <div class="card" id="cardidentity">
      <h2>What the ECU proved</h2>
      <p class="hint" style="margin-top:0">
        A profile is <b>compatible</b> when one of the reads a mapping
        nominates answers in the declared shape. That activates the
        mapping; it does not say which SGBD revision the ECU is — that
        is the separate <b>exact SGBD</b> line, and “unknown” there is
        the honest answer until an ident read succeeds.
      </p>
      <div id="caridentity"></div>
    </div>

    <div class="card" id="cardactive">
      <h2>Active on this ECU</h2>
      <div id="carmappings"></div>
    </div>

    <div class="card">
      <h2>Requests</h2>
      <p class="hint" style="margin-top:0">
        Failing first. <b>asked</b> is how many times this session put
        the request on the wire — not how often it was scheduled (a
        resting or retired request is scheduled and skipped), and not a
        frame count (six OBD PIDs go in one frame). A request that was
        asked but never <b>ok</b> is a channel the car is not answering,
        which is otherwise indistinguishable from one nobody asked for;
        a <b>retired</b> one is no longer asked at all. <b>every</b> is
        the declared period → the measured one. Tap a row for the whole
        pipeline: scheduled → submitted → frames → outcome → decoded →
        accepted → stored.
      </p>
      <div class="tablewrap"><table id="carrequests"></table></div>
    </div>

    <div class="card">
      <h2>Not being read</h2>
      <p class="hint" style="margin-top:0">
        Resolution filters silently by design — a mapping for another ECU
        variant is skipped, not an error. This is that decision, written
        down: the answer to “why is this channel missing?”
      </p>
      <div id="cardropped"></div>
    </div>

    <div class="card">
      <h2>Channels</h2>
      <div class="tablewrap"><table id="carchannels"></table></div>
    </div>
  </div>

  <div id="pane-claude" style="display:none">
    <div class="card">
      <h2>Coding agent</h2>
      <div id="claude"></div>
      <div class="row" style="margin-top:12px">
        <button data-claude="restart">Restart session</button>
        <button data-claude="stop" class="danger">Stop</button>
      </div>
      <p class="hint">
        Status and lifecycle only. There is deliberately no terminal and no
        prompt box here — that would be an interactive shell behind a web
        form, on the box holding the WireGuard key and the link to the car.
        Attach over SSH, or drive it from the Claude app by its Remote
        Control name.
      </p>
    </div>

    <div class="card">
      <h2>Session output</h2>
      <p class="hint" style="margin-top:0">
        The last lines of the tmux pane. This is the only place a login
        prompt or a crash-loop error appears — the journal stays empty.
      </p>
      <pre id="claudepane" style="display:none"></pre>
    </div>
  </div>

  <!-- Power sits outside the management panes: rebooting or halting
       the box is relevant whichever of them you are on. -->
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
</div><!-- /mgmt -->

<!-- Fixed to the viewport, so it is read on any tab. -->
<div id="msg"></div>

<script>
const $ = id => document.getElementById(id);
let armed = null, armedTimer = null;
/* Declared here, started at the bottom: refresh() and stopPolling() both
   close over it, and both are defined above where it is assigned. */
let poller = null;

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
  /* Newlines in the message are real line breaks - a pull result is
     three facts, not one sentence. */
  $("msg").innerHTML = `<div class="msg ${bad ? "err" : "ok"}">`
    + escape_(text).replace(/\n/g, "<br>") + "</div>";
  clearTimeout(say._t);
  say._t = setTimeout(() => { $("msg").innerHTML = ""; }, bad ? 14000 : 10000);
}

function busy(btn, label) {
  btn.dataset.was = btn.textContent;
  btn.textContent = label;
  btn.disabled = true;
}

function unbusy(btn) {
  if (btn.dataset.was) btn.textContent = btn.dataset.was;
  btn.disabled = false;
}

function compatibleProfiles(identity) {
  return ((identity && identity.profiles) || [])
    .filter(p => p.outcome === "compatible").map(p => p.profile);
}

/* One row per profile the loaded mappings require, with every probe
   that was sent and how it went - a refusal reads "negative_response
   (NRC 0x31 ...)", not "false". Then the identity line, kept apart. */
function identityHtml(identity) {
  const tick = o => o === "compatible" || o === "confirmed" ? "yes"
    : o === "unknown" || o === "ambiguous" ? "live" : "no";
  const profiles = (identity && identity.profiles) || [];
  const exact = (identity && identity.exact_sgbd) || {outcome: "unknown"};

  const rows = profiles.map(p => `
    <div class="drop"><b>${escape_(p.profile)}</b>
      <span class="tick ${tick(p.outcome)}">${escape_(p.outcome)}</span>
      ${(p.probes || []).map(q => `<span>${q.answered ? "✓" : "✗"}
        ${escape_(q.request)} — ${escape_(q.reason)}${q.detail
          ? " (" + escape_(q.detail) + ")" : ""}</span>`).join("")}
      ${p.note ? `<span>${escape_(p.note)}</span>` : ""}
      ${p.derived_from && p.derived_from.length
        ? `<span>rows derived from the ${escape_(p.derived_from.join(", "))}
           table — provenance, not identity</span>` : ""}
    </div>`);

  if (rows.length === 0)
    rows.push('<div class="drop"><span>no loaded mapping requires a '
      + 'profile — nothing was probed</span></div>');

  rows.push(`
    <div class="drop"><b>exact SGBD</b>
      <span class="tick ${tick(exact.outcome)}">${escape_(exact.outcome)}</span>
      <span>${escape_(exact.summary || "")}</span>
    </div>`);

  return rows.join("");
}

function escape_(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

/* The structured part of a request's last fault (`last_detail` from
   /api/diagnostics), rendered from its FIELDS: "NRC 0x31
   requestOutOfRange" from `nrc`/`nrc_name`, "to 0x18" from a NACK's
   `target`, "after 3x 0x78" from a timeout's `pending`. The message
   text beside it is prose and is never parsed. Null-safe: a session
   recorded before the detail existed, or a fault without fields,
   renders nothing here. */
function faultText(detail) {
  if (!detail || typeof detail !== "object") return "";
  const hex = n => "0x" + Number(n).toString(16).toUpperCase().padStart(2, "0");
  if (detail.nrc != null) {
    let text = `NRC ${hex(detail.nrc)} ${detail.nrc_name || "unknown"}`;
    if (detail.service != null) text += ` (to ${hex(detail.service)})`;
    return text;
  }
  if (detail.target != null) return `no route to ${hex(detail.target)}`;
  if (detail.pending) return `after ${detail.pending}x 0x78`;
  return "";
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

  /* Pause/resume live here rather than on live.py's dashboard, which
     can be handed out as a public share link. */
  $("syncrow").innerHTML = !sync.reachable ? ""
    : sync.enabled
      ? '<button data-sync="pause">Pause sync</button>'
      : '<button data-sync="resume">Resume sync</button>';

  renderClaude(s.claude);
  renderRecording(s.recording || {}, s.services || []);
  renderSessions(s.sessions || []);

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

/* ---------------------------------------------------- car link tab */
/* Known from disk, with or without a car: which files loaded, which came
   from --extra-mappings, and the rates they declare. */
function renderLoaded(loaded, connected) {
  const maps = loaded.mappings || [];

  if (!maps.length) {
    $("carloaded").innerHTML =
      '<span class="sub">no mappings loaded</span>';
    return;
  }

  const extra = maps.filter(m => m.extra).length;

  $("carloaded").innerHTML =
    `<div class="recmeta" style="margin-bottom:12px">`
    + [`<b>${maps.length}</b> file${maps.length === 1 ? "" : "s"}`,
       `<b>${extra}</b> via --extra-mappings`,
       `<b>${loaded.channels || 0}</b> channels declared`,
      ].map(x => `<span>${x}</span>`).join("")
    + `</div>`
    + maps.map(m => `
      <div class="sess">
        <span class="nm"><b>${escape_(m.id)}</b>
          <span>v${m.version} · ${m.requests} request${m.requests === 1 ? "" : "s"}
            · ${m.signals} channel${m.signals === 1 ? "" : "s"}
            · ${escape_(m.ecu_family)} ${escape_(m.ecu_target)}</span></span>
        ${m.extra ? '<span class="badge extra">--extra</span>' : ""}
        ${m.verification === "verified" ? '<span class="tick yes">verified</span>'
          : `<span class="badge rej">${escape_(m.verification)}</span>`}
      </div>`).join("")
    + `<table style="margin-top:14px"><thead><tr>
         <th>class</th><th>every</th><th>requests</th></tr></thead><tbody>`
    + (loaded.classes || []).map(c => `<tr>
         <td class="k">${escape_(c.name)}</td>
         <td>${c.period_s < 1 ? c.period_s.toFixed(1) : Math.round(c.period_s)}s
             ${c.stagger ? " each" : ""}</td>
         <td>${c.requests}</td></tr>`).join("")
    + `</tbody></table>`;
}

/* Fetched only while its tab is open: a much bigger payload than the
   status poll, and nothing in it changes second to second. */
let carLoaded = false;
/* Request rows whose pipeline detail is open, by request id. */
const openRequests = new Set();

async function loadCar(force) {
  if (carLoaded && !force) return;

  try {
    /* Through the proxy: a runtime that is down is a 503 whose body
       still carries ready:false and a detail line, which is exactly
       the "needs the car" rendering below - not an error. */
    const r = await fetch("/api/diagnostics", {cache: "no-store"});
    const d = await r.json().catch(() => ({}));
    if (!r.ok && d.ready !== false) throw new Error(d.error || `HTTP ${r.status}`);
    renderCar(d);
    carLoaded = r.ok;
  } catch (e) {
    $("carsession").innerHTML =
      `<div class="recwarn">${escape_(e.message)}</div>`;
  }
}

function renderCar(d) {
  /* Split by what the car is needed for. Which mappings loaded and at
     what rates is settled at boot; only the session facts - which ECU
     answered, what capability filtering dropped, success rates - need a
     link. Blanking everything made the panel unable to answer "did my
     extra mappings load?", which is the question you have with the car
     off, before the drive rather than after it. */
  renderLoaded(d.loaded || {}, d.ready);

  if (!d.ready) {
    $("carsession").innerHTML =
      `<div class="recwarn">${escape_(d.detail || "not connected")}</div>`
      + `<p class="hint">The mapping set below is what this process would
         poll once the car answers. Which ECU responds, what gets filtered
         for this vehicle, and per-request success rates need the link.</p>`;
    $("cardropped").innerHTML =
      '<span class="sub">needs the car — filtering is decided against the '
      + 'ECU that answers</span>';
    $("carrequests").innerHTML = "";
    $("carchannels").innerHTML = "";
    //: "Active on this ECU" has no meaning without one.
    $("cardactive").style.display = "none";
    $("cardidentity").style.display = "none";
    return;
  }

  $("cardactive").style.display = "";
  $("cardidentity").style.display = "";

  const s = d.session || {}, t = d.totals || {};

  $("carsession").innerHTML =
    `<div class="grid">`
    + stat("Requests asked", (t.submitted == null ? t.sent || 0 : t.submitted).toLocaleString())
    + stat("Success", t.success_pct == null ? "—" : t.success_pct + "%",
           t.success_pct == null ? "" :
           t.success_pct > 98 ? "good" : t.success_pct > 90 ? "warn" : "bad")
    + stat("Channels", `${t.channels || 0}`)
    + stat("Polled", `${t.requests || 0} requests`, "", true)
    + `</div>`
    + `<div class="recmeta" style="margin-top:12px">`
    + [`ECU <b>${escape_(s.ecu || "?")}</b> at <b>${escape_(s.ecu_addr || "?")}</b>`,
       `compatible <b>${escape_(compatibleProfiles(s.identity).join(", ") || "none")}</b>`,
       `exact SGBD <b>${escape_((s.identity && s.identity.exact_sgbd
          && s.identity.exact_sgbd.outcome) || "unknown")}</b>`,
       `${s.supported_pids || 0} PIDs advertised`,
       `mode <b>${escape_(s.mode || "?")}</b>`,
       s.other_ecus && s.other_ecus.length
         ? `also on the bus: ${escape_(s.other_ecus.join(", "))}` : "",
       /* What the transport refused to hand to anyone. A request's own
          counters cannot show a discarded frame - no request received
          it - so the link-level tally lives here (issue #12). */
       /* The PHYSICAL frame count, once per frame. The per-request
          rows attribute a shared OBD batch to every member, so their
          frames add up to more than this whenever batching is on. */
       t.wire
         ? `wire: <b>${(t.wire.exchanges || 0).toLocaleString()}</b> exchanges · `
           + `<b>${(t.wire.tx_frames || 0).toLocaleString()}</b> tx · `
           + `<b>${(t.wire.rx_frames || 0).toLocaleString()}</b> rx`
           + (t.wire.setup_tx_frames || t.wire.setup_faults
               ? ` · setup <b>${t.wire.setup_tx_frames || 0}</b> tx / `
                 + `<b>${t.wire.setup_rx_frames || 0}</b> rx`
                 + (t.wire.setup_faults
                     ? ` (<b>${t.wire.setup_faults}</b> failed)` : "")
               : "")
           + (t.wire.obd_batches
               ? ` · ${t.wire.obd_batched_pids} PIDs in ${t.wire.obd_batches} batches`
               : "")
         : "",
       `scheduled <b>${(t.scheduled || 0).toLocaleString()}</b> · `
         + `asked <b>${(t.submitted || 0).toLocaleString()}</b>`
         + (t.skipped_resting ? ` · <b>${t.skipped_resting}</b> skipped resting` : "")
         + (t.skipped_retired ? ` · <b>${t.skipped_retired}</b> skipped retired` : ""),
       `signals: decoded <b>${(t.decoded_signals || 0).toLocaleString()}</b> · `
         + `accepted <b>${(t.accepted_signals || 0).toLocaleString()}</b> · `
         + (t.recorder
             ? `stored <b>${(t.persisted_signals || 0).toLocaleString()}</b>`
               + ` in ${(t.recorder.rows || 0).toLocaleString()} rows`
               + (t.recorder.dropped_cycles
                   ? ` · <b>${t.recorder.dropped_cycles}</b> cycles dropped` : "")
             : "not recording")
         + (t.all_rejected ? ` · <b>${t.all_rejected}</b> answers all rejected` : ""),
       d.transport
         ? `link: <b>${d.transport.timeouts || 0}</b> timeouts · `
           + `<b>${d.transport.late_response || 0}</b> late · `
           + `<b>${d.transport.unexpected_response || 0}</b> stray · `
           + `<b>${d.transport.pending_exhausted || 0}</b> pending exhausted`
           + (d.transport.ambiguous_resends
               ? ` · <b>${d.transport.ambiguous_resends}</b> ambiguous re-polls` : "")
           + (d.transport.ambiguous_answers
               ? ` · <b>${d.transport.ambiguous_answers}</b> flagged stale` : "")
           + (d.transport.outstanding && d.transport.outstanding.length
               ? ` · awaiting ${escape_(d.transport.outstanding
                   .map(o => o.label || o.expected).join(", "))}` : "")
         : "",
      ].filter(Boolean).map(x => `<span>${x}</span>`).join("")
    + `</div>`
    /* The full fingerprint: every versioned file that shaped this
       session, which is what a recorded drive is compared on. */
    + `<div class="hint" style="word-break:break-all">`
    + `mapping set: ${escape_(s.mapping_set || "")}</div>`;

  $("caridentity").innerHTML = identityHtml(s.identity);

  $("carmappings").innerHTML = (d.mappings || []).map(m => `
    <div class="sess">
      <span class="nm"><b>${escape_(m.id)}</b>
        <span>v${m.version} · ${m.requests} request${m.requests === 1 ? "" : "s"}
          · ${escape_(m.ecu_family)} ${escape_(m.ecu_target)}
          · ${escape_(m.source_type)}</span></span>
      ${m.extra ? '<span class="badge extra">--extra</span>' : ""}
      ${m.verification === "verified" ? '<span class="tick yes">verified</span>'
        : `<span class="badge rej">${escape_(m.verification)}</span>`}
    </div>`).join("");

  /* Failing first: that is the order you debug in. A retired request
     (the reader gave up on it) and one that was asked but never
     answered come first, then resting (stood down after repeated
     faults), then anything with failures, then the healthy rows. */
  const rank = q => q.state === "retired" ? 0
    : q.stages && q.stages.submitted && !q.ok ? 0
    : q.resting_for ? 1 : q.failed ? 2 : 3;
  const reqs = (d.requests || []).slice().sort((a, b) =>
    rank(a) - rank(b) || a.id.localeCompare(b.id));
  const secs = v => v == null ? "—" : v >= 100 ? Math.round(v) + "s"
    : v >= 10 ? v.toFixed(1) + "s" : v.toFixed(2) + "s";
  const ms = v => v == null ? "—" : Math.round(v) + " ms";
  const n = v => (v || 0).toLocaleString();

  /* One row per request; the pipeline behind it, one stage per line,
     so the summary stays a summary. Every number here is in the JSON
     (`stages`, `latency_ms`, `refresh_s`); the row only picks. */
  const detail = q => {
    /* An older live.py has no `stages` at all. Zeros would read as "a
       request that never went out"; say there is nothing to show. */
    if (!q.stages) return `<span class="stage">— (this live.py reports no stage counters; update it)</span>`;
    const st = q.stages, w = st.wire || {};
    const skipped = [];
    if (st.skipped_resting) skipped.push(`${st.skipped_resting} resting`);
    if (st.skipped_retired) skipped.push(`${st.skipped_retired} retired`);
    /* `decode_failed` is a subset of `positive` (a frame that fitted
       the request and the mapping could not read), not another
       outcome beside it - so it is shown inside the positive count. */
    const positive = st.positive_response
      ? `positive <b>${n(st.positive_response)}</b>`
        + (st.decode_failed ? ` (of which <b>${n(st.decode_failed)}</b> decode failed)` : "")
      : "";
    const outcomes = [
      ["negative (NRC)", st.negative_response],
      ["timeout", st.timeout], ["nack", st.nack],
      ["no answer in batch", st.no_response], ["late", st.late],
    ].filter(([, v]) => v).map(([k, v]) => `${k} <b>${n(v)}</b>`);
    if (positive) outcomes.unshift(positive);
    const setup = w.setup_tx_frames || w.setup_rx_frames || w.setup_faults
      ? ` (+ setup <b>${n(w.setup_tx_frames)}</b> tx / <b>${n(w.setup_rx_frames)}</b> rx`
        + (w.setup_faults ? `, <b>${w.setup_faults}</b> failed in setup` : "") + ")"
      : "";
    const lat = q.latency_ms;
    const ref = q.refresh_s;
    return `<span class="stage">scheduled <b>${n(st.scheduled)}</b>`
      + `<span class="arrow">→</span>submitted <b>${n(st.submitted)}</b>`
      + (skipped.length ? ` (skipped: ${skipped.join(", ")})` : "")
      + `<span class="arrow">→</span>wire <b>${n(w.exchanges)}</b> exchanges, `
      + `<b>${n(w.tx_frames)}</b> tx / <b>${n(w.rx_frames)}</b> rx frames${setup}</span>`
      + `<span class="stage">outcome: ${outcomes.length ? outcomes.join(" · ") : "nothing yet"}</span>`
      + `<span class="stage">signals: decoded <b>${n(st.decoded_signals)}</b>`
      + `<span class="arrow">→</span>accepted <b>${n(st.accepted_signals)}</b>`
      + `<span class="arrow">→</span>stored `
      + (st.persisted_signals == null ? "<b>—</b> (not recording)" : `<b>${n(st.persisted_signals)}</b>`)
      + (st.all_rejected
          ? ` · <span class="tick no">${st.all_rejected} answer${st.all_rejected === 1 ? "" : "s"} with every signal rejected`
            + (st.last_rejection ? ` (${escape_(st.last_rejection.join(", "))})` : "") + `</span>`
          : "")
      + `</span>`
      + `<span class="stage">latency: `
      + (lat ? `avg <b>${ms(lat.avg)}</b> · p95 <b>${ms(lat.p95)}</b> (last ${lat.window}) · last <b>${ms(lat.last)}</b>` : "—")
      + ` · last tx <b>${secs(q.last_tx_age)}</b> ago · last rx <b>${secs(q.last_rx_age)}</b> ago</span>`
      + `<span class="stage">refresh: declared <b>${secs(q.period_s)}</b> · measured `
      + (ref ? `median <b>${secs(ref.median)}</b> · avg <b>${secs(ref.avg)}</b> · max <b>${secs(ref.max)}</b> (last ${ref.window}) · last <b>${secs(ref.last)}</b> (over ${ref.n} refreshes)` : "<b>—</b>")
      + `</span>`;
  };

  $("carrequests").innerHTML =
    `<thead><tr><th>request</th><th>where</th><th>every</th>
      <th>asked</th><th>ok</th><th>fail</th><th>rate</th><th>state · last error</th>
      </tr></thead><tbody>`
    + reqs.map((q, i) => {
        const st = q.stages || {};
        const submitted = st.submitted == null ? q.sent : st.submitted;
        const dead = q.state === "retired" || (submitted && !q.ok);
        const rate = q.success_pct == null ? "—" : q.success_pct + "%";
        const cls = q.success_pct == null ? "" :
          q.success_pct > 98 ? "ok" : q.success_pct > 90 ? "warn" : "bad";
        const what = q.pid ? `${escape_(q.address)} pid ${escape_(q.pid)}`
          : q.did ? `${escape_(q.address)} did ${escape_(q.did)}`
          : escape_(q.address);
        /* "resting, ~5s after 3 x transport_nack" - an approximate
           state, deliberately not a countdown: the value is a snapshot
           taken when the tab loaded, and a ticking number that froze
           would read as a hung page. */
        const resting = q.resting_for
          ? `resting ~${Math.ceil(q.resting_for)}s after `
            + `${q.consecutive_faults} fault${q.consecutive_faults === 1 ? "" : "s"}`
          : "";
        const every = q.period_s == null ? "—"
          : q.refresh_s && q.refresh_s.median != null
            ? `${secs(q.period_s)} → ${secs(q.refresh_s.median)}`
            : secs(q.period_s);
        const state = q.state === "retired"
          ? `<span class="tick no">retired</span> `
          : resting ? `<span class="tick no">${escape_(resting)}</span> `
          : q.state === "idle" ? `<span class="sub">idle</span> ` : "";
        return `<tr class="reqrow ${dead ? "rowbad" : ""}" data-i="${i}">
          <td class="k">${escape_(q.id)}</td>
          <td>${what}</td>
          <td>${every}</td>
          <td>${n(submitted)}</td><td>${n(q.ok)}</td><td>${n(q.failed)}</td>
          <td class="${cls}">${rate}</td>
          <td>${state}${st.all_rejected ? `<span class="tick no">${st.all_rejected} all rejected</span> ` : ""}${q.late ? `<span class="tick no">${q.late} late</span> ` : ""}${q.ambiguous ? `<span class="tick no">${q.ambiguous} stale</span> ` : ""}${faultText(q.last_detail) ? `<span class="tick no">${escape_(faultText(q.last_detail))}</span> ` : ""}${escape_(q.last_error || "")}</td></tr>
          <tr class="reqdetail" data-i="${i}"><td colspan="8">${detail(q)}</td></tr>`;
      }).join("")
    + `</tbody>`;

  /* Tap a summary row to open its pipeline. Rows opened before a
     refresh stay open across it: the table is rebuilt whenever the tab
     is (re)opened - `loadCar(true)` on every switch to it, never on the
     system poll - and a detail that closed itself under your thumb
     would be unreadable. */
  for (const row of $("carrequests").querySelectorAll("tr.reqrow")) {
    const id = reqs[row.dataset.i].id;
    if (openRequests.has(id)) {
      row.classList.add("open");
      row.nextElementSibling.classList.add("open");
    }
    row.onclick = () => {
      const open = row.classList.toggle("open");
      row.nextElementSibling.classList.toggle("open", open);
      if (open) openRequests.add(id); else openRequests.delete(id);
    };
  }

  const why = {
    ecu_mismatch: "Mapping files for another ECU or variant",
    capability: "Requests this ECU does not advertise",
    inputs: "Derived channels missing an input",
    family: "Filtered by ECU family",
  };
  const groups = {};
  for (const x of d.dropped || []) (groups[x.reason] ||= []).push(x);

  $("cardropped").innerHTML = Object.keys(groups).length === 0
    ? '<span class="sub">nothing was filtered — every mapping applies</span>'
    : Object.entries(groups).map(([reason, items]) =>
        `<div class="dropwhy">${escape_(why[reason] || reason)}
           (${items.length})</div>`
        + items.map(x => `<div class="drop"><b>${escape_(x.id)}</b>
            <span>${escape_(x.detail)}</span></div>`).join("")
      ).join("");

  /* Quality is signal-level and says something the request counters
     cannot: a channel can be answering every request and still be
     returning nothing but sentinels. Rendered as the labels themselves
     ("sentinel 76") rather than a bare percentage, because WHICH way a
     reading is unusable is the part that tells you what to do about it. */
  const qcell = c => {
    if (!c.quality || Object.keys(c.quality).length === 0) return "—";
    const bad = Object.entries(c.quality).filter(([q]) => q !== "ok");
    if (bad.length === 0) return '<span class="sub">ok</span>';
    return bad.map(([q, n]) => `${escape_(q)} ${n}`).join(", ");
  };

  /* `rows` is what reached SQLite for this channel, this process - the
     last stage, distinct from "logged" (whether it is meant to be). A
     dash means nothing is recording. `every` is the measured refresh
     of the request that carries the channel; derived channels have no
     exchange of their own. */
  $("carchannels").innerHTML =
    `<thead><tr><th>channel</th><th>unit</th><th>from</th>
      <th>v</th><th>every</th><th>stored</th><th>rows</th><th>quality</th><th>value</th></tr></thead><tbody>`
    + (d.channels || []).map(c => `<tr class="${c.state === "retired" ? "rowbad" : ""}">
        <td class="k">${escape_(c.key)}</td>
        <td>${escape_(c.unit || "")}</td>
        <td>${escape_(c.derived ? "derived" : c.request)}${c.state === "retired" ? ' <span class="tick no">retired</span>' : ""}</td>
        <td>${c.version == null ? "—" : c.version}</td>
        <td>${c.refresh_s && c.refresh_s.median != null ? secs(c.refresh_s.median) : "—"}</td>
        <td class="${c.logged ? "" : "warn"}">${c.logged ? "yes" : "no"}</td>
        <td>${c.persisted == null ? "—" : n(c.persisted)}</td>
        <td class="${c.flagged ? "warn" : ""}">${qcell(c)}</td>
        <td>${c.value == null ? "—" : escape_(String(c.value))}</td>
        </tr>`).join("")
    + `</tbody>`;
}

/* The agent is optional. Where the unit is not installed the server
   sends null and the tab never appears. */
function renderClaude(c) {
  if (!c) {
    $("tab-claude").style.display = "none";
    return;
  }

  $("tab-claude").style.display = "";

  /* Three states, not two. "Active" alone is a lie when the systemd
     unit's while-loop is alive but the agent inside it is restarting
     every five seconds - usually lost authentication. */
  const head = c.crash_looping
    ? `<span class="big warn">crash-looping</span>`
      + `<span class="unit">the loop is up, the agent is not — `
      + `check the pane below, it is usually authentication</span>`
    : c.active && c.agent_running
      ? `<span class="big on">running</span>`
        + `<span class="unit">remote control: <b>${escape_(c.remote_name)}</b></span>`
      : `<span class="big off">stopped</span>`
        + `<span class="unit">${escape_(c.state)}</span>`;

  const bits = [`unit <b>${escape_(c.unit)}</b>`,
                `tmux <b>${c.tmux_alive ? escape_(c.tmux_session) : "no session"}</b>`];
  if (c.since) bits.push(`since <b>${escape_(c.since.slice(0, 16))}</b>`);

  $("claude").innerHTML =
    `<div class="cl">${head}</div>`
    + `<div class="recmeta">`
    + bits.map(x => `<span>${x}</span>`).join("") + `</div>`;

  const pre = $("claudepane");
  /* textContent, never innerHTML: this is whatever the agent printed,
     and it is not markup we control. */
  pre.textContent = c.pane || "(no output captured)";
  pre.style.display = "block";
}

/* "Is the service up?" and "is data landing?" are different questions.
   A process can be perfectly healthy with the ENET cable out. */
function renderRecording(r, services) {
  const n = r.samples;
  const live = n > 0;
  const runningUnit = (services.find(x => x.unit === "f10-dashboard") || {});
  const w = r.window_s || 60;

  let head;
  if (!runningUnit.active) {
    head = `<span class="big off">stopped</span>`
         + `<span class="unit">f10-dashboard is not running</span>`;
  } else if (n == null) {
    /* Distinguish "before the first drive" from "the configured
       directory does not exist" - identical on screen otherwise, and the
       second is a config bug masquerading as a quiet runtime. */
    const bad = r.sessions_dir && r.sessions_dir_exists === false;
    head = `<span class="big off">${bad ? "!" : "?"}</span>`
         + `<span class="unit">${bad
             ? "sessions_dir does not exist: " + escape_(r.sessions_dir)
             : "no drive file yet"}</span>`;
  } else {
    head = `<span class="big ${live ? "on" : "off"}">`
         + `${n.toLocaleString()}</span>`
         + `<span class="unit">samples in the last ${w}s`
         + (r.channels ? ` · ${r.channels} channels` : "") + `</span>`;
  }

  const bits = [];
  if (r.link != null) bits.push(`car <b>${r.link ? "linked" : "no link"}</b>`);
  if (r.hz) bits.push(`<b>${r.hz}</b> Hz`);
  if (r.mode) bits.push(`mode <b>${escape_(r.mode)}</b>`
    + (r.duty === "asleep" ? " (asleep)" : ""));
  if (r.run != null) bits.push(`run <b>${r.run}</b>`);
  if (r.db) bits.push(escape_(r.db));

  let warn = "";
  if (runningUnit.active && r.link === false)
    warn = "⚠ the runtime is up but not talking to the car — check the cable";
  else if (runningUnit.active && n === 0)
    warn = "⚠ connected, but nothing has been written for a minute";
  else if (r.clock_synced === false)
    warn = "⚠ clock not NTP-synced — timestamps on this run are suspect";

  /* Each item its own element: the row is a flex container with a gap,
     which does nothing for a single concatenated text run - that is how
     "9.8 Hz" and "mode normal" ran together as "9.8 Hzmode normal". */
  $("recording").innerHTML =
    `<div class="rec">${head}</div>`
    + `<div class="recmeta">`
    + bits.map(x => `<span>${x}</span>`).join("")
    + `</div>`
    + (warn ? `<div class="recwarn">${escape_(warn)}</div>` : "");
}

function renderSessions(rows) {
  if (!rows.length) {
    $("sessions").innerHTML = '<span class="sub">no drive files</span>';
    return;
  }

  $("sessions").innerHTML = rows.map(r => {
    const tick = r.active ? '<span class="tick live">recording</span>'
      : r.synced === true ? '<span class="tick yes">in the lake</span>'
      : r.synced === false ? '<span class="tick no">not shipped</span>'
      : '<span class="tick no">unknown</span>';
    const when = new Date(r.mtime * 1000).toLocaleString();

    return `<div class="sess">
      <span class="nm"><b>${escape_(r.name)}</b>
        <span>${r.size_mb} MB · ${escape_(when)}</span></span>
      ${tick}
      ${r.active ? "" :
        `<button data-del="${escape_(r.name)}"
           ${r.synced === true ? "" : "disabled"}>Delete</button>`}
    </div>`;
  }).join("");
}

async function refresh() {
  if (poller === null) return;          // host is rebooting or halting

  try {
    const s = await api("/api/status");
    render(s);
    setRuntime(!!(s.recording && s.recording.up));
  } catch (e) {
    $("sub").textContent = "lost contact — " + e.message;
  }
}

/* ---- tabs -------------------------------------------------------- */

const TELE_TABS = ["drive", "detail", "table"];
const MGMT_TABS = ["system", "car", "claude"];
let tab = null;

function showTab(want) {
  if (!TELE_TABS.includes(want) && !MGMT_TABS.includes(want)) want = "drive";
  tab = want;
  const tele = TELE_TABS.includes(want);

  for (const t of document.querySelectorAll(".tab"))
    t.classList.toggle("on", t.dataset.tab === want);

  document.body.classList.toggle("tele", tele);
  $("pane-tele").style.display = tele ? "" : "none";
  $("mgmt").style.display = tele ? "none" : "";

  for (const name of MGMT_TABS)
    $("pane-" + name).style.display = name === want ? "" : "none";

  if (want === "car") loadCar(true);

  if (tele) {
    openTelemetry();
    teleMode(want);
    window.scrollTo(0, 0);
    fitFrame();
  }

  try { localStorage.setItem("f10tab", want); } catch (e) {}
  if (location.hash !== "#" + want) history.replaceState(null, "", "#" + want);
}

function wantedTab() {
  const fromHash = location.hash.replace(/^#/, "");
  if (fromHash) return fromHash;
  try { return localStorage.getItem("f10tab") || "drive"; }
  catch (e) { return "drive"; }
}

/* ---- the telemetry frame ----------------------------------------- */

/* The dashboard/ page, loaded the first time a telemetry tab opens -
   not before, so a phone left on the System tab does not hold a stream
   open. Same origin, so the tabs drive its mode switch directly and the
   browser reuses this page's credentials for everything it fetches. */
const tele = $("tele");
let teleLoaded = false;
let runtimeUp = null;

function openTelemetry() {
  if (!tele.getAttribute("src")) tele.src = "/dashboard/";
}

tele.addEventListener("load", () => {
  const doc = tele.contentDocument;
  if (!doc || !doc.getElementById("modeswitch")) return;   // about:blank
  teleLoaded = true;
  /* The panel's tabs are the mode switch now; two would disagree. */
  doc.getElementById("modeswitch").style.display = "none";
  if (TELE_TABS.includes(tab)) teleMode(tab);
});

function teleMode(mode) {
  const doc = tele.contentDocument;
  const btn = doc && doc.querySelector(`#modeswitch [data-mode="${mode}"]`);
  if (btn) btn.click();
}

function fitFrame() {
  const frame = $("teleframe");
  const top = frame.getBoundingClientRect().top + window.scrollY;
  frame.style.height = Math.max(320, window.innerHeight - top - 6) + "px";
}

/* The proxy answers 503 while live.py is down; the framed page's own
   stream retries by itself, but its one-time loads (meta, run list)
   gave up. So: a banner while it is down, and one reload of the frame
   when it is back - the honest resync. */
function setRuntime(up) {
  $("telewarn").style.display = up ? "none" : "";
  if (up && runtimeUp === false && teleLoaded) {
    try { tele.contentWindow.location.reload(); } catch (e) {}
  }
  runtimeUp = up;
}

window.addEventListener("resize", fitFrame);
window.addEventListener("hashchange", () =>
  showTab(location.hash.replace(/^#/, "") || "drive"));

document.addEventListener("click", async ev => {
  const b = ev.target.closest("button");
  if (!b) return;

  if (b.dataset.tab) {
    showTab(b.dataset.tab);
    return;
  }

  try {
    if (b.dataset.claude) {
      const verb = b.dataset.claude;
      arm(b, verb === "stop" ? "Stop" : "Restart session", async () => {
        busy(b, "…");
        try {
          await post("claude", {verb});
          say("Claude session " + (verb === "stop" ? "stopped" : "restarted")
              + " — give it a few seconds");
        } finally {
          unbusy(b);
        }
        setTimeout(refresh, 3000);
      });
      return;
    }

    if (b.dataset.del) {
      arm(b, "Delete", async () => {
        busy(b, "…");
        try {
          const r = await post("delete_session", {name: b.dataset.del});
          say(`Deleted ${r.deleted} — ${r.freed_mb} MB freed`);
        } finally {
          unbusy(b);
        }
        refresh();
      });
      return;
    }

    if (b.dataset.sync) {
      busy(b, "…");
      try {
        await post("sync", {verb: b.dataset.sync, confirm: true});
        say("Sync " + (b.dataset.sync === "pause" ? "paused" : "resumed"));
      } finally {
        unbusy(b);
      }
      setTimeout(refresh, 800);
      return;
    }

    if (b.dataset.log) {
      busy(b, "…");
      try {
        const boot = $("prevboot").checked ? -1 : 0;
        const r = await post("logs", {unit: b.dataset.log, boot});
        const pre = $("logs");
        pre.style.display = "block";
        pre.textContent = r.lines || "(no journal entries)";
        /* Newest last, so show the end - and bring the block itself
           into view, since on a phone it opens below the fold. */
        pre.scrollTop = pre.scrollHeight;
        pre.scrollIntoView({behavior: "smooth", block: "nearest"});
        say(`${b.dataset.log}${boot ? " (previous boot)" : ""}: `
            + `${(r.lines || "").split("\n").length} log lines`);
      } finally {
        unbusy(b);
      }
      return;
    }

    if (b.dataset.restart) {
      arm(b, "Restart", async () => {
        busy(b, "…");
        try {
          await post("restart", {unit: b.dataset.restart});
          say("Restarted " + b.dataset.restart);
        } finally {
          unbusy(b);
        }
        setTimeout(refresh, 1500);
      });
      return;
    }

    if (b.dataset.toggle) {
      const verb = b.dataset.verb;
      arm(b, verb === "stop" ? "Stop" : "Start", async () => {
        busy(b, "…");
        try {
          await post("service", {unit: b.dataset.toggle, verb});
          say((verb === "stop" ? "Stopped " : "Started ") + b.dataset.toggle);
        } finally {
          unbusy(b);
        }
        setTimeout(refresh, 1500);
      });
      return;
    }

    if (b.id === "btn-pull") {
      arm(b, "Pull latest", async () => {
        /* A fetch over a phone hotspot takes seconds. Without a busy
           state the button looks inert for the whole of it. */
        busy(b, "Pulling…");
        try {
          const r = await post("pull");
          say(r.changed
            ? `Pulled ${r.commits} commit${r.commits === 1 ? "" : "s"}: `
              + `${r.before} → ${r.after}\n“${r.subject}”\n${r.note}.`
            : `Already up to date at ${r.after}\n“${r.subject}”`);
        } finally {
          unbusy(b);
        }
        refresh();
      });
      return;
    }

    const act = b.dataset.act;
    if (act === "reboot" || act === "shutdown") {
      arm(b, act === "reboot" ? "Reboot" : "Shut down", async () => {
        busy(b, "…");
        await post(act);
        say(act === "reboot"
          ? "Rebooting — this page will go dead for a minute or so."
          : "Halting. Wait for the green LED to stop blinking, "
            + "then cut the powerbank.");
        /* The host is going away. Polling it just replaces the message
           the user needs to read with "lost contact". */
        stopPolling();
      });
    }
  } catch (e) {
    say(e.message, true);
    refresh();
  }
});

function stopPolling() {
  clearInterval(poller);
  poller = null;
}

showTab(wantedTab());
refresh();
poller = setInterval(refresh, 5000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
