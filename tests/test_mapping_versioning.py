"""
Mapping data versioning: the loader requirement, per-channel version
resolution, the run manifest the recorder writes, the VERSIONS.lock
enforcement, and the version stamp flowing through sync to the lake.

All offline - no car, no network, no ClickHouse. See docs/DATA_VERSIONING.md.
"""

import os
import pathlib
import sqlite3
import tempfile
import time
import unittest

from tests import support  # noqa: F401

from bmwdiag.mapping.errors import InvalidFieldError, MissingFieldError
from bmwdiag.mapping.loader import load_text, load_tree, load_file
from bmwdiag.mapping.registry import MappingRegistry, AllCapabilities
from bmwdiag.mapping import modes
from bmwdiag.mapping import versioning


BASE = """
schema_version: 1

mapping:
  id: ver-fixture
  version: __VER__
  production: false

ecu:
  family: test
  target: 0x7E

requests:
  probe:
    protocol: obd
    service: 0x01
    pid: 0x0C
    response: {data_length: 2}
    signals:
      alpha:
        label: Alpha
        unit: rpm
        decode: {type: uint16_be, divide: 4.0}
"""


def _fixture(ver="1"):
    return BASE.replace("__VER__", ver)


class LoaderRequiresVersion(unittest.TestCase):
    def test_integer_version_loads(self):
        m = load_text(_fixture("3"), "test")
        self.assertEqual(m.version, 3)
        self.assertIsInstance(m.version, int)

    def test_quoted_digit_version_coerces_to_int(self):
        m = load_text(_fixture('"2"'), "test")
        self.assertEqual(m.version, 2)

    def test_missing_version_is_rejected(self):
        text = BASE.replace("  version: __VER__\n", "")
        with self.assertRaises(MissingFieldError):
            load_text(text, "test")

    def test_zero_version_is_rejected(self):
        with self.assertRaises(InvalidFieldError):
            load_text(_fixture("0"), "test")

    def test_negative_version_is_rejected(self):
        with self.assertRaises(InvalidFieldError):
            load_text(_fixture("-1"), "test")

    def test_non_numeric_version_is_rejected(self):
        with self.assertRaises(InvalidFieldError):
            load_text(_fixture("abc"), "test")


class ChannelVersionResolution(unittest.TestCase):
    def setUp(self):
        self.reg = MappingRegistry()
        for m in load_tree(support.MAPPINGS, production_only=False):
            self.reg.add(m)
        self.profile = self.reg.resolve(AllCapabilities(), config={})

    #
    # These assert PROPAGATION - that a channel carries the version of
    # the file that defines it - not any particular number. The numbers
    # themselves are pinned in mappings/VERSIONS.lock and checked by
    # LockfileEnforcement below; repeating them here would only mean
    # editing this file on every bump without testing anything more.
    #
    def engine_version(self):
        engine = next(
            m for m in self.reg.mappings if m.id == "sae-obd-engine"
        )

        return engine.version

    def test_read_signal_inherits_its_file_version(self):
        # rpm is read from the production OBD engine mapping.
        self.assertEqual(
            self.profile.channel_version("rpm"), self.engine_version()
        )

    def test_derived_channel_has_a_version(self):
        # boost is a derived channel in the same file, so same version.
        self.assertEqual(
            self.profile.channel_version("boost"), self.engine_version()
        )

    def test_unknown_channel_has_no_version(self):
        self.assertIsNone(self.profile.channel_version("nonexistent_channel"))

    def test_manifest_is_deduplicated_and_carries_versions(self):
        manifest = self.profile.mapping_manifest()
        ids = [m["id"] for m in manifest]
        self.assertEqual(len(ids), len(set(ids)))            # no dupes
        self.assertTrue(all(m["version"] >= 1 for m in manifest))
        engine = next(m for m in manifest if m["id"] == "sae-obd-engine")
        self.assertTrue(engine["production"])

    def test_mapping_set_is_sorted_id_at_version(self):
        s = self.profile.mapping_set()
        self.assertIn(f"sae-obd-engine@{self.engine_version()}", s)
        parts = s.split(",")
        self.assertEqual(parts, sorted(parts))               # deterministic


class LockfileEnforcement(unittest.TestCase):
    def test_repo_lockfile_matches_disk(self):
        """
        The committed mappings/VERSIONS.lock must match the mapping files
        on disk. If this fails, a mapping's version/path changed without
        the lock being regenerated - run:
            python3 -m bmwdiag.mapping lock mappings/
        and review (and bump versions for any content change).
        """
        lock_path = os.path.join(support.MAPPINGS, versioning.LOCK_NAME)
        self.assertTrue(os.path.exists(lock_path), "VERSIONS.lock is missing")

        on_disk = versioning.build_lock([support.MAPPINGS], modes.DEFAULT_MODE_CONFIG)
        locked = versioning.load_lock(lock_path)
        problems = versioning.diff_lock(on_disk, locked)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_diff_detects_a_version_bump(self):
        on_disk = versioning.build_lock([support.MAPPINGS], modes.DEFAULT_MODE_CONFIG)
        locked = versioning.load_lock(
            os.path.join(support.MAPPINGS, versioning.LOCK_NAME)
        )
        # simulate a bumped-but-not-relocked file
        bumped = {"mappings": [dict(e) for e in on_disk["mappings"]]}
        bumped["mappings"][0]["version"] += 1
        problems = versioning.diff_lock(bumped, locked)
        self.assertTrue(any("version mismatch" in p for p in problems))


