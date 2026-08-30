"""
The host clock, and why a run must never span a correction.

The Pi has no RTC. On 2026-08-29 it booted with a stale clock, started
recording, and systemd-timesyncd corrected the clock forward by ~76.5
minutes 47 seconds in. The result was a session containing a phantom
4578-second gap, a fictitious 5064-second duration for eight real
minutes, and samples stamped 76 minutes in the past - all shipped to the
lake that way.

That is worse than a bad value. A bad value is one wrong number; a bad
clock corrupts every rate, gradient and trend derived from the data,
which is the whole premise of the long-term model.

Three defences are tested here: waiting at startup, labelling the run,
and detecting a step mid-run. Offline - the clock is never actually
changed, only the functions that read it.
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


class Detection(unittest.TestCase):
    def test_the_anchor_is_stable_while_the_clock_only_drifts(self):
        """
        `time.time() - time.monotonic()` is constant unless the wall
        clock is stepped. That is the whole detection mechanism.
        """
        first = live.clock_anchor()
        time.sleep(0.05)
        second = live.clock_anchor()

        self.assertLess(abs(second - first), live.CLOCK_STEP_THRESHOLD)

    def test_the_threshold_is_above_ordinary_slew(self):
        """
        NTP slews small corrections gradually and only steps large ones.
        A threshold under a second would fire on normal adjustment.
        """
        self.assertGreaterEqual(live.CLOCK_STEP_THRESHOLD, 1.0)
        #: And far below the failure it exists to catch (76.5 minutes).
        self.assertLess(live.CLOCK_STEP_THRESHOLD, 60.0)

    def test_the_2026_08_29_jump_would_be_caught(self):
        anchor = live.clock_anchor()
        stepped = anchor + 76.5 * 60

        self.assertGreater(
            abs(stepped - anchor), live.CLOCK_STEP_THRESHOLD
        )


class SyncProbe(unittest.TestCase):
    def setUp(self):
        self._stamp = live.TIMESYNC_STAMP
        self._run = getattr(live, "subprocess").run

    def tearDown(self):
        live.TIMESYNC_STAMP = self._stamp
        live.subprocess.run = self._run

    def test_the_timesyncd_stamp_is_a_positive_answer(self):
        with tempfile.NamedTemporaryFile() as stamp:
            live.TIMESYNC_STAMP = stamp.name

            self.assertTrue(live.clock_is_synced())

    def test_an_unknown_clock_reads_as_not_synced(self):
        """
        Both probes failing means "cannot tell". Reporting that as
        synced is exactly the assumption that shipped a broken timeline,
        so unknown must fail closed.
        """
        live.TIMESYNC_STAMP = "/nonexistent/timesync/synchronized"

        def boom(*a, **k):
            raise OSError("no timedatectl here")

        live.subprocess.run = boom

        self.assertFalse(live.clock_is_synced())

    def test_timedatectl_yes_is_accepted(self):
        live.TIMESYNC_STAMP = "/nonexistent/timesync/synchronized"

        class Proc:
            returncode = 0
            stdout = "yes\n"

        live.subprocess.run = lambda *a, **k: Proc()

        self.assertTrue(live.clock_is_synced())

    def test_timedatectl_no_is_not(self):
        live.TIMESYNC_STAMP = "/nonexistent/timesync/synchronized"

        class Proc:
            returncode = 0
            stdout = "no\n"

        live.subprocess.run = lambda *a, **k: Proc()

        self.assertFalse(live.clock_is_synced())


class StartupWait(unittest.TestCase):
    def setUp(self):
        self._synced = live.clock_is_synced

    def tearDown(self):
        live.clock_is_synced = self._synced

    def test_an_already_synced_clock_does_not_wait(self):
        live.clock_is_synced = lambda: True
        started = time.monotonic()

        self.assertTrue(live.wait_for_clock(5.0, report=lambda *_: None))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_the_wait_is_bounded_and_not_fatal(self):
        """
        A car parked out of network range must still record. An honestly
        labelled bad-clock run is worth more than no run at all.
        """
        live.clock_is_synced = lambda: False
        started = time.monotonic()

        result = live.wait_for_clock(0.3, report=lambda *_: None)

        self.assertFalse(result)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_zero_disables_the_wait(self):
        live.clock_is_synced = lambda: False
        started = time.monotonic()

        live.wait_for_clock(0, report=lambda *_: None)

        self.assertLess(time.monotonic() - started, 0.2)


class RecordedOnTheRun(unittest.TestCase):
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
                "SELECT id, clock_synced FROM runs ORDER BY id"
            ).fetchall()
        finally:
            con.close()

    def test_a_synced_run_is_marked_synced(self):
        self.rec.start_run("V", "gw", "DDE", 0x12, "normal", True)
        time.sleep(0.2)
        self.rec.close()

        self.assertEqual(self._runs()[0][1], 1)

    def test_an_unsynced_run_is_marked_unsynced(self):
        self.rec.start_run("V", "gw", "DDE", 0x12, "normal", False)
        time.sleep(0.2)
        self.rec.close()

        self.assertEqual(self._runs()[0][1], 0)

    def test_unknown_is_stored_as_null_not_guessed(self):
        """
        NULL and 0 mean different things: "nobody asked" versus "asked,
        and the answer was no". Collapsing them would hide which runs
        predate the check.
        """
        self.rec.start_run("V", "gw", "DDE", 0x12, "normal")
        time.sleep(0.2)
        self.rec.close()

        self.assertIsNone(self._runs()[0][1])

    def test_a_step_starts_a_new_run_so_none_spans_the_discontinuity(self):
        """
        The invariant the fix buys: samples either side of a clock
        correction are never in the same run, so nothing stitches a bad
        timeline to a good one.
        """
        self.rec.start_run("V", "gw", "DDE", 0x12, "normal", False)
        self.rec.write(time.time(), {"rpm": 800.0})
        time.sleep(0.3)

        #: what the poll loop does on detecting a step
        self.rec.event("clock", "clock stepped +4590.0s")
        self.rec.start_run("V", "gw", "DDE", 0x12, "normal", True)
        self.rec.write(time.time(), {"rpm": 900.0})
        time.sleep(0.3)
        self.rec.close()

        runs = self._runs()

        self.assertEqual([r[1] for r in runs], [0, 1])

        con = sqlite3.connect(self.db)
        try:
            by_run = dict(con.execute(
                "SELECT run_id, count(*) FROM samples GROUP BY run_id"
            ))
            ended = con.execute(
                "SELECT ended_at FROM runs WHERE id = 1"
            ).fetchone()[0]
            events = [
                r[0] for r in con.execute(
                    "SELECT message FROM events WHERE kind = 'clock'"
                )
            ]
        finally:
            con.close()

        self.assertEqual(by_run, {1: 1, 2: 1})
        self.assertIsNotNone(ended, "the pre-step run was left open")
        self.assertTrue(any("stepped" in m for m in events))


class ReachesTheLake(unittest.TestCase):
    def _db(self, path, clock_col=True, value=1):
        con = sqlite3.connect(path)
        cols = ("id INTEGER PRIMARY KEY, started_at REAL, ended_at REAL,"
                " vin TEXT, gateway TEXT, ecu TEXT, ecu_addr INTEGER,"
                " mapping_set TEXT, mode TEXT")

        if clock_col:
            cols += ", clock_synced INTEGER"

        con.execute(f"CREATE TABLE runs({cols})")
        row = "1,1.0,2.0,'V','gw','DDE',18,'sae-obd-engine@3','normal'"
        con.execute(
            f"INSERT INTO runs VALUES({row}"
            + (f",{'NULL' if value is None else value})" if clock_col else ")")
        )
        con.commit()
        con.close()

    def test_the_agent_ships_the_flag(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db(db, value=1)

        self.assertEqual(
            sync_agent.read_sessions(db, 0)[0]["clock_synced"], 1
        )

    def test_an_older_database_ships_unknown(self):
        db = os.path.join(tempfile.mkdtemp(), "old.db")
        self._db(db, clock_col=False)

        self.assertIsNone(
            sync_agent.read_sessions(db, 0)[0]["clock_synced"]
        )

    def test_the_ingest_server_keeps_null_as_null(self):
        """Never coerced to 0 or 1 - unknown must stay unknown."""
        from common import wire

        db = os.path.join(tempfile.mkdtemp(), "old.db")
        self._db(db, clock_col=False)
        rows = sync_agent.read_sessions(db, 0)

        batch = wire.columnar(
            "sessions",
            [{k: v for k, v in r.items() if not k.startswith("_")}
             for r in rows],
        )

        self.assertIsNone(
            ingest_server.build_sessions(batch)[0]["clock_synced"]
        )

    def test_the_ingest_server_passes_a_real_flag_through(self):
        from common import wire

        db = os.path.join(tempfile.mkdtemp(), "t.db")
        self._db(db, value=0)
        rows = sync_agent.read_sessions(db, 0)
        batch = wire.columnar(
            "sessions",
            [{k: v for k, v in r.items() if not k.startswith("_")}
             for r in rows],
        )

        self.assertEqual(
            ingest_server.build_sessions(batch)[0]["clock_synced"], 0
        )


class DeploymentPreventsIt(unittest.TestCase):
    """The systemd side, which stops the common case ever arising."""

    def _unit(self, name):
        path = os.path.join(support.ROOT, "hardware", "raspberry-pi",
                            "f10pi", "systemd", name)

        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_the_recorder_is_ordered_after_time_sync(self):
        unit = self._unit("f10-dashboard.service")

        self.assertIn("After=time-sync.target", unit)
        self.assertIn("Wants=time-sync.target", unit)

    def test_it_only_wants_time_sync_and_does_not_require_it(self):
        """
        `Requires` would mean a car parked out of network range never
        records at all. The run is labelled instead.
        """
        unit = self._unit("f10-dashboard.service")

        self.assertNotIn("Requires=time-sync.target", unit)

    def test_the_sync_agent_is_ordered_too(self):
        self.assertIn("time-sync.target", self._unit("f10-sync.service"))

    def test_a_migration_exists_for_the_deployed_lake(self):
        path = os.path.join(
            support.ROOT, "infra", "clickhouse", "migrations",
            "2026-08-30_sessions_clock.sql",
        )

        self.assertTrue(os.path.exists(path))

        with open(path, encoding="utf-8") as fh:
            sql = fh.read()

        self.assertIn("ADD COLUMN IF NOT EXISTS clock_synced", sql)
        #: Existing rows must NOT be back-filled as trustworthy.
        self.assertNotIn("UPDATE", sql.upper().replace("ALTER", ""))


if __name__ == "__main__":
    unittest.main()
