"""
Drive mode as a recorded property of a session.

The rule this enforces: a run has exactly ONE mode. Switching mode ends
the current run and opens a new one, so no dataset can silently mix
sampling rates. Mode is a confound in every longitudinal comparison this
project exists to make - "DPF dP has crept up over three months" means
nothing if half the data was taken in `long` and half in `debug` - and
making it a session property is the only encoding where no query can
forget to account for it.

Covers the path offline: recorder -> SQLite -> sync agent -> ingest.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest

from tests import support  # noqa: F401

import live

sys.path.insert(0, os.path.join(support.ROOT, "infra"))
from sync import agent as sync_agent          # noqa: E402
from ingest import server as ingest_server    # noqa: E402
from common import wire                       # noqa: E402


class RecorderStoresTheMode(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "rec.db")
        self.rec = live.Recorder(self.db)
        self.rec.open()

    def tearDown(self):
        try:
            self.rec.close()
        except Exception:
            pass

    def _runs(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT id, mode, ended_at FROM runs ORDER BY id"
            ).fetchall()
        finally:
            con.close()

    def test_a_run_records_the_mode_it_was_taken_in(self):
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12, "long")
        time.sleep(0.2)
        self.rec.close()

        self.assertEqual(self._runs()[0][1], "long")

    def test_the_mode_table_version_rides_in_the_mapping_set(self):
        """
        The mode NAME alone does not identify a rate: the table is
        editable config, so `long` before and after an edit are different
        samplings. Which revision is recorded in `mapping_set`, next to
        every mapping file version - one string for the whole sampling
        configuration, rather than a second column to remember to join.
        """
        from bmwdiag.mapping import load_file, MappingRegistry
        from bmwdiag.mapping.registry import AllCapabilities

        registry = MappingRegistry([load_file(support.OBD_MAPPING)])
        profile = registry.resolve(AllCapabilities(), config={})

        self.rec.set_metadata(profile, ["drive-modes@3"])
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12, "long")
        time.sleep(0.2)
        self.rec.close()

        con = sqlite3.connect(self.db)
        try:
            mode, mset = con.execute(
                "SELECT mode, mapping_set FROM runs"
            ).fetchone()
        finally:
            con.close()

        self.assertEqual(mode, "long")
        self.assertIn("drive-modes@3", mset)
        self.assertIn("sae-obd-engine@", mset)
        #: sorted, so the string is stable for a given configuration
        self.assertEqual(mset.split(","), sorted(mset.split(",")))

    def test_switching_mode_starts_a_new_run(self):
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12, "normal")
        time.sleep(0.2)
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12, "debug")
        time.sleep(0.2)
        self.rec.close()

        runs = self._runs()

        self.assertEqual([r[1] for r in runs], ["normal", "debug"])

    def test_the_previous_run_is_closed_not_left_open(self):
        """
        Otherwise a mode switch leaves a session that appears to run until
        the process exits, overlapping the one that replaced it.
        """
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12, "normal")
        time.sleep(0.2)
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12, "debug")
        time.sleep(0.2)
        self.rec.close()

        runs = self._runs()

        self.assertIsNotNone(runs[0][2], "the superseded run has no ended_at")
        self.assertIsNotNone(runs[1][2])

    def test_samples_land_in_the_run_that_was_open(self):
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12, "normal")
        self.rec.write(time.time(), {"rpm": 800.0})
        time.sleep(0.3)
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12, "debug")
        self.rec.write(time.time(), {"rpm": 900.0})
        time.sleep(0.3)
        self.rec.close()

        con = sqlite3.connect(self.db)
        try:
            by_run = dict(con.execute(
                "SELECT run_id, count(*) FROM samples GROUP BY run_id"
            ))
        finally:
            con.close()

        self.assertEqual(by_run, {1: 1, 2: 1})

    def test_the_default_mode_is_recorded_when_none_is_given(self):
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        time.sleep(0.2)
        self.rec.close()

        self.assertEqual(self._runs()[0][1], "normal")


class Shipping(unittest.TestCase):
    def _db(self, path, with_mode=True):
        con = sqlite3.connect(path)
        cols = ("id INTEGER PRIMARY KEY, started_at REAL, ended_at REAL,"
                " vin TEXT, gateway TEXT, ecu TEXT, ecu_addr INTEGER,"
                " mapping_set TEXT")

        if with_mode:
            cols += ", mode TEXT"

        con.execute(f"CREATE TABLE runs({cols})")

        if with_mode:
            con.execute(
                "INSERT INTO runs VALUES(1,1.0,2.0,'V','gw','DDE',18,"
                "'drive-modes@1,sae-obd-engine@2','long')"
            )
        else:
            con.execute(
                "INSERT INTO runs VALUES(1,1.0,2.0,'V','gw','DDE',18,"
                "'sae-obd-engine@1')"
            )

        con.commit()
        con.close()

    def test_the_agent_ships_the_mode(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db(db)

        rows = sync_agent.read_sessions(db, 0)

        self.assertEqual(rows[0]["mode"], "long")
        #: The table revision travels in the mapping set, not its own column.
        self.assertIn("drive-modes@1", rows[0]["mappings"])

    def test_a_database_recorded_before_modes_still_syncs(self):
        """
        An older database has no `runs.mode`. It must sync as unknown -
        not crash, and not be back-filled with a guess.
        """
        db = os.path.join(tempfile.mkdtemp(), "old.db")
        self._db(db, with_mode=False)

        rows = sync_agent.read_sessions(db, 0)

        self.assertEqual(rows[0]["mode"], "")

    def test_the_ingest_server_builds_the_column(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db(db)
        rows = sync_agent.read_sessions(db, 0)

        batch = wire.columnar(
            "sessions",
            [{k: v for k, v in r.items() if not k.startswith("_")}
             for r in rows],
            meta={"db": "t.db"},
        )
        built = ingest_server.build_sessions(batch)

        self.assertEqual(built[0]["mode"], "long")
        self.assertIn("drive-modes@1", built[0]["mappings"])

    def test_the_mode_survives_the_wire_format(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db(db)
        rows = sync_agent.read_sessions(db, 0)
        batch = wire.columnar(
            "sessions",
            [{k: v for k, v in r.items() if not k.startswith("_")}
             for r in rows],
        )

        back = wire.decode(wire.encode(batch))

        self.assertEqual(wire.rows_of(back)[0]["mode"], "long")


class SchemaMatchesTheWriter(unittest.TestCase):
    """The declared schema and the code that writes it must not drift."""

    def _sql(self, *parts):
        with open(os.path.join(support.ROOT, "infra", *parts),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_the_sessions_table_declares_mode(self):
        self.assertIn("mode", self._sql("clickhouse", "init", "001_schema.sql"))

    def test_a_migration_exists_for_already_deployed_lakes(self):
        """
        The init script only runs on a fresh volume, so a column added to
        it is invisible to the deployed lake without a migration.
        """
        path = os.path.join(
            support.ROOT, "infra", "clickhouse", "migrations",
            "2026-08-30_sessions_mode.sql",
        )

        self.assertTrue(os.path.exists(path))

        with open(path, encoding="utf-8") as fh:
            sql = fh.read()

        self.assertIn("ADD COLUMN IF NOT EXISTS mode", sql)
        self.assertIn("telemetry.sessions", sql)


if __name__ == "__main__":
    unittest.main()
