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

from bmwdiag.identity import session_id_from_ulid
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


def global_session_id(db_path: str, run_id: int,
                      session_uid: str = "") -> int:
    """
    The lake's numeric session id for one local run.

    Prefers the run's own durable identity. `session_uid` is a ULID minted
    when the run was created and stored with it, so the id derived from it
    survives renaming, copying and identical basenames - none of which say
    anything about which drive the data is.

    LEGACY PATH, and it stays. Runs recorded before session_uid existed
    have none, and their sessions are already in the lake under an id
    derived from the database's basename. Re-deriving those would not
    correct them, it would duplicate them: the old rows would remain and a
    second copy would appear under a new id. So a run without a uid keeps
    the filename derivation it was written with, with all of that
    scheme's faults, and only new runs get durable identity.

    Local run ids reset to 1 in every database, so the raw id collides
    across drives once they all land in one ClickHouse table; both schemes
    exist to namespace that away.
    """
    if session_uid:
        return session_id_from_ulid(session_uid)

    ns = zlib.crc32(os.path.basename(db_path).encode()) & 0xFFFFFFFF

    return (ns << 20) | (int(run_id) & 0xFFFFF)


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether `table` has `column` (older dbs predate mapping versioning)."""
    return any(r[1] == column for r in con.execute(f"PRAGMA table_info({table})"))


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    """Whether `table` exists (older dbs predate run-scoped provenance)."""
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()

    return row is not None


def read_samples(db_path: str, after_rowid: int, limit: int) -> List[Dict]:
    """Unsynced sample rows, resolved to VIN + channel key, read-only."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    try:
        #
        # Provenance is resolved per RUN, not per channel-forever.
        #
        # params holds channel identity and is written once, on first
        # sight, so params.mapping_ver records whichever version happened
        # to be loaded the first time this database ever saw the channel.
        # Reuse the same database across a mapping revision and every new
        # sample would ship under the old version.
        #
        # run_channels answers it correctly: one row per (run, channel)
        # carrying the version that decoded THAT run. The join is LEFT and
        # falls back to params.mapping_ver, because rows recorded before
        # this table existed have no run_channels row.
        #
        # That fallback is the BEST AVAILABLE legacy provenance, not a
        # guarantee. A database that already crossed a mapping revision
        # before run_channels existed may carry a stale params.mapping_ver
        # for exactly the reason this change exists - and after the fact
        # there is nothing left to reconstruct the true version from. It
        # is reported rather than blanked because it is the only evidence
        # there is, not because it is known to be right.
        #
        # An empty string in run_channels.mapping_version is NOT a
        # fallback trigger: the row exists, so that run's answer is known
        # and the answer is "unknown". Only a missing ROW defers.
        #
        has_run_channels = _has_table(con, "run_channels")
        ver_col = (
            "p.mapping_ver" if _has_column(con, "params", "mapping_ver")
            else "''"
        )
        ver = (
            f"COALESCE(rc.mapping_version, {ver_col})" if has_run_channels
            else ver_col
        )
        #
        # Unit is snapshotted per run for the same reason: a mapping
        # revision that corrects a unit must not restate the units of
        # samples recorded before the correction.
        #
        unit = "COALESCE(rc.unit, p.unit)" if has_run_channels else "p.unit"
        join = (
            "LEFT JOIN run_channels rc "
            "  ON rc.run_id = s.run_id AND rc.param_id = s.param_id "
            if has_run_channels else ""
        )
        #
        # s.quality only exists post-data-quality, and is NULL for rows
        # that were already in a database when the column was added.
        #
        # Both cases report 'ok', which needs stating precisely: it claims
        # "the decoder of the day accepted this value", NOT "this value is
        # verified good". Those rows come from the narrow decode path,
        # which dropped every reading it could not use - so nothing
        # rejected is in there. What IS in there is everything nobody had
        # taught the decoder to reject yet, which on this car means every
        # lambda sentinel and every saturated MAP.
        #
        # The alternative would be a seventh 'unknown' enum value, and the
        # lake's Enum8 cannot express one without an ALTER MODIFY COLUMN.
        # Not worth a schema migration to relabel history that is already
        # described in docs/DATA_QUALITY.md.
        #
        qual = "s.quality" if _has_column(con, "samples", "quality") else "NULL"
        #: the run's durable identity, when it has one
        suid = ("r.session_uid" if _has_column(con, "runs", "session_uid")
                else "''")
        cur = con.execute(
            f"SELECT s.rowid, r.vin, s.run_id, s.ts, p.key, {unit}, s.value, "
            f"{ver}, {qual}, {suid} "
            "FROM samples s "
            "JOIN runs r   ON r.id = s.run_id "
            "JOIN params p ON p.id = s.param_id "
            f"{join}"
            "WHERE s.rowid > ? ORDER BY s.rowid LIMIT ?",
            (after_rowid, limit),
        )
        rows = cur.fetchall()
    finally:
        con.close()

    return [
        {"_rowid": rid, "vehicle_id": vin,
         "session_id": global_session_id(db_path, run_id, suid_val or ""),
         "ts": ts, "channel_raw": key, "unit": unit or "", "value": value,
         "mapping_ver": mver or "", "quality": qual or "ok"}
        for rid, vin, run_id, ts, key, unit, value, mver, qual, suid_val
        in rows
    ]


