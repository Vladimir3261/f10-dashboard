"""
Durable session identity, physical trips, and vehicle events.

Three problems that share one root: the storage model described how data
was ACQUIRED, not what the car did.

  * identity came from the filename, so renaming a database changed the
    identity of every run in it and two files sharing a basename merged
    into one session;
  * a run is one HSFZ connection, and a drive is usually several - drive
    11 recorded as four - so every longitudinal question was asking about
    the wrong unit;
  * nothing recorded that the oil was changed, so a baseline could span a
    maintenance event and pool two different cars.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest

from tests import support  # noqa: F401

import live
from analysis import session_report
from analysis.trips import MAX_TRIP_GAP_S, RunRow, group_trips, load_runs
from bmwdiag.identity import is_ulid, new_ulid, session_id_from_ulid
from bmwdiag.vehicle import (
    VehicleEvent,
    baseline_is_valid_across,
    events_between,
    load_events,
)

sys.path.insert(0, os.path.join(support.ROOT, "infra"))
from sync import agent as sync_agent          # noqa: E402

EXAMPLE = os.path.join(support.ROOT, "config", "vehicle-events.example.yaml")


class TheIdentifier(unittest.TestCase):
    def test_it_is_unique(self):
        self.assertEqual(len({new_ulid() for _ in range(500)}), 500)

    def test_it_sorts_by_creation_time(self):
        early = new_ulid(now_ms=1_000_000_000_000)
        late = new_ulid(now_ms=2_000_000_000_000)

        self.assertLess(early, late)

    def test_it_uses_an_unambiguous_alphabet(self):
        #
        # Crockford base32 omits I, L, O and U so an id cannot be misread
        # aloud or mistyped into a DIFFERENT VALID id.
        #
        for letter in "ILOU":
            self.assertNotIn(letter, new_ulid(randomness=b"\xff" * 10))

    def test_the_numeric_derivation_is_stable(self):
        uid = new_ulid()

        self.assertEqual(session_id_from_ulid(uid), session_id_from_ulid(uid))

    def test_different_ids_derive_different_keys(self):
        keys = {session_id_from_ulid(new_ulid()) for _ in range(500)}

        self.assertEqual(len(keys), 500)

    def test_junk_is_not_a_ulid(self):
        for bad in ("", "nope", "I" * 26, new_ulid()[:-1]):
            self.assertFalse(is_ulid(bad))


class IdentityIsIndependentOfTheFile(unittest.TestCase):
    """The defect: identity was a function of where the data was stored."""

    def _db(self, name):
        path = os.path.join(tempfile.mkdtemp(), name)
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12, clock_synced=True)
        time.sleep(0.05)
        rec.write(time.time(), {"coolant": 80.0})
        rec.close()

        return path

    def test_a_run_gets_a_durable_id(self):
        con = sqlite3.connect(self._db("tele.db"))

        try:
            uid = con.execute("SELECT session_uid FROM runs").fetchone()[0]
        finally:
            con.close()

        self.assertTrue(is_ulid(uid), uid)

    def test_renaming_the_database_does_not_change_identity(self):
        #
        # The regression. Under the old scheme every run in a renamed file
        # got a new id, so a re-sync duplicated the entire history.
        #
        path = self._db("tele.db")
        before = sync_agent.read_sessions(path, 0)[0]["session_id"]

        renamed = os.path.join(os.path.dirname(path), "telemetry-old.db")
        os.rename(path, renamed)
        after = sync_agent.read_sessions(renamed, 0)[0]["session_id"]

        self.assertEqual(before, after)

    def test_copying_the_database_does_not_change_identity(self):
        import shutil

        path = self._db("tele.db")
        before = sync_agent.read_sessions(path, 0)[0]["session_id"]

        copy = os.path.join(tempfile.mkdtemp(), "another-name.db")
        shutil.copy(path, copy)

        self.assertEqual(sync_agent.read_sessions(copy, 0)[0]["session_id"],
                         before)

    def test_identical_basenames_do_not_collide(self):
        #
        # Two drive files both called drive.db used to merge into one
        # session in the lake, silently combining two different drives.
        #
        a = self._db("drive.db")
        b = self._db("drive.db")

        self.assertNotEqual(os.path.dirname(a), os.path.dirname(b))
        self.assertNotEqual(
            sync_agent.read_sessions(a, 0)[0]["session_id"],
            sync_agent.read_sessions(b, 0)[0]["session_id"],
        )

    def test_samples_and_sessions_agree_on_the_id(self):
        #
        # They are derived independently; if they ever disagreed the
        # samples would orphan from their session.
        #
        path = self._db("tele.db")

        self.assertEqual(
            sync_agent.read_samples(path, 0, 10)[0]["session_id"],
            sync_agent.read_sessions(path, 0)[0]["session_id"],
        )

    def test_the_uid_itself_reaches_the_lake_payload(self):
        path = self._db("tele.db")
        row = sync_agent.read_sessions(path, 0)[0]

        self.assertTrue(is_ulid(row["session_uid"]))

    def test_a_legacy_database_keeps_its_filename_identity(self):
        #
        # Deliberate. Those sessions are already in the lake under the old
        # derivation; re-deriving would not correct them, it would
        # duplicate them.
        #
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " started_at REAL, ended_at REAL, vin TEXT, gateway TEXT,"
            " ecu TEXT, ecu_addr INTEGER);"
            "CREATE TABLE params(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " key TEXT UNIQUE, pid INTEGER, label TEXT, unit TEXT);"
            "CREATE TABLE samples(run_id INTEGER, ts REAL, param_id INTEGER,"
            " value REAL);"
        )
        con.execute("INSERT INTO runs(id, started_at, vin) VALUES(1,1e9,'V')")
        con.commit()
        con.close()

        row = sync_agent.read_sessions(path, 0)[0]

        self.assertEqual(row["session_uid"], "")
        self.assertEqual(
            row["session_id"], sync_agent.global_session_id(path, 1)
        )


class TripsGroupRunsIntoDrives(unittest.TestCase):
    def _run(self, run_id, started, ended, boot="boot1", hw="dpf=absent",
             clock=1, uid=None):
        return RunRow(run_id=run_id, session_uid=uid or f"UID{run_id:023d}",
                      started=started, ended=ended, boot_id=boot,
                      mode="normal", vehicle_hardware=hw, clock_synced=clock)

    def test_a_reconnect_does_not_start_a_new_drive(self):
        #
        # Drive 11's real shape: four runs split by link faults, gaps of
        # about five seconds. One physical trip.
        #
        trips = group_trips([
            self._run(1, 1000, 1300),
            self._run(2, 1305, 1600),
            self._run(3, 1608, 1900),
        ])

        self.assertEqual(len(trips), 1)
        self.assertEqual([r.run_id for r in trips[0].runs], [1, 2, 3])

    def test_a_long_gap_starts_a_new_drive(self):
        trips = group_trips([
            self._run(1, 1000, 1300),
            self._run(2, 1300 + MAX_TRIP_GAP_S + 1, 2000),
        ])

        self.assertEqual(len(trips), 2)
        self.assertIn("gap of", trips[1].reason)

    def test_a_different_boot_always_splits(self):
        #
        # Stronger than any gap and independent of the clock: the recorder
        # was power-cycled, so the car was switched off.
        #
        trips = group_trips([
            self._run(1, 1000, 1300),
            self._run(2, 1305, 1600, boot="boot2"),
        ])

        self.assertEqual(len(trips), 2)
        self.assertEqual(trips[1].reason, "different host boot")

    def test_a_configuration_change_splits(self):
        trips = group_trips([
            self._run(1, 1000, 1300, hw="dpf=present"),
            self._run(2, 1305, 1600, hw="dpf=absent"),
        ])

        self.assertEqual(trips[1].reason, "vehicle configuration changed")

    def test_an_undisciplined_clock_splits_rather_than_guesses(self):
        #
        # A gap is a timestamp difference. On a run whose clock stepped it
        # is not evidence, so the grouping refuses rather than assuming -
        # same contract as docs/ALIGNMENT.md.
        #
        trips = group_trips([
            self._run(1, 1000, 1300),
            self._run(2, 1305, 1600, clock=0),
        ])

        self.assertEqual(len(trips), 2)
        self.assertIn("clock not disciplined", trips[1].reason)

    def test_an_unknown_clock_splits_too(self):
        trips = group_trips([
            self._run(1, 1000, 1300),
            self._run(2, 1305, 1600, clock=None),
        ])

        self.assertEqual(len(trips), 2)

    def test_overlapping_runs_are_reported_not_merged(self):
        trips = group_trips([
            self._run(1, 1000, 1600),
            self._run(2, 1300, 1900),
        ])

        self.assertEqual(len(trips), 2)
        self.assertIn("overlapping", trips[1].reason)

    def test_a_run_with_no_end_splits_rather_than_swallowing_the_rest(self):
        #
        # `ended` is NULL when the process was killed. Treating that as
        # "still running" would merge every later drive into one; falling
        # back to `started` makes the gap look larger, which splits.
        #
        trips = group_trips([
            self._run(1, 1000, None),
            self._run(2, 5000, 5300),
        ])

        self.assertEqual(len(trips), 2)

    def test_grouping_is_deterministic_and_order_independent(self):
        runs = [
            self._run(1, 1000, 1300),
            self._run(2, 1305, 1600),
            self._run(3, 9000, 9300),
        ]
        forward = [t.trip_uid for t in group_trips(runs)]
        backward = [t.trip_uid for t in group_trips(list(reversed(runs)))]

        self.assertEqual(forward, backward)
        self.assertEqual(forward, [t.trip_uid for t in group_trips(runs)])

    def test_trip_identity_comes_from_the_first_run(self):
        #
        # Derived, not minted: re-analysing the same data must not look
        # like a new set of trips every time.
        #
        trips = group_trips([self._run(1, 1000, 1300, uid="U" * 26)])

        self.assertEqual(trips[0].trip_uid, "U" * 26)

    def test_every_boundary_carries_its_reason(self):
        trips = group_trips([
            self._run(1, 1000, 1300),
            self._run(2, 9000, 9300),
            self._run(3, 9305, 9600, boot="boot2"),
        ])

        for trip in trips:
            self.assertTrue(trip.reason)

    def test_no_runs_is_no_trips(self):
        self.assertEqual(group_trips([]), [])


class TripsFromARealDatabase(unittest.TestCase):
    def test_multiple_runs_in_one_drive_are_one_trip(self):
        path = os.path.join(tempfile.mkdtemp(), "tele.db")
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()

        for _ in range(3):
            rec.start_run("VINREDACTED", "gw", "DDE", 0x12, clock_synced=True)
            time.sleep(0.05)
            rec.write(time.time(), {"coolant": 80.0})

        rec.close()

        trips = group_trips(load_runs(path))

        self.assertEqual(len(trips), 1)
        self.assertEqual(len(trips[0].runs), 3)

    def test_the_report_says_which_part_of_the_drive_it_is(self):
        path = os.path.join(tempfile.mkdtemp(), "tele.db")
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()

        for _ in range(2):
            rec.start_run("VINREDACTED", "gw", "DDE", 0x12, clock_synced=True)
            time.sleep(0.05)
            rec.write(time.time(), {"coolant": 80.0})

        rec.close()

        run = session_report.load_run(path, 1)

        self.assertEqual(run["trip"]["of"], 2)
        self.assertEqual(run["trip"]["position"], 1)
        self.assertTrue(is_ulid(run["session_uid"]))


class VehicleEventsSegmentABaseline(unittest.TestCase):
    def _events(self):
        return (
            VehicleEvent(kind="oil_change", at=1000.0),
            VehicleEvent(kind="sensor_replacement", at=5000.0),
        )

    def test_an_event_between_two_points_invalidates_the_comparison(self):
        self.assertFalse(baseline_is_valid_across(self._events(), 900, 1100))

    def test_no_event_between_them_leaves_it_valid(self):
        self.assertTrue(baseline_is_valid_across(self._events(), 1100, 4000))

    def test_the_range_is_half_open_so_a_boundary_belongs_to_one_side(self):
        #
        # (start, end]: an event exactly at `start` already applied to the
        # baseline, one exactly at `end` has not. Without a rule, an event
        # on the boundary counts twice or not at all.
        #
        self.assertEqual(len(events_between(self._events(), 1000, 5000)), 1)
        self.assertEqual(len(events_between(self._events(), 999, 1000)), 1)

    def test_it_reads_either_order(self):
        self.assertEqual(len(events_between(self._events(), 5000, 900)), 2)

    def test_the_committed_example_loads_and_is_ordered(self):
        events = load_events(EXAMPLE)

        self.assertTrue(events)
        self.assertEqual(list(events), sorted(events, key=lambda e: e.at))

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(
            load_events(os.path.join(tempfile.mkdtemp(), "nope.yaml")), ()
        )

    def test_an_undated_event_is_skipped_not_placed_at_zero(self):
        #
        # At zero it would sit before all history and silently invalidate
        # every baseline there is.
        #
        path = os.path.join(tempfile.mkdtemp(), "events.yaml")

        with open(path, "w") as fh:
            fh.write("events:\n  - kind: oil_change\n"
                     "    description: date lost\n")

        self.assertEqual(load_events(path), ())

    def test_dates_and_timestamps_both_read(self):
        path = os.path.join(tempfile.mkdtemp(), "events.yaml")

        with open(path, "w") as fh:
            fh.write("events:\n  - {kind: a, at: 2026-06-15}\n"
                     "  - {kind: b, at: 100}\n")

        events = load_events(path)

        self.assertEqual([e.kind for e in events], ["b", "a"])

    def test_metadata_is_not_shared_between_events(self):
        #
        # A NamedTuple's default is ONE object shared by every instance
        # that omits it, so a mutable default would be a single dict
        # quietly shared across every event.
        #
        a = VehicleEvent(kind="x", at=1.0)
        b = VehicleEvent(kind="y", at=2.0)

        self.assertEqual(a.metadata, ())
        self.assertIsInstance(a.metadata, tuple)
        self.assertIs(type(a.metadata), type(b.metadata))
