"""
Mapping provenance is a property of the RUN, not of the channel forever.

`params` holds channel identity and is written once, on first sight, with
INSERT OR IGNORE. So `params.mapping_ver` records whichever mapping version
happened to be loaded the first time a database ever saw that channel. Keep
using the same database across a mapping revision - which is the normal case,
telemetry.db lives for months - and every new sample would still ship under
the old version, while being decoded by the new one.

The obvious repair is the wrong one: updating params.mapping_ver in place
would make every historical sample claim it was decoded by a revision that
did not exist when it was recorded. Provenance has to be immutable per run,
so it is recorded per (run, channel) instead.

These tests are written the way the failure actually happens: one database,
reopened across a mapping version bump.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest

from tests import support  # noqa: F401

import live
from bmwdiag.mapping import MappingRegistry, load_text
from bmwdiag.mapping.registry import AllCapabilities

sys.path.insert(0, os.path.join(support.ROOT, "infra"))
from sync import agent as sync_agent          # noqa: E402
from ingest import server as ingest_server    # noqa: E402
from common import wire                       # noqa: E402


def profile_at_version(version: int, unit: str = "rpm"):
    """A one-channel mapping plus one derived channel, at `version`."""
    mapping = load_text(
        "schema_version: 1\n"
        f"mapping: {{id: prov-test, version: {version}, production: false}}\n"
        "ecu: {target: 0x12}\n"
        "requests:\n"
        "  probe:\n"
        "    protocol: uds\n"
        "    service: 0x22\n"
        "    did: 0xDA2E\n"
        "    response: {data_length: 2}\n"
        "    signals:\n"
        f"      x: {{label: X, unit: '{unit}', decode: {{type: uint8}}}}\n"
        "derived:\n"
        "  x_doubled:\n"
        "    label: X doubled\n"
        "    unit: ''\n"
        "    operation: linear\n"
        "    inputs: {value: x}\n"
        "    scale: 2.0\n"
        "    trigger: [x]\n",
        "prov-test",
    )

    return MappingRegistry([mapping]).resolve(AllCapabilities())


class ProvenanceCase(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "tele.db")

    def record(self, version, values, unit="rpm"):
        """Open the db, record one cycle at `version`, close it again."""
        rec = live.Recorder(self.db, chunk=1, interval=0.05)
        rec.set_metadata(profile_at_version(version, unit))
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        time.sleep(0.05)
        rec.write(time.time(), values)
        rec.close()

    def rows(self):
        """Every sample as (channel, value, mapping_ver), sync's own view."""
        return [
            (r["channel_raw"], r["value"], r["mapping_ver"])
            for r in sync_agent.read_samples(self.db, 0, 1000)
        ]


class SameDatabaseAcrossAVersionBump(ProvenanceCase):
    """The regression this table exists for."""

    def setUp(self):
        super().setUp()
        #: v1 records x=1, then the SAME database is reopened under v2.
        self.record(1, {"x": 1.0})
        self.record(2, {"x": 2.0})

    def test_the_channel_row_was_reused_not_duplicated(self):
        #
        # Establishes the precondition. params is keyed UNIQUE on `key`,
        # so the second run reuses the first run's row - which is exactly
        # why its mapping_ver cannot be trusted as the answer.
        #
        con = sqlite3.connect(self.db)

        try:
            params = con.execute(
                "SELECT COUNT(*) FROM params WHERE key = 'x'"
            ).fetchone()[0]
            stored = con.execute(
                "SELECT mapping_ver FROM params WHERE key = 'x'"
            ).fetchone()[0]
        finally:
            con.close()

        self.assertEqual(params, 1)
        self.assertEqual(stored, "1", "params still holds the FIRST version")

    def test_old_samples_resolve_to_v1(self):
        old = [r for r in self.rows() if r[1] == 1.0]

        self.assertEqual(len(old), 1)
        self.assertEqual(old[0][2], "1")

    def test_new_samples_resolve_to_v2(self):
        #
        # The bug in one assertion: without run-scoped provenance this
        # reads "1", because the sample points at a params row written by
        # the earlier run.
        #
        new = [r for r in self.rows() if r[1] == 2.0]

        self.assertEqual(len(new), 1)
        self.assertEqual(new[0][2], "2")

    def test_history_was_not_relabelled(self):
        #
        # The other half. Fixing this by updating params.mapping_ver would
        # pass the test above and silently rewrite the past.
        #
        self.assertEqual(
            sorted((value, ver) for _k, value, ver in self.rows()),
            [(1.0, "1"), (2.0, "2")],
        )

    def test_the_sync_layer_ships_both_versions(self):
        rows = sync_agent.read_samples(self.db, 0, 1000)
        built = ingest_server.build_samples(
            wire.decode(wire.encode(wire.columnar(
                "samples",
                [{k: v for k, v in r.items() if k != "_rowid"} for r in rows],
                meta={"mapping_ver": "99"},
            )))
        )

        #: per-row provenance must beat the coarse batch-level fallback
        self.assertEqual(
            sorted((r["value"], r["mapping_ver"]) for r in built),
            [(1.0, "1"), (2.0, "2")],
        )

    def test_run_channels_holds_one_row_per_run(self):
        con = sqlite3.connect(self.db)

        try:
            rows = con.execute(
                "SELECT run_id, mapping_id, mapping_version FROM run_channels "
                "JOIN params p ON p.id = run_channels.param_id "
                "WHERE p.key = 'x' ORDER BY run_id"
            ).fetchall()
        finally:
            con.close()

        self.assertEqual(
            rows, [(1, "prov-test", "1"), (2, "prov-test", "2")]
        )