def read_sessions(db_path: str, after_id: int) -> List[Dict]:
    """New or updated runs (as sessions). Small; re-sends the open run."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    try:
        # runs.mapping_set only exists post-versioning; fall back to ''.
        mset = "mapping_set" if _has_column(con, "runs", "mapping_set") else "''"
        # runs.mode only exists post-drive-modes. A run recorded before
        # they existed was implicitly the pre-v2 rates, which is what
        # `debug` reproduces - but claiming that here would be inventing
        # a fact, so an unknown mode stays empty.
        mode = "mode" if _has_column(con, "runs", "mode") else "''"
        # Whether the host clock was NTP-disciplined when the run opened.
        # NULL on databases recorded before this was tracked - unknown
        # stays unknown rather than being assumed good.
        clk = ("clock_synced" if _has_column(con, "runs", "clock_synced")
               else "NULL")
        #
        # What the car physically WAS for this run. Snapshotted at record
        # time, so the lake can condition on the configuration that was
        # true for a session rather than on whatever the analyst's machine
        # believes today. '' on runs recorded before it was tracked -
        # unknown, and the lake side must not read that as "no hardware".
        #
        vlabel = ("vehicle_label" if _has_column(con, "runs", "vehicle_label")
                  else "''")
        vhw = ("vehicle_hardware"
               if _has_column(con, "runs", "vehicle_hardware") else "''")
        #
        # Durable identity and the boot that recorded the run. The uid
        # decides the numeric session id; the boot id is evidence for
        # grouping runs into one physical trip, since two runs from
        # different boots cannot be the same drive.
        #
        suid = ("session_uid" if _has_column(con, "runs", "session_uid")
                else "''")
        boot = ("boot_id" if _has_column(con, "runs", "boot_id") else "''")
        rows = con.execute(
            f"SELECT id, vin, started_at, ended_at, ecu, ecu_addr, gateway, "
            f"{mset}, {mode}, {clk}, {vlabel}, {vhw}, {suid}, {boot} "
            "FROM runs WHERE id >= ? ORDER BY id",
            (after_id,),
        ).fetchall()
    finally:
        con.close()

    return [
        {"_id": rid, "vehicle_id": vin,
         "session_id": global_session_id(db_path, rid, suid_val or ""),
         "started": started, "ended": ended, "ecu": ecu or "",
         "ecu_addr": ecu_addr, "gateway": gateway or "",
         "mappings": mset_val or "", "mode": mode_val or "",
         "clock_synced": clk_val,
         "vehicle_label": vlabel_val or "",
         "vehicle_hardware": vhw_val or "",
         #: the full durable identity, carried unchanged. The numeric
         #: session_id above is derived from it; this is the one that
         #: cannot collide.
         "session_uid": suid_val or "",
         "boot_id": boot_val or ""}
        for rid, vin, started, ended, ecu, ecu_addr, gateway, mset_val,
        mode_val, clk_val, vlabel_val, vhw_val, suid_val, boot_val in rows
    ]


def read_errors(db_path: str, after_rowid: int, limit: int) -> List[Dict]:
    """
    Unsynced per-request faults. Absent on databases recorded before fault
    logging existed, so a missing table is empty, not an error.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    try:
        if not con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='errors'"
        ).fetchone():
            return []

        rows = con.execute(
            "SELECT e.rowid, r.vin, e.run_id, e.ts, e.request_id, e.kind, e.message "
            "FROM errors e JOIN runs r ON r.id = e.run_id "
            "WHERE e.rowid > ? ORDER BY e.rowid LIMIT ?",
            (after_rowid, limit),
        ).fetchall()
    finally:
        con.close()

    return [
        {"_rowid": rid, "vehicle_id": vin,
         "session_id": global_session_id(db_path, run_id),
         "ts": ts, "request_id": request_id, "kind": kind,
         "message": (message or "")[:500]}
        for rid, vin, run_id, ts, request_id, kind, message in rows
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

    def _sync_errors(self, db: str) -> None:
        """
        Ship per-request faults. Low volume next to samples, but it is what
        makes an error RATE per channel computable - without it a failing
        request and an unasked one both just have no rows.
        """
        wm = self.state.get(db, "errors_rowid")
        rows = read_errors(db, wm, self.cfg["batch_rows"])

        if not rows:
            return

        cursor = max(r["_rowid"] for r in rows)
        batch = wire.columnar(
            "channel_errors",
            [{k: v for k, v in r.items() if k != "_rowid"} for r in rows],
            cursor=cursor,
            meta={"db": os.path.basename(db), "mapping_ver": self.cfg["mapping_ver"]},
        )

        if self._send_with_retry(batch):
            self.state.set(db, "errors_rowid", cursor)

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
                self._sync_errors(db)

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
