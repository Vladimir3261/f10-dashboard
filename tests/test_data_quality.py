"""
Quality labels: recording *why* a value may not be a measurement.

Before this, three different situations produced the same storage. A real
reading was a row; a sentinel the ECU returned to say "no value" was no
row; a channel nobody polled was also no row. So "the sensor reported
unavailable" and "we never asked" were indistinguishable, and a value
sitting on its sensor's rail was indistinguishable from a real one.

Covers the storage path offline: SQLite schema and migration -> sync agent
read -> ingest builder. The decode side is in test_mapping_decoder.py.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest

from tests import support  # noqa: F401

import live
from bmwdiag.mapping.decoder import QUALITIES

sys.path.insert(0, os.path.join(support.ROOT, "infra"))
from sync import agent as sync_agent          # noqa: E402
from ingest import server as ingest_server    # noqa: E402
from common import wire                       # noqa: E402


class RecorderCase(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "rec.db")
        #
        # A short flush interval so a test does not wait on the 2 s
        # production one; `_settle` still closes to force the last flush.
        #
        self.rec = live.Recorder(self.db, chunk=1, interval=0.05)
        self.rec.open()
        self.rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        time.sleep(0.05)

    def tearDown(self):
        try:
            self.rec.close()
        except Exception:
            pass

    def _settle(self):
        """Close the recorder so every buffered sample is on disk."""
        self.rec.close()

    def _rows(self):
        con = sqlite3.connect(self.db)

        try:
            return con.execute(
                "SELECT p.key, s.value, s.quality FROM samples s "
                "JOIN params p ON p.id = s.param_id ORDER BY s.rowid"
            ).fetchall()
        finally:
            con.close()


class StoringQuality(RecorderCase):
    def test_a_flagged_value_is_stored_not_dropped(self):
        #
        # The whole point. lambda's 0xFFFF decodes to exactly 2.0 and the
        # ECU means "no value" by it. The row exists, carries the number
        # the bytes actually decoded to, and says not to trust it.
        #
        self.rec.write(
            time.time(),
            {"engine.lambda": 2.0, "engine.rpm": 800.0},
            {"engine.lambda": "sentinel"},
        )
        self._settle()

        rows = dict((k, (v, q)) for k, v, q in self._rows())

        self.assertEqual(rows["engine.lambda"], (2.0, "sentinel"))
        self.assertEqual(rows["engine.rpm"], (800.0, "ok"))

    def test_unlabelled_values_record_ok_not_null(self):
        #
        # A caller that passes no labels went through the narrow decode
        # path, which drops everything not usable - so what arrives really
        # is ok. NULL is reserved for "recorded before quality existed".
        #
        self.rec.write(time.time(), {"engine.rpm": 800.0})
        self._settle()

        self.assertEqual(self._rows(), [("engine.rpm", 800.0, "ok")])

    def test_every_label_round_trips(self):
        self.rec.write(
            time.time(),
            {q: float(i) for i, q in enumerate(QUALITIES)},
            {q: q for q in QUALITIES},
        )
        self._settle()

        self.assertEqual(
            {k: q for k, _, q in self._rows()},
            {q: q for q in QUALITIES},
        )


class Migration(unittest.TestCase):
    """A database recorded before quality existed must still open and sync."""

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(self.db)
        con.executescript(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " started_at REAL NOT NULL, ended_at REAL, vin TEXT,"
            " gateway TEXT, ecu TEXT, ecu_addr INTEGER);"
            "CREATE TABLE params (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " key TEXT UNIQUE NOT NULL, pid INTEGER, label TEXT, unit TEXT);"
            "CREATE TABLE samples (run_id INTEGER NOT NULL, ts REAL NOT NULL,"
            " param_id INTEGER NOT NULL, value REAL NOT NULL);"
        )
        con.execute("INSERT INTO runs(started_at, vin) VALUES (1e9, 'VINREDACTED')")
        con.execute("INSERT INTO params(key, unit) VALUES ('engine.rpm', 'rpm')")
        con.execute("INSERT INTO samples VALUES (1, 1e9, 1, 800.0)")
        con.commit()
        con.close()

    def test_the_column_is_added_in_place(self):
        rec = live.Recorder(self.db)
        rec.open()

        try:
            con = sqlite3.connect(self.db)
            cols = {r[1] for r in con.execute("PRAGMA table_info(samples)")}
            con.close()

            self.assertIn("quality", cols)
        finally:
            rec.close()

    def test_pre_quality_rows_survive_and_sync(self):
        #
        # ALTER leaves existing rows NULL. They still ship, reported as
        # 'ok' - which claims "the decoder of the day accepted this", not
        # "this is verified good". The lake's Enum8 has no way to say
        # unknown, and inventing one would need a schema migration to
        # relabel history that DATA_QUALITY.md already describes.
        #
        rec = live.Recorder(self.db)
        rec.open()
        rec.close()

        rows = sync_agent.read_samples(self.db, 0, 100)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quality"], "ok")

    def test_a_database_with_no_quality_column_still_reads(self):
        #
        # The agent must not require the migration to have run: it reads
        # drive files it did not create, including ones copied off a card.
        #
        rows = sync_agent.read_samples(self.db, 0, 100)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quality"], "ok")
        self.assertEqual(rows[0]["value"], 800.0)


class ToTheLake(RecorderCase):
    def test_quality_survives_the_wire_and_the_ingest(self):
        self.rec.write(
            time.time(),
            {"engine.lambda": 2.0, "engine.manifold_pressure.absolute": 255.0},
            {"engine.lambda": "sentinel",
             "engine.manifold_pressure.absolute": "saturated"},
        )
        self._settle()

        rows = sync_agent.read_samples(self.db, 0, 100)
        batch = wire.columnar("samples", [
            {k: v for k, v in r.items() if k != "_rowid"} for r in rows
        ])
        built = ingest_server.build_samples(wire.decode(wire.encode(batch)))

        self.assertEqual(
            {r["channel_raw"]: r["quality"] for r in built},
            {"engine.lambda": "sentinel",
             "engine.manifold_pressure.absolute": "saturated"},
        )

    def test_every_label_is_one_the_lake_accepts(self):
        #
        # ClickHouse drops an unknown COLUMN silently but fails an entire
        # insert batch on an unknown ENUM VALUE. So a label that reaches
        # the ingest and is not in the enum does not lose one row, it
        # stalls the sync. This asserts the producer side of that
        # contract; the enum itself is pinned in test_mapping_decoder.
        #
        self.rec.write(
            time.time(),
            {q: float(i) for i, q in enumerate(QUALITIES)},
            {q: q for q in QUALITIES},
        )
        self._settle()

        rows = sync_agent.read_samples(self.db, 0, 100)
        built = ingest_server.build_samples(
            wire.columnar("samples", [
                {k: v for k, v in r.items() if k != "_rowid"} for r in rows
            ])
        )

        for row in built:
            self.assertIn(row["quality"], QUALITIES)


class ThroughTheExecutor(RecorderCase):
    """
    The runtime path: a flagged reading is stored, and only stored.

    The narrow view stays exactly what it was, which is what keeps the
    dashboard from suddenly displaying a sentinel as a measurement.
    """

    def _profile(self):
        from bmwdiag.mapping import load_text, MappingRegistry
        from bmwdiag.mapping.registry import AllCapabilities

        mapping = load_text(
            "schema_version: 1\n"
            "mapping: {id: t, version: 1, production: false}\n"
            "ecu: {target: 0x12}\n"
            "requests:\n"
            "  probe:\n"
            "    protocol: uds\n"
            "    service: 0x22\n"
            "    did: 0xDA2E\n"
            "    response: {data_length: 3}\n"
            "    signals:\n"
            "      good:  {label: G, unit: '', decode: {type: uint8, offset: 0}}\n"
            "      gone:  {label: S, unit: '', decode: {type: uint8, offset: 1,"
            " invalid: [255]}}\n"
            "      rail:  {label: R, unit: '', decode: {type: uint8, offset: 2,"
            " saturated: [255]}}\n",
            "test",
        )

        return MappingRegistry([mapping]).resolve(AllCapabilities())

    def _executor(self, profile):
        class Answering:
            def request(self, payload, *, dst, timeout=None):
                #: good = 10, gone = the sentinel, rail = pinned
                return bytes([0x62, 0xDA, 0x2E, 10, 255, 255])

        return live.MappingExecutor(profile, transport=Answering())

    def test_the_narrow_view_is_unchanged(self):
        profile = self._profile()
        values = self._executor(profile).execute(profile.requests)

        self.assertEqual(values, {"good": 10.0})

    def test_the_reading_view_labels_all_three(self):
        profile = self._profile()
        readings = self._executor(profile).execute_readings(profile.requests)

        self.assertEqual(
            {k: r.quality for k, r in readings.items()},
            {"good": "ok", "gone": "sentinel", "rail": "saturated"},
        )
        self.assertEqual(readings["gone"].value, 255.0)

    def test_a_flagged_reading_reaches_storage_with_its_reason(self):
        #
        # The defect in one assertion. Before this, `gone` and `rail`
        # produced no rows at all and were indistinguishable from channels
        # nobody polled.
        #
        profile = self._profile()
        readings = self._executor(profile).execute_readings(profile.requests)

        self.rec.write(
            time.time(),
            {k: r.value for k, r in readings.items()},
            {k: r.quality for k, r in readings.items()},
        )
        self._settle()

        self.assertEqual(
            {k: (v, q) for k, v, q in self._rows()},
            {"good": (10.0, "ok"),
             "gone": (255.0, "sentinel"),
             "rail": (255.0, "saturated")},
        )
