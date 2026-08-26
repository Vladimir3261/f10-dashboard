"""
Telemetry sync: wire format, the fault-tolerant client agent, and the
server's normalization - all offline, no ClickHouse, no network beyond a
localhost fake ingest server.
"""

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests import support  # noqa: F401

import sys
sys.path.insert(0, os.path.join(support.ROOT, "infra"))
from common import wire                                    # noqa: E402
from sync import agent as sync_agent                       # noqa: E402
from ingest import server as ingest_server                 # noqa: E402


def make_db(path, runs, samples):
    """Build a telemetry.db-shaped fixture."""
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL,"
        " ended_at REAL, vin TEXT, gateway TEXT, ecu TEXT, ecu_addr INTEGER);"
        "CREATE TABLE params(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE,"
        " pid INTEGER, label TEXT, unit TEXT);"
        "CREATE TABLE samples(run_id INTEGER, ts REAL, param_id INTEGER, value REAL);"
    )
    for r in runs:
        con.execute("INSERT INTO runs(id,started_at,ended_at,vin,ecu,ecu_addr)"
                    " VALUES(?,?,?,?,?,?)", r)
    pid = {}
    for key, unit in {("rpm", "rpm"), ("n47d_oil_temp", "°C")}:
        cur = con.execute("INSERT INTO params(key,unit) VALUES(?,?)", (key, unit))
        pid[key] = cur.lastrowid
    for run_id, ts, key, value in samples:
        con.execute("INSERT INTO samples(run_id,ts,param_id,value) VALUES(?,?,?,?)",
                    (run_id, ts, pid[key], value))
    con.commit()
    con.close()
    return pid


