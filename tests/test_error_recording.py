"""
Per-request faults, recorded rather than discarded.

Before this, a request that timed out or was refused left no trace at all -
`on_error` was never wired to anything. That made a failing channel and an
unasked one indistinguishable: both simply have no rows in `samples`, so
"how often does this channel actually fail?" had no answer.

Covers the whole path offline: classification -> SQLite -> sync agent ->
ingest builder.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest

from tests import support  # noqa: F401

import live
from bmwdiag.mapping import fault_kind
from bmwdiag.mapping.errors import DecodeError

sys.path.insert(0, os.path.join(support.ROOT, "infra"))
from sync import agent as sync_agent          # noqa: E402
from ingest import server as ingest_server    # noqa: E402
from common import wire                       # noqa: E402


class HsfzError(Exception):
    pass


class HsfzNack(HsfzError):
    pass


class Classification(unittest.TestCase):
    """A kind is data you can GROUP BY; a message is prose that changes."""

    def test_each_fault_gets_a_stable_kind(self):
        cases = [
            (HsfzNack("will not route to 0x18"), "transport_nack"),
            (TimeoutError("HSFZ read timeout"), "transport_timeout"),
            (ConnectionResetError("reset"), "transport_link"),
            (BrokenPipeError("pipe"), "transport_link"),
            (DecodeError("bad", "file", "path"), "decode"),
            (ValueError("something else"), "other"),
        ]

        for exc, expected in cases:
            self.assertEqual(fault_kind(exc), expected, exc)


class Recording(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "rec.db")
        self.rec = live.Recorder(self.db)
        self.rec.open()
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        time.sleep(0.05)

    def tearDown(self):
        try:
            self.rec.close()
        except Exception:
            pass

    def _rows(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT request_id, kind, message FROM errors ORDER BY rowid"
            ).fetchall()
        finally:
            con.close()

    def test_a_fault_is_stored_with_its_request_and_kind(self):
        self.rec.error("egs.selector.DA2E", "transport_nack", "will not route")
        time.sleep(0.3)
        self.rec.close()

        self.assertEqual(
            self._rows(), [("egs.selector.DA2E", "transport_nack", "will not route")]
        )

    def test_a_long_message_is_truncated(self):
        """A fault storm must not be able to bloat the database."""
        self.rec.error("r", "other", "x" * 5000)
        time.sleep(0.3)
        self.rec.close()

        self.assertLessEqual(len(self._rows()[0][2]), 500)


class Shipping(unittest.TestCase):
    def _db_with_errors(self, path, include_table=True):
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL,"
            " ended_at REAL, vin TEXT, gateway TEXT, ecu TEXT, ecu_addr INTEGER);"
        )
        con.execute("INSERT INTO runs(id,started_at,vin) VALUES(1,1.0,'V')")

        if include_table:
            con.executescript(
                "CREATE TABLE errors(run_id INTEGER, ts REAL, request_id TEXT,"
                " kind TEXT, message TEXT);"
            )
            con.execute(
                "INSERT INTO errors VALUES(1, 1.5, 'egs.selector.DA2E',"
                " 'transport_nack', 'will not route to 0x18')"
            )

        con.commit()
        con.close()

    def test_the_agent_reads_faults(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db_with_errors(db)

        rows = sync_agent.read_errors(db, 0, 100)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], "egs.selector.DA2E")
        self.assertEqual(rows[0]["kind"], "transport_nack")

    def test_a_database_without_the_table_is_empty_not_an_error(self):
        """Databases recorded before fault logging existed must still sync."""
        db = os.path.join(tempfile.mkdtemp(), "old.db")
        self._db_with_errors(db, include_table=False)

        self.assertEqual(sync_agent.read_errors(db, 0, 100), [])

    def test_the_ingest_server_builds_rows_for_clickhouse(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db_with_errors(db)
        rows = sync_agent.read_errors(db, 0, 100)

        batch = wire.columnar(
            "channel_errors",
            [{k: v for k, v in r.items() if k != "_rowid"} for r in rows],
            meta={"db": "t.db"},
        )
        built = ingest_server.build_channel_errors(batch)

        self.assertEqual(built[0]["request_id"], "egs.selector.DA2E")
        self.assertEqual(built[0]["kind"], "transport_nack")
        self.assertIn("channel_errors", ingest_server.BUILDERS)

    def test_the_batch_survives_the_wire_format(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db_with_errors(db)
        rows = sync_agent.read_errors(db, 0, 100)
        batch = wire.columnar(
            "channel_errors",
            [{k: v for k, v in r.items() if k != "_rowid"} for r in rows],
        )

        back = wire.decode(wire.encode(batch))

        self.assertEqual(
            wire.rows_of(back)[0]["request_id"], "egs.selector.DA2E"
        )


if __name__ == "__main__":
    unittest.main()