class RecorderWritesTheManifest(unittest.TestCase):
    def setUp(self):
        import live
        self.live = live
        self.reg = MappingRegistry()
        for m in load_tree(support.MAPPINGS, production_only=False):
            self.reg.add(m)
        self.profile = self.reg.resolve(AllCapabilities(), config={})
        self.db = os.path.join(tempfile.mkdtemp(), "rec.db")

    def _record(self):
        rec = self.live.Recorder(self.db)
        rec.set_metadata(self.profile)
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        time.sleep(0.05)
        rec.write(time.time(), {"rpm": 800.0, "boost": 1.1})
        time.sleep(0.05)
        rec.close()

    def test_run_mappings_and_versions_are_recorded(self):
        self._record()
        con = sqlite3.connect(self.db)
        try:
            mset = con.execute("SELECT mapping_set FROM runs").fetchone()[0]
            rm = con.execute(
                "SELECT mapping_id, version, production FROM run_mappings"
            ).fetchall()
            params = dict(con.execute(
                "SELECT key, mapping_ver FROM params"
            ).fetchall())
        finally:
            con.close()

        engine = next(
            m for m in self.reg.mappings if m.id == "sae-obd-engine"
        )

        self.assertIn(f"sae-obd-engine@{engine.version}", mset)
        self.assertTrue(len(rm) >= 1)
        self.assertTrue(all(v >= 1 for _, v, _ in rm))
        self.assertEqual(params["rpm"], str(engine.version))
        self.assertEqual(params["boost"], str(engine.version))


class RunOneCarriesItsProvenance(unittest.TestCase):
    """
    THE FIRST run of a recorder must carry its mapping provenance.

    It did not, on the car path, for weeks. `poll_loop` called
    `start_run()` forty-nine lines before `set_metadata()`, so the
    recorder thread read `meta_source` while it was still None and wrote
    an empty `mapping_set` with no `run_mappings` rows.

    Nothing caught it because the demo path has the two calls the right
    way round, and every existing test used a recorder set up demo-style.
    The pattern on disk was exactly `X...` - run 1 empty, the rest fine -
    which while drives fragmented into 4-6 runs cost ~18% of runs and
    left the provenance in the others. Fixing the fragmentation made a
    drive one run, and the loss went to 100%.

    So these tests assert it for run ONE specifically, and for the case
    where metadata was never set at all.
    """

    def setUp(self):
        import live
        self.live = live
        self.reg = MappingRegistry()
        for m in load_tree(support.MAPPINGS, production_only=False):
            self.reg.add(m)
        self.profile = self.reg.resolve(AllCapabilities(), config={})
        self.db = os.path.join(tempfile.mkdtemp(), "rec.db")

    def _read(self):
        con = sqlite3.connect(self.db)
        try:
            runs = con.execute(
                "SELECT id, mapping_set FROM runs ORDER BY id"
            ).fetchall()
            rm = con.execute(
                "SELECT run_id, count(*) FROM run_mappings GROUP BY run_id"
            ).fetchall()
        finally:
            con.close()

        return runs, dict(rm)

    def test_run_one_records_its_mapping_set(self):
        rec = self.live.Recorder(self.db)
        rec.set_metadata(self.profile)
        rec.open()
        rec.start_run("V", "gw", "DDE", 0x12)
        time.sleep(0.2)
        rec.close()

        runs, rm = self._read()

        self.assertEqual(runs[0][0], 1)
        self.assertTrue(runs[0][1], "run 1 has an empty mapping_set")
        self.assertIn("sae-obd-engine@", runs[0][1])
        self.assertGreater(rm.get(1, 0), 0, "run 1 has no run_mappings rows")

    def test_every_run_carries_it_not_just_the_later_ones(self):
        """
        The old bug was invisible from run 2 onward. Assert across a
        sequence, the way a drive with mode switches produces one.
        """
        rec = self.live.Recorder(self.db)
        rec.set_metadata(self.profile)
        rec.open()

        for mode in ("normal", "debug", "long"):
            rec.start_run("V", "gw", "DDE", 0x12, mode)
            time.sleep(0.15)

        rec.close()

        runs, rm = self._read()

        self.assertEqual(len(runs), 3)

        for run_id, mapping_set in runs:
            with self.subTest(run=run_id):
                self.assertTrue(mapping_set, f"run {run_id} lost provenance")
                self.assertGreater(rm.get(run_id, 0), 0)

    def test_provenance_is_snapshot_when_the_run_opens(self):
        """
        Not looked up later by the writer thread. Metadata replaced after
        the run is enqueued must not change what that run recorded -
        otherwise the value depends on thread timing.
        """
        rec = self.live.Recorder(self.db)
        rec.set_metadata(self.profile)
        rec.open()
        rec.start_run("V", "gw", "DDE", 0x12)
        #: Immediately swap it for something else, before the writer runs.
        rec.set_metadata(
            MappingRegistry().resolve(AllCapabilities(), config={})
        )
        time.sleep(0.3)
        rec.close()

        runs, _ = self._read()

        self.assertIn("sae-obd-engine@", runs[0][1])

    def test_opening_a_run_with_no_metadata_says_so(self):
        """
        It stays permitted - a recorder can legitimately be used without
        a profile - but it must not be silent, because a run with no
        provenance looks healthy until someone tries to attribute the
        data months later.
        """
        import contextlib
        import io

        rec = self.live.Recorder(self.db)
        rec.open()
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            rec.start_run("V", "gw", "DDE", 0x12)

        time.sleep(0.2)
        rec.close()

        self.assertIn("NO mapping provenance", out.getvalue())