class Wire(unittest.TestCase):
    def test_round_trip(self):
        rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
        batch = wire.columnar("samples", rows, cursor=9, meta={"db": "d"})
        self.assertEqual(batch["rows"], 2)
        self.assertEqual(batch["cursor"], 9)
        back = wire.decode(wire.encode(batch))
        self.assertEqual(wire.rows_of(back), rows)

    def test_bad_frame_rejected(self):
        with self.assertRaises(ValueError):
            wire.decode(b"not a batch")

    def test_compression_beats_row_json(self):
        import json
        rows = [{"vehicle_id": "V", "session_id": 1, "ts": 1e9 + i,
                 "channel_raw": "rpm", "unit": "", "value": 800.0 + i}
                for i in range(2000)]
        blob = wire.encode(wire.columnar("samples", rows, cursor=2000))
        self.assertLess(len(blob), len(json.dumps(rows)) // 5)


class Normalization(unittest.TestCase):
    def test_maps_known_and_passes_through_unknown(self):
        cmap = ingest_server.CHANNEL_MAP
        self.assertEqual(cmap.normalize("V", "rpm"), "engine.rpm")
        self.assertEqual(cmap.normalize("V", "n47d_dpf_dp"),
                         "dpf.differential_pressure")
        # unknown key is preserved, never lost
        self.assertEqual(cmap.normalize("V", "totally_new_channel"),
                         "totally_new_channel")

    def test_build_samples_fills_channel_and_ts(self):
        batch = wire.columnar("samples", [
            {"vehicle_id": "V", "session_id": 3, "ts": 1_756_000_000.5,
             "channel_raw": "n47d_oil_temp", "unit": "°C", "value": 88.0},
        ], meta={"mapping_ver": "abc"})
        rows = ingest_server.build_samples(batch)
        self.assertEqual(rows[0]["channel"], "engine.oil_temperature")
        self.assertEqual(rows[0]["channel_raw"], "n47d_oil_temp")
        self.assertEqual(rows[0]["mapping_ver"], "abc")
        self.assertTrue(rows[0]["ts"].startswith("2025-") or rows[0]["ts"][:2] == "20")


class FakeIngest:
    """A localhost stand-in for the ingest server: decodes and records."""

    def __init__(self, fail_times=0, code_on_fail=503):
        self.batches = []
        self.fail_times = fail_times
        self.code_on_fail = code_on_fail
        self.token = "secret"
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self.send_response(401); self.send_header("Content-Length", "0")
                    self.end_headers(); return
                n = int(self.headers.get("Content-Length", "0"))
                blob = self.rfile.read(n)
                if outer.fail_times > 0:
                    outer.fail_times -= 1
                    self.send_response(outer.code_on_fail)
                    self.send_header("Content-Length", "0"); self.end_headers()
                    return
                batch = wire.decode(blob)
                outer.batches.append(batch)
                self.send_response(200)
                self.send_header("Content-Length", "0"); self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()


class AgentSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "telemetry.db")
        make_db(
            self.db,
            runs=[(1, 1_756_000_000.0, 1_756_000_100.0, "VIN123", "0x12", 18)],
            samples=[(1, 1_756_000_000.0 + i * 0.1, "rpm", 800 + i)
                     for i in range(2500)],
        )
        self.fake = FakeIngest()

    def tearDown(self):
        self.fake.close()

    def _agent(self, **over):
        cfg = dict(sync_agent.DEFAULTS)
        cfg.update({
            "server_url": self.fake.url(), "token": "secret",
            "databases": [self.db],
            "state_file": os.path.join(self.tmp, "state.json"),
            "batch_rows": 1000, "max_backoff": 0.05,
        })
        cfg.update(over)
        return sync_agent.Agent(cfg)

    def test_full_backfill_then_idempotent_tail(self):
        agent = self._agent()
        # drain: 2500 samples / 1000 per batch -> 3 sample batches
        agent._sync_sessions(self.db)
        while agent._sync_samples(self.db):
            pass
        sample_batches = [b for b in self.fake.batches if b["table"] == "samples"]
        total = sum(b["rows"] for b in sample_batches)
        self.assertEqual(total, 2500)
        self.assertEqual([b["rows"] for b in sample_batches], [1000, 1000, 500])
        # sessions synced too, with the VIN
        sess = [b for b in self.fake.batches if b["table"] == "sessions"]
        self.assertTrue(sess)
        self.assertEqual(wire.rows_of(sess[0])[0]["vehicle_id"], "VIN123")

        # watermark persisted at the last rowid
        self.assertEqual(agent.state.get(self.db, "samples_rowid"), 2500)

        # a second drain sends nothing (nothing new)
        before = len(self.fake.batches)
        self.assertFalse(agent._sync_samples(self.db))
        self.assertEqual(len(self.fake.batches), before)

    def test_watermark_survives_a_restart(self):
        a1 = self._agent()
        while a1._sync_samples(self.db):
            pass
        # a fresh agent reading the same state file resumes at the end
        a2 = self._agent()
        self.assertEqual(a2.state.get(self.db, "samples_rowid"), 2500)
        self.assertFalse(a2._sync_samples(self.db))

    def test_retries_transient_failure_then_advances(self):
        self.fake.fail_times = 2          # first two POSTs -> 503
        agent = self._agent(batch_rows=5000)
        ok = agent._sync_samples(self.db)  # should retry until 200
        self.assertFalse(ok)               # <5000 rows -> no more
        self.assertEqual(agent.state.get(self.db, "samples_rowid"), 2500)
        # exactly one batch of data ultimately landed (retries are re-sends)
        landed = [b for b in self.fake.batches if b["table"] == "samples"]
        self.assertEqual(len(landed), 1)

    def test_auth_failure_pauses_without_advancing(self):
        agent = self._agent(token="wrong")
        agent._sync_samples(self.db)
        self.assertFalse(agent.enabled)                       # paused
        self.assertEqual(agent.state.get(self.db, "samples_rowid"), 0)  # no advance

    def test_new_samples_after_a_sync_are_picked_up(self):
        agent = self._agent()
        while agent._sync_samples(self.db):
            pass
        # live.py appends more (a later drive)
        con = sqlite3.connect(self.db)
        pid = con.execute("SELECT id FROM params WHERE key='rpm'").fetchone()[0]
        con.executemany(
            "INSERT INTO samples(run_id,ts,param_id,value) VALUES(1,?,?,?)",
            [(1_756_000_300.0 + i, pid, 900 + i) for i in range(30)],
        )
        con.commit(); con.close()
        self.assertFalse(agent._sync_samples(self.db))   # <batch -> no more
        landed = sum(b["rows"] for b in self.fake.batches if b["table"] == "samples")
        self.assertEqual(landed, 2530)


if __name__ == "__main__":
    unittest.main()
