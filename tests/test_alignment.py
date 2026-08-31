"""
The time-alignment contract: when two observations may be compared.

Cross-channel metrics subtract values that were never sampled together.
Unbounded nearest-value matching produced a number for every pair however
stale, and the number was indistinguishable from a measurement - so a
boost actual minus a setpoint read twelve seconds earlier was reported as
control error when it is mostly the engine having moved.

These cover the matcher, the per-pair windows, the coverage that says how
much had to be thrown away, and the guards that keep the committed SQL
from quietly dropping the session/window/clock keys again.
"""

import json
import os
import re
import sqlite3
import tempfile
import time
import unittest

from tests import support  # noqa: F401

import live
from analysis import session_report
from analysis.alignment import (
    MIN_USEFUL_COVERAGE,
    align,
    pairing_for,
)

SQL = os.path.join(support.ROOT, "analysis", "clickhouse", "insights.sql")
DASHBOARD = os.path.join(
    support.ROOT, "infra", "grafana", "dashboards", "f10-health.json"
)


class TheWindowIsEnforced(unittest.TestCase):
    def test_a_sample_inside_the_window_matches(self):
        result = align([(10.0, 1.0)], [(10.4, 99.0)], 0.5)

        self.assertEqual(result.pairs, [(10.0, 1.0, 99.0)])
        self.assertEqual(result.coverage_pct, 100.0)

    def test_a_sample_outside_the_window_is_rejected(self):
        #
        # The defect in one assertion: the old matcher returned 99.0 here,
        # because it was the nearest - never mind that it was 12 seconds
        # away on a channel that moves in milliseconds.
        #
        result = align([(10.0, 1.0)], [(22.0, 99.0)], 0.5)

        self.assertEqual(result.pairs, [])
        self.assertEqual(result.matched, 0)
        self.assertEqual(result.attempted, 1)

    def test_the_boundary_is_inclusive(self):
        self.assertEqual(len(align([(10.0, 1.0)], [(10.5, 9.0)], 0.5).pairs), 1)
        self.assertEqual(len(align([(10.0, 1.0)], [(10.6, 9.0)], 0.5).pairs), 0)

    def test_the_nearer_of_two_candidates_wins(self):
        result = align([(10.0, 1.0)], [(9.6, 11.0), (10.2, 22.0)], 1.0)

        self.assertEqual(result.pairs[0][2], 22.0)

    def test_an_empty_side_matches_nothing(self):
        self.assertEqual(align([(1.0, 1.0)], [], 5.0).pairs, [])
        self.assertEqual(align([], [(1.0, 1.0)], 5.0).attempted, 0)


class CoverageReportsWhatWasRejected(unittest.TestCase):
    def test_coverage_counts_the_unmatched(self):
        a = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0)]
        b = [(0.1, 9.0)]                       # only the first can match
        result = align(a, b, 0.5)

        self.assertEqual(result.matched, 1)
        self.assertEqual(result.attempted, 4)
        self.assertEqual(result.coverage_pct, 25.0)

    def test_low_coverage_is_not_usable(self):
        #
        # A metric covering a quarter of its own inputs describes the poll
        # schedule, not the car. The report must be able to say so rather
        # than print a confident average.
        #
        result = align([(0.0, 1.0)] + [(float(i), 1.0) for i in range(1, 5)],
                       [(0.1, 9.0)], 0.5)

        self.assertLess(result.coverage_pct, MIN_USEFUL_COVERAGE)
        self.assertFalse(result.usable)

    def test_full_coverage_is_usable(self):
        a = [(0.0, 1.0), (1.0, 2.0)]
        b = [(0.05, 9.0), (1.05, 8.0)]

        self.assertTrue(align(a, b, 0.5).usable)

    def test_median_gap_is_reported(self):
        result = align([(0.0, 1.0), (1.0, 1.0)], [(0.1, 9.0), (1.4, 9.0)], 1.0)

        self.assertEqual(result.median_gap_s, 0.4)


class WindowsArePerPair(unittest.TestCase):
    def test_a_control_loop_gets_a_tight_window(self):
        self.assertEqual(
            pairing_for("n47d_boost_act", "n47d_boost_set").max_age_s, 0.5
        )

    def test_a_slow_thermal_pair_gets_a_wide_one(self):
        self.assertEqual(pairing_for("n47d_coolant", "coolant").max_age_s, 15.0)

    def test_order_does_not_matter(self):
        self.assertEqual(
            pairing_for("coolant", "n47d_coolant").max_age_s,
            pairing_for("n47d_coolant", "coolant").max_age_s,
        )

    def test_an_undeclared_pair_gets_a_strict_default(self):
        #
        # Strict rather than permissive: an undeclared comparison should
        # look visibly poor and get a real window, not inherit a
        # convenient one.
        #
        self.assertEqual(pairing_for("nothing", "nowhere").max_age_s, 1.0)

    def test_every_declared_pair_explains_itself(self):
        from analysis.alignment import PAIRINGS

        for pair, rule in PAIRINGS.items():
            with self.subTest(pair=pair):
                self.assertTrue(rule.why.strip(), "a window needs a reason")
                self.assertGreater(rule.max_age_s, 0)


