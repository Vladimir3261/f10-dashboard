#!/usr/bin/env python3
"""
sync/agent.py - fault-tolerant local telemetry sync.

Runs on the same machine as live.py (the car laptop / future embedded
box). It reads the SQLite telemetry databases READ-ONLY - live.py's
writing path is untouched - and ships everything not yet synced to the
ingest server, which is the only thing that talks to ClickHouse.

Built for a mobile link that is slow and drops without warning:

  * **Watermark, not a queue.** Progress is a durable per-database rowid
    watermark in a sidecar state file. On first run the watermark is 0,
    so the very first sync uploads the entire backlog; after that it
    tails. `samples.rowid` is monotonic because samples are append-only.
  * **One batch in flight, ever.** The loop sends a batch and blocks
    until it is acked before building the next - a slow upload can never
    pile up a queue behind it.
  * **Advance only on ack.** The watermark moves only after a 2xx. A
    connection lost mid-upload just means the same batch is re-sent; the
    server's ReplacingMergeTree collapses the replay, so delivery is
    effectively once even though it is retried.
  * **Backoff on trouble.** Timeouts / 5xx / network errors back off with
    jitter and retry the same batch. A 401 (bad token) or 4xx (bad
    request) pauses and surfaces on the dashboard rather than spinning.

A small control HTTP endpoint exposes status and an on/off switch so the
sync can be watched and paused from the live.py dashboard during a drive.

Stdlib only. Config via a JSON file (--config) and/or env; see
config.example.json.
"""

import argparse
import glob
import json
import os
import random
import sqlite3
import zlib
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import wire  # noqa: E402


# ----------------------------------------------------------- config


DEFAULTS = {
    "server_url": "http://localhost:8090",
    "token": "",
    "databases": ["telemetry.db"],       # paths or globs
    "state_file": "local/sync-state.json",
    "batch_rows": 5000,
    "idle_interval": 5.0,                 # seconds between polls when caught up
    "control_port": 8091,
    "mapping_ver": "",
    "connect_timeout": 10.0,
    "read_timeout": 60.0,
    "max_backoff": 60.0,
    "enabled": True,
}


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)

    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))

    # env overrides (SYNC_SERVER_URL, SYNC_TOKEN, ...)
    for key in cfg:
        env = os.environ.get("SYNC_" + key.upper())

        if env is not None:
            cur = cfg[key]
            cfg[key] = type(cur)(env) if not isinstance(cur, (list, dict)) else env

    return cfg


# ----------------------------------------------------------- state


class State:
    """Durable per-database watermarks, written atomically."""

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Dict[str, int]] = {}
        self.lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}

    def get(self, db: str, key: str) -> int:
        with self.lock:
            return int(self.data.get(db, {}).get(key, 0))

    def set(self, db: str, key: str, value: int) -> None:
        with self.lock:
            self.data.setdefault(db, {})[key] = int(value)
            self._flush()

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"

        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh)
            fh.flush()
            os.fsync(fh.fileno())

        os.replace(tmp, self.path)


# ----------------------------------------------------------- reading


def global_session_id(db_path: str, run_id: int) -> int:
    """
    A globally-unique session id from the local run id.

    Local run ids reset to 1 in every SQLite database, so the raw id
    collides across drives (and across the main db and the per-drive
    session dbs) once they all land in one ClickHouse table. Namespacing
    by the database basename keeps each drive distinct and is
    deterministic, so a re-sync still de-duplicates. The source db name
    also rides along in the sessions row for traceability.
    """
    ns = zlib.crc32(os.path.basename(db_path).encode()) & 0xFFFFFFFF
    return (ns << 20) | (int(run_id) & 0xFFFFF)


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether `table` has `column` (older dbs predate mapping versioning)."""
    return any(r[1] == column for r in con.execute(f"PRAGMA table_info({table})"))


def read_samples(db_path: str, after_rowid: int, limit: int) -> List[Dict]:
    """Unsynced sample rows, resolved to VIN + channel key, read-only."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    try:
        # p.mapping_ver only exists on databases recorded after mapping
        # versioning landed; fall back to '' for older ones.
        ver = "p.mapping_ver" if _has_column(con, "params", "mapping_ver") else "''"
        cur = con.execute(
            f"SELECT s.rowid, r.vin, s.run_id, s.ts, p.key, p.unit, s.value, {ver} "
            "FROM samples s "
            "JOIN runs r   ON r.id = s.run_id "
            "JOIN params p ON p.id = s.param_id "
            "WHERE s.rowid > ? ORDER BY s.rowid LIMIT ?",
            (after_rowid, limit),
        )
        rows = cur.fetchall()
    finally:
        con.close()

    return [
        {"_rowid": rid, "vehicle_id": vin,
         "session_id": global_session_id(db_path, run_id),
         "ts": ts, "channel_raw": key, "unit": unit or "", "value": value,
         "mapping_ver": mver or ""}
        for rid, vin, run_id, ts, key, unit, value, mver in rows
    ]