class PollLoopSetsMetadataFirst(unittest.TestCase):
    """
    The ordering invariant inside `poll_loop`, asserted structurally.

    The Recorder tests above cover the recorder. They cannot cover this:
    the bug was that `poll_loop` called `start_run()` before
    `set_metadata()`, and reaching that line needs a gateway, an ECU scan
    and a resolved profile. The demo path had the two the right way
    round, which is exactly why every existing test passed while the car
    path shipped empty provenance for weeks.

    So this reads the source. That is unusual and worth justifying: the
    invariant is real, it is cheap to state here, and the alternative is
    either no coverage at all or a fake vehicle stack built to assert one
    line of ordering.
    """

    def _calls_in(self, function_name):
        """Recorder method names called in `function_name`, in order."""
        import ast

        source = pathlib.Path(
            os.path.join(support.ROOT, "live.py")
        ).read_text()

        tree = ast.parse(source)
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == function_name
        )

        return [
            node.func.attr
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "rec"
        ]

    def test_the_car_path_sets_metadata_before_opening_a_run(self):
        calls = self._calls_in("poll_loop")

        self.assertIn("set_metadata", calls)
        self.assertIn("start_run", calls)
        self.assertLess(
            calls.index("set_metadata"), calls.index("start_run"),
            "poll_loop opens a run before it knows what it is recording; "
            "that run's mapping_set will be empty",
        )

    def test_the_demo_path_does_too(self):
        calls = self._calls_in("demo_loop")

        self.assertLess(
            calls.index("set_metadata"), calls.index("start_run")
        )


class VersionFlowsToTheLake(unittest.TestCase):
    """The per-channel version reaches the ingest builders through sync."""

    def _versioned_db(self, path):
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL,"
            " ended_at REAL, vin TEXT, gateway TEXT, ecu TEXT, ecu_addr INTEGER,"
            " mapping_set TEXT);"
            "CREATE TABLE params(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE,"
            " pid INTEGER, label TEXT, unit TEXT, mapping_ver TEXT);"
            "CREATE TABLE samples(run_id INTEGER, ts REAL, param_id INTEGER, value REAL);"
        )
        con.execute("INSERT INTO runs(id,started_at,vin,ecu,ecu_addr,mapping_set)"
                    " VALUES(1,1.0,'V','DDE',18,'sae-obd-engine@1')")
        cur = con.execute("INSERT INTO params(key,unit,mapping_ver) VALUES('rpm','rpm','1')")
        pid = cur.lastrowid
        con.execute("INSERT INTO samples VALUES(1, 1.5, ?, 800.0)", (pid,))
        con.commit()
        con.close()

    def setUp(self):
        import sys
        sys.path.insert(0, os.path.join(support.ROOT, "infra"))
        from sync import agent as sync_agent
        from ingest import server as ingest_server
        from common import wire
        self.agent, self.ingest, self.wire = sync_agent, ingest_server, wire
        self.db = os.path.join(tempfile.mkdtemp(), "tele.db")
        self._versioned_db(self.db)

    def test_read_samples_carries_per_channel_version(self):
        rows = self.agent.read_samples(self.db, 0, 100)
        self.assertEqual(rows[0]["mapping_ver"], "1")

    def test_ingest_prefers_per_row_version_over_meta(self):
        rows = self.agent.read_samples(self.db, 0, 100)
        batch = self.wire.columnar("samples", rows, meta={"mapping_ver": "99"})
        built = self.ingest.build_samples(batch)
        # per-row "1" must win over the coarse meta "99"
        self.assertEqual(built[0]["mapping_ver"], "1")

    def test_sessions_carry_the_mapping_set(self):
        rows = self.agent.read_sessions(self.db, 0)
        self.assertEqual(rows[0]["mappings"], "sae-obd-engine@1")
        batch = self.wire.columnar("sessions", rows, meta={"db": "tele.db"})
        built = self.ingest.build_sessions(batch)
        self.assertEqual(built[0]["mappings"], "sae-obd-engine@1")


if __name__ == "__main__":
    unittest.main()