class TheReportRefusesUnusableConclusions(unittest.TestCase):
    """End to end: a staggered pair must not produce a tracking number."""

    def _db(self, gap_s):
        path = os.path.join(tempfile.mkdtemp(), "tele.db")
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12, clock_synced=True)
        time.sleep(0.05)
        base = time.time()

        for i in range(20):
            t = base + i * 30.0
            rec.write(t, {"n47d_boost_act": 1500.0, "speed": 50.0})
            rec.write(t + gap_s, {"n47d_boost_set": 1400.0})

        rec.close()

        return path

    def test_a_staggered_pair_is_reported_as_insufficient(self):
        run = session_report.load_run(self._db(12.0), None)
        tracking = session_report.load_behaviour(run, None)["setpoint_tracking"]
        boost = [t for t in tracking if t["actual"] == "n47d_boost_act"]

        self.assertTrue(boost, "the pair should still be reported")
        self.assertFalse(boost[0]["usable"])
        self.assertEqual(boost[0]["coverage_pct"], 0.0)

    def test_a_co_scheduled_pair_produces_a_number(self):
        run = session_report.load_run(self._db(0.2), None)
        tracking = session_report.load_behaviour(run, None)["setpoint_tracking"]
        boost = [t for t in tracking if t["actual"] == "n47d_boost_act"]

        self.assertTrue(boost[0]["usable"])
        self.assertEqual(boost[0]["mean_abs_deviation"], 100.0)

    def test_the_findings_say_why_rather_than_going_quiet(self):
        run = session_report.load_run(self._db(12.0), None)
        lb = session_report.load_behaviour(run, None)
        lines = session_report.findings(run, {}, [], lb, {})
        text = " ".join(lines)

        self.assertIn("cannot be concluded", text)
        self.assertIn("staggered", text)


class CrossSessionAndClockTrust(unittest.TestCase):
    def test_a_local_report_cannot_cross_runs(self):
        #
        # load_run is scoped to one run_id, so a Python-side comparison
        # physically cannot reach into another drive. Asserted rather than
        # assumed, because it is the property the ClickHouse side has to
        # reproduce with an explicit session_id join key.
        #
        path = os.path.join(tempfile.mkdtemp(), "tele.db")
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()

        for value in (1.0, 2.0):
            rec.start_run("VINREDACTED", "gw", "DDE", 0x12, clock_synced=True)
            time.sleep(0.05)
            rec.write(time.time(), {"coolant": value})

        rec.close()

        self.assertEqual(
            [v for _t, v in session_report.load_run(path, 1)["series"]["coolant"]],
            [1.0],
        )
        self.assertEqual(
            [v for _t, v in session_report.load_run(path, 2)["series"]["coolant"]],
            [2.0],
        )

    def test_an_untrusted_clock_is_declared_at_the_top_of_the_report(self):
        path = os.path.join(tempfile.mkdtemp(), "tele.db")
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12, clock_synced=False)
        time.sleep(0.05)
        rec.write(time.time(), {"coolant": 80.0})
        rec.close()

        run = session_report.load_run(path, None)

        self.assertEqual(run["clock_synced"], 0)

        md = session_report.render_markdown(
            run, session_report.warmup(run), session_report.crosschecks(run),
            session_report.phase_mask(run), {}, session_report.dpf(run),
            session_report.quality(run),
        )

        self.assertIn("host clock", md)
        self.assertIn("NOT NTP-disciplined", md)

    def test_an_unknown_clock_is_not_assumed_good(self):
        path = os.path.join(tempfile.mkdtemp(), "tele.db")
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12)     # clock_synced=None
        time.sleep(0.05)
        rec.write(time.time(), {"coolant": 80.0})
        rec.close()

        run = session_report.load_run(path, None)
        md = session_report.render_markdown(
            run, session_report.warmup(run), session_report.crosschecks(run),
            session_report.phase_mask(run), {}, session_report.dpf(run),
            session_report.quality(run),
        )

        self.assertIn("UNKNOWN", md)


class TheCommittedSqlKeepsTheContract(unittest.TestCase):
    """
    Guards over the query text. The defect lives in the SQL, so it cannot
    be caught by running the query against data that happens not to
    contain a cross-session neighbour.
    """

    def sql(self):
        with open(SQL) as fh:
            return fh.read()

    def panels(self):
        with open(DASHBOARD) as fh:
            return json.load(fh)["panels"]

    def test_no_asof_join_keys_on_vehicle_alone(self):
        #
        # Joining on vehicle_id lets ASOF reach back into the PREVIOUS
        # DRIVE for its nearest value - across an ignition cycle, hours
        # of parking, and a different mapping/mode configuration.
        #
        self.assertNotIn("a.vehicle_id=b.vehicle_id", self.sql())
        self.assertNotIn("a.vehicle_id=c.vehicle_id", self.sql())

        for panel in self.panels():
            sql = " ".join(t.get("rawSql", "") for t in panel.get("targets", []))

            with self.subTest(panel=panel.get("title")):
                self.assertNotIn("a.vehicle_id=b.vehicle_id", sql)

    def test_every_asof_join_keys_on_session(self):
        joins = re.findall(r"ASOF (?:LEFT )?JOIN.*?ON ([^\n]+)", self.sql(),
                           re.DOTALL)

        self.assertTrue(joins, "the battery has no ASOF joins any more")

        for on_clause in joins:
            with self.subTest(on=on_clause.strip()[:60]):
                self.assertIn("session_id", on_clause)

    def test_time_derived_sections_require_a_disciplined_clock(self):
        #
        # CLAUDE.md already required this and the queries did not do it.
        # NULL means "recorded before the flag" - unknown, so excluded
        # rather than assumed good.
        #
        parts = re.split(r"\n-- (\d+[a-z]?)\. ", "\n" + self.sql())
        sections = dict(zip(parts[1::2], parts[2::2]))

        for number in ("2", "3", "4", "5"):
            with self.subTest(section=number):
                self.assertIn("clock_synced=1", sections[number])

    def test_the_coverage_section_exists(self):
        self.assertIn("alignment coverage", self.sql())

    def test_the_boost_panel_warns_that_it_will_be_sparse(self):
        boost = [p for p in self.panels() if "Boost tracking" in p.get("title", "")]

        self.assertTrue(boost)
        self.assertIn("SPARSE OR EMPTY", boost[0]["description"])