class DerivedChannelsGetTheSameGuarantee(ProvenanceCase):
    def setUp(self):
        super().setUp()
        self.record(1, {"x": 1.0, "x_doubled": 2.0})
        self.record(2, {"x": 5.0, "x_doubled": 10.0})

    def test_derived_samples_carry_their_run_version(self):
        derived = sorted(
            (value, ver) for key, value, ver in self.rows()
            if key == "x_doubled"
        )

        self.assertEqual(derived, [(2.0, "1"), (10.0, "2")])


class UnitsAreSnapshottedPerRun(ProvenanceCase):
    def test_a_corrected_unit_does_not_restate_old_samples(self):
        #
        # Same class of defect one column over: params.unit is also
        # written once. A mapping that corrects a unit must not change
        # what earlier samples claim to have been measured in.
        #
        self.record(1, {"x": 1.0}, unit="rpm")
        self.record(2, {"x": 2.0}, unit="1/min")

        by_value = {
            r["value"]: r["unit"] for r in sync_agent.read_samples(self.db, 0, 1000)
        }

        self.assertEqual(by_value, {1.0: "rpm", 2.0: "1/min"})


class PreMigrationDatabase(unittest.TestCase):
    """
    A database recorded before run_channels existed must still sync, and
    must keep the provenance it was written with.
    """

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(self.db)
        con.executescript(
            "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " started_at REAL NOT NULL, ended_at REAL, vin TEXT, gateway TEXT,"
            " ecu TEXT, ecu_addr INTEGER, mapping_set TEXT);"
            "CREATE TABLE params(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " key TEXT UNIQUE, pid INTEGER, label TEXT, unit TEXT,"
            " mapping_ver TEXT);"
            "CREATE TABLE samples(run_id INTEGER, ts REAL, param_id INTEGER,"
            " value REAL);"
        )
        con.execute("INSERT INTO runs(id, started_at, vin) VALUES(1, 1e9, 'V')")
        con.execute(
            "INSERT INTO params(key, unit, mapping_ver) VALUES('x','rpm','7')"
        )
        con.execute("INSERT INTO samples VALUES(1, 1e9, 1, 800.0)")
        con.commit()
        con.close()

    def test_it_reads_without_the_table(self):
        #
        # The agent reads drive files it did not create, including ones
        # copied off a card, so it must not require the migration to have
        # run at all.
        #
        rows = sync_agent.read_samples(self.db, 0, 100)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mapping_ver"], "7")
        self.assertEqual(rows[0]["unit"], "rpm")

    def test_opening_it_adds_the_table_without_backfilling(self):
        #
        # Opening migrates. The old run gets no run_channels row: the only
        # version it could be given is today's, which is precisely the
        # retroactive relabelling this design exists to prevent.
        #
        rec = live.Recorder(self.db)
        rec.open()
        rec.close()

        con = sqlite3.connect(self.db)

        try:
            self.assertTrue(sync_agent._has_table(con, "run_channels"))
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM run_channels").fetchone()[0],
                0,
            )
        finally:
            con.close()

        #: and it still resolves to what it was recorded with
        self.assertEqual(
            sync_agent.read_samples(self.db, 0, 100)[0]["mapping_ver"], "7"
        )

    def test_the_migration_is_idempotent(self):
        for _ in range(3):
            rec = live.Recorder(self.db)
            rec.open()
            rec.close()

        self.assertEqual(
            sync_agent.read_samples(self.db, 0, 100)[0]["mapping_ver"], "7"
        )

    def test_new_runs_in_an_old_database_get_provenance(self):
        #
        # The mixed case: history keeps params.mapping_ver, anything
        # recorded from now on resolves through run_channels.
        #
        rec = live.Recorder(self.db, chunk=1, interval=0.05)
        rec.set_metadata(profile_at_version(9))
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        time.sleep(0.05)
        rec.write(time.time(), {"x": 42.0})
        rec.close()

        by_value = {
            r["value"]: r["mapping_ver"]
            for r in sync_agent.read_samples(self.db, 0, 100)
        }

        self.assertEqual(by_value, {800.0: "7", 42.0: "9"})