def read_sessions(db_path: str, after_id: int) -> List[Dict]:
    """New or updated runs (as sessions). Small; re-sends the open run."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    try:
        # runs.mapping_set only exists post-versioning; fall back to ''.
        mset = "mapping_set" if _has_column(con, "runs", "mapping_set") else "''"
        rows = con.execute(
            f"SELECT id, vin, started_at, ended_at, ecu, ecu_addr, gateway, {mset} "
            "FROM runs WHERE id >= ? ORDER BY id",
            (after_id,),
        ).fetchall()
    finally:
        con.close()

    return [
        {"_id": rid, "vehicle_id": vin,
         "session_id": global_session_id(db_path, rid),
         "started": started, "ended": ended, "ecu": ecu or "",
         "ecu_addr": ecu_addr, "gateway": gateway or "", "mappings": mset_val or ""}
        for rid, vin, started, ended, ecu, ecu_addr, gateway, mset_val in rows
    ]


def max_sample_rowid(db_path: str) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    try:
        row = con.execute("SELECT MAX(rowid) FROM samples").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        con.close()


# ----------------------------------------------------------- agent


class Agent:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.state = State(cfg["state_file"])
        self.enabled = bool(cfg["enabled"])
        self.stop = threading.Event()
        self.status: Dict[str, Any] = {
            "enabled": self.enabled, "state": "starting",
            "last_sync": None, "last_error": None,
            "sent_rows": 0, "sent_bytes": 0, "databases": {},
        }
        self.status_lock = threading.Lock()

    # -- databases --------------------------------------------------

    def databases(self) -> List[str]:
        found: List[str] = []

        for spec in self.cfg["databases"]:
            hits = glob.glob(spec)
            found.extend(hits if hits else ([spec] if os.path.isfile(spec) else []))

        # de-dup, stable order
        seen = set()
        out = []

        for db in found:
            real = os.path.abspath(db)

            if real not in seen:
                seen.add(real)
                out.append(db)

        return out

    # -- transport --------------------------------------------------

    def _post(self, batch: Dict[str, Any]) -> int:
        """POST one encoded batch. Returns HTTP status; raises on network."""
        blob = wire.encode(batch)
        url = self.cfg["server_url"].rstrip("/") + "/ingest"
        request = urllib.request.Request(
            url, data=blob, method="POST",
            headers={
                "Authorization": f"Bearer {self.cfg['token']}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(blob)),
            },
        )

        with self.status_lock:
            self.status["sent_bytes"] += len(blob)

        try:
            with urllib.request.urlopen(
                request, timeout=self.cfg["read_timeout"]
            ) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def _send_with_retry(self, batch: Dict[str, Any]) -> bool:
        """Send one batch, retrying transient failures. False = give up now."""
        backoff = 1.0

        while not self.stop.is_set():
            try:
                code = self._post(batch)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                self._set_status(state="offline", last_error=str(exc))
            else:
                if code == 200:
                    return True

                if code == 401:
                    self._set_status(state="paused (auth)",
                                     last_error="401 unauthorized - check token")
                    self.enabled = False
                    return False

                if 400 <= code < 500 and code != 429:
                    self._set_status(state="paused (bad request)",
                                     last_error=f"HTTP {code}")
                    self.enabled = False
                    return False

                self._set_status(state="retrying", last_error=f"HTTP {code}")

            # transient: back off with jitter and retry the SAME batch
            wait = min(backoff, self.cfg["max_backoff"])
            wait += random.uniform(0, wait * 0.3)
            self.stop.wait(wait)
            backoff = min(backoff * 2, self.cfg["max_backoff"])

        return False

    # -- one database ----------------------------------------------

    def _sync_sessions(self, db: str) -> None:
        wm = self.state.get(db, "sessions_id")
        rows = read_sessions(db, wm)

        if not rows:
            return

        batch = wire.columnar(
            "sessions",
            [{k: v for k, v in r.items() if k != "_id"} for r in rows],
            cursor=max(r["_id"] for r in rows),
            meta={"db": os.path.basename(db), "mapping_ver": self.cfg["mapping_ver"]},
        )

        if self._send_with_retry(batch):
            closed = [r["_id"] for r in rows if r["ended"]]

            if closed:
                self.state.set(db, "sessions_id", max(closed) + 1)

    def _sync_samples(self, db: str) -> bool:
        """Send one batch of samples. Returns True if there may be more."""
        wm = self.state.get(db, "samples_rowid")
        rows = read_samples(db, wm, self.cfg["batch_rows"])

        if not rows:
            return False

        cursor = max(r["_rowid"] for r in rows)
        batch = wire.columnar(
            "samples",
            [{k: v for k, v in r.items() if k != "_rowid"} for r in rows],
            cursor=cursor,
            meta={"db": os.path.basename(db), "mapping_ver": self.cfg["mapping_ver"]},
        )

        if self._send_with_retry(batch):
            self.state.set(db, "samples_rowid", cursor)

            with self.status_lock:
                self.status["sent_rows"] += len(rows)
                self.status["last_sync"] = time.time()

            return len(rows) == self.cfg["batch_rows"]   # full batch -> more

        return False

    # -- main loop --------------------------------------------------

    def run(self) -> None:
        threading.Thread(target=self._serve_control, daemon=True).start()

        while not self.stop.is_set():
            if not self.enabled:
                self._set_status(state="paused")
                self.stop.wait(1.0)
                continue

            did_work = False

            for db in self.databases():
                if self.stop.is_set() or not self.enabled:
                    break

                self._sync_sessions(db)

                # drain this db one batch at a time (single in flight)
                while self.enabled and not self.stop.is_set():
                    more = self._sync_samples(db)
                    self._update_db_status(db)

                    if not more:
                        break

                    did_work = True

            if self.enabled:
                self._set_status(state="synced" if not did_work else "syncing",
                                 last_error=None if not did_work else
                                 self.status.get("last_error"))

            self.stop.wait(self.cfg["idle_interval"])

    # -- status -----------------------------------------------------

    def _set_status(self, **kw) -> None:
        with self.status_lock:
            self.status.update(kw)
            self.status["enabled"] = self.enabled

    def _update_db_status(self, db: str) -> None:
        try:
            top = max_sample_rowid(db)
        except sqlite3.Error:
            top = None

        wm = self.state.get(db, "samples_rowid")

        with self.status_lock:
            self.status["databases"][os.path.basename(db)] = {
                "synced_rowid": wm,
                "max_rowid": top,
                "pending": (top - wm) if top is not None else None,
            }

    def _serve_control(self) -> None:
        agent = self

        class Control(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

            def _json(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self._cors()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if self.path.split("?")[0] == "/sync/status":
                    with agent.status_lock:
                        self._json(200, dict(agent.status))
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                path = self.path.split("?")[0]

                if path == "/sync/pause":
                    agent.enabled = False
                    agent._set_status(state="paused")
                    self._json(200, {"enabled": False})
                elif path == "/sync/resume":
                    agent.enabled = True
                    agent._set_status(state="resuming", last_error=None)
                    self._json(200, {"enabled": True})
                else:
                    self._json(404, {"error": "not found"})

        server = ThreadingHTTPServer(("0.0.0.0", self.cfg["control_port"]), Control)
        server.daemon_threads = True
        server.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description="local telemetry sync agent")
    ap.add_argument("--config", default=os.environ.get("SYNC_CONFIG"))
    args = ap.parse_args()

    cfg = load_config(args.config)

    if not cfg["token"]:
        print("[!] no token configured - set token in config or SYNC_TOKEN",
              file=sys.stderr)
        return 2

    agent = Agent(cfg)
    print(f"[+] sync agent -> {cfg['server_url']}  "
          f"(control :{cfg['control_port']}, dbs {agent.databases()})",
          flush=True)

    try:
        agent.run()
    except KeyboardInterrupt:
        agent.stop.set()

    return 0


if __name__ == "__main__":
    sys.exit(main())
