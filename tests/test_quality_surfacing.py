"""
Quality carried the rest of the way: into diagnostics, and out of analytics.

Storing a quality label is only half the job. Two consumers have to act on
it or the distinction dies at the edges:

  * the diagnostics view, because request-level counters cannot express
    "every exchange succeeded and not one reading was a measurement";
  * the analytics, because averaging a sentinel together with real
    readings is precisely how a longitudinal health model learns
    something false.

The decode and storage halves are covered in test_mapping_decoder.py and
test_data_quality.py.
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
from bmwdiag.mapping import MappingRegistry, load_text
from bmwdiag.mapping.execute import MappingExecutor
from bmwdiag.mapping.registry import AllCapabilities


def flagging_profile():
    """One healthy channel, one sentinel, one on its rail."""
    mapping = load_text(
        "schema_version: 1\n"
        "mapping: {id: q-test, version: 1, production: false}\n"
        "ecu: {target: 0x12}\n"
        "requests:\n"
        "  probe:\n"
        "    protocol: uds\n"
        "    service: 0x22\n"
        "    did: 0xDA2E\n"
        "    response: {data_length: 3}\n"
        "    signals:\n"
        "      good: {label: G, unit: '', decode: {type: uint8, offset: 0}}\n"
        "      gone: {label: S, unit: '', decode: {type: uint8, offset: 1,"
        " invalid: [255]}}\n"
        "      rail: {label: R, unit: '', decode: {type: uint8, offset: 2,"
        " saturated: [255]}}\n",
        "q-test",
    )

    return MappingRegistry([mapping]).resolve(AllCapabilities())


class Answering:
    def request(self, payload, *, dst, timeout=None, expect=None):
        return bytes([0x62, 0xDA, 0x2E, 10, 255, 255])


class TheExecutorCountsQualityPerChannel(unittest.TestCase):
    def test_counts_are_signal_level_not_request_level(self):
        #
        # The distinction the issue turns on: one request, three signals,
        # three different verdicts. A request counter cannot say this.
        #
        profile = flagging_profile()
        executor = MappingExecutor(profile, transport=Answering())

        for _ in range(4):
            executor.execute_readings(profile.requests)

        self.assertEqual(
            executor.quality_stats(),
            {"good": {"ok": 4}, "gone": {"sentinel": 4}, "rail": {"saturated": 4}},
        )
        #: and the request itself looks perfectly healthy throughout
        self.assertEqual(executor.stats()["probe"]["failed"], 0)

    def test_the_snapshot_is_a_copy(self):
        """Read from the HTTP thread while the poll loop writes."""
        profile = flagging_profile()
        executor = MappingExecutor(profile, transport=Answering())
        executor.execute_readings(profile.requests)

        snapshot = executor.quality_stats()
        snapshot["good"]["ok"] = 999

        self.assertEqual(executor.quality_stats()["good"]["ok"], 1)


class TheDiagnosticsViewShowsIt(unittest.TestCase):
    def build(self):
        profile = flagging_profile()
        executor = MappingExecutor(profile, transport=Answering())

        for _ in range(4):
            executor.execute_readings(profile.requests)

        diag = live.Diagnostics()
        diag.publish(profile=profile, executor=executor, plan=None)

        return diag

    def channels(self):
        return {c["key"]: c for c in self.build().report()["channels"]}

    def test_a_healthy_channel_reports_no_flags(self):
        good = self.channels()["good"]

        self.assertEqual(good["quality"], {"ok": 4})
        self.assertEqual(good["flagged"], 0)
        self.assertEqual(good["flagged_pct"], 0.0)

    def test_a_channel_answering_only_sentinels_is_visible_as_such(self):
        #
        # This is the case the whole issue exists for. The request is at
        # 100% success; the channel is useless. Before this, the two were
        # indistinguishable in the view.
        #
        gone = self.channels()["gone"]

        self.assertEqual(gone["quality"], {"sentinel": 4})
        self.assertEqual(gone["flagged"], 4)
        self.assertEqual(gone["flagged_pct"], 100.0)

    def test_a_channel_that_never_decoded_is_not_called_healthy(self):
        #
        # None, not 0.0: "0% flagged" on a channel that has never answered
        # would read as a clean bill of health.
        #
        profile = flagging_profile()
        diag = live.Diagnostics()
        diag.publish(
            profile=profile,
            executor=MappingExecutor(profile, transport=Answering()),
            plan=None,
        )
        channels = {c["key"]: c for c in diag.report()["channels"]}

        self.assertEqual(channels["good"]["quality"], {})
        self.assertIsNone(channels["good"]["flagged_pct"])


class AnalyticsExcludeFlaggedByDefault(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "tele.db")
        rec = live.Recorder(self.db, chunk=1, interval=0.05)
        rec.set_metadata(flagging_profile())
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
        time.sleep(0.05)
        #: two real readings and one sentinel on the same channel
        rec.write(time.time(), {"good": 10.0}, {"good": "ok"})
        rec.write(time.time() + 1, {"good": 20.0}, {"good": "ok"})
        rec.write(time.time() + 2, {"good": 255.0}, {"good": "sentinel"})
        rec.write(time.time() + 3, {"gone": 255.0}, {"gone": "sentinel"})
        rec.close()

    def test_flagged_samples_are_left_out_of_the_series(self):
        run = session_report.load_run(self.db, None)

        self.assertEqual(
            [v for _t, v in run["series"]["good"]], [10.0, 20.0],
            "the sentinel must not reach any statistic",
        )

    def test_they_are_counted_rather_than_silently_dropped(self):
        run = session_report.load_run(self.db, None)

        self.assertEqual(run["flagged_counts"]["good"], {"sentinel": 1})
        self.assertEqual(run["flagged_counts"]["gone"], {"sentinel": 1})

    def test_they_can_be_asked_for_explicitly(self):
        run = session_report.load_run(self.db, None, include_flagged=True)

        self.assertEqual(
            [v for _t, v in run["series"]["good"]], [10.0, 20.0, 255.0]
        )
        self.assertTrue(run["include_flagged"])

    def test_a_channel_flagged_into_silence_still_appears(self):
        #
        # `gone` has no usable sample at all, so it has no series. Omitting
        # it from the quality table would put "answered, but nothing
        # usable" back into the same bucket as "never polled", which is
        # the confusion this layer exists to end.
        #
        run = session_report.load_run(self.db, None)
        rows = {q["key"]: q for q in session_report.quality(run)}

        self.assertIn("gone", rows)
        self.assertEqual(rows["gone"]["samples"], 0)
        self.assertEqual(rows["gone"]["flagged"], {"sentinel": 1})

    def test_the_report_says_what_it_excluded(self):
        run = session_report.load_run(self.db, None)
        md = session_report.render_markdown(
            run, session_report.warmup(run), session_report.crosschecks(run),
            session_report.phase_mask(run), {}, session_report.dpf(run),
            session_report.quality(run),
        )

        self.assertIn("excluded from every statistic", md)
        self.assertIn("sentinel", md)


class PreQualityDatabasesStillAnalyse(unittest.TestCase):
    def test_a_database_without_the_column_reads_as_ok(self):
        #
        # The analysis tool opens drive files it did not create, including
        # ones recorded before quality existed. Those must not become
        # unreadable, and their rows count as ok - meaning "the decoder of
        # the day accepted this", not "verified good".
        #
        db = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " started_at REAL, ended_at REAL, vin TEXT, gateway TEXT,"
            " ecu TEXT, ecu_addr INTEGER);"
            "CREATE TABLE params(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " key TEXT UNIQUE, pid INTEGER, label TEXT, unit TEXT);"
            "CREATE TABLE samples(run_id INTEGER, ts REAL, param_id INTEGER,"
            " value REAL);"
        )
        con.execute("INSERT INTO runs(id, started_at, ended_at) VALUES(1,1e9,1e9)")
        con.execute("INSERT INTO params(key, unit) VALUES('good','')")
        con.execute("INSERT INTO samples VALUES(1, 1e9, 1, 10.0)")
        con.commit()
        con.close()

        run = session_report.load_run(db, None)

        self.assertEqual([v for _t, v in run["series"]["good"]], [10.0])
        self.assertEqual(run["flagged_counts"], {})


class UnitsComeFromTheRunSnapshot(unittest.TestCase):
    """
    Follow-on from #5: `params` is first-seen channel identity, so its
    unit is whatever was loaded the first time the database ever saw the
    channel. A later mapping that corrects a unit must not make an older
    report display the new one, and a newer run must not display the old.
    """

    def test_each_run_reports_its_own_unit(self):
        db = os.path.join(tempfile.mkdtemp(), "tele.db")

        for version, unit in ((1, "rpm"), (2, "1/min")):
            mapping = load_text(
                "schema_version: 1\n"
                f"mapping: {{id: u-test, version: {version}, production: false}}\n"
                "ecu: {target: 0x12}\n"
                "requests:\n"
                "  probe:\n"
                "    protocol: uds\n"
                "    service: 0x22\n"
                "    did: 0xDA2E\n"
                "    response: {data_length: 1}\n"
                "    signals:\n"
                f"      good: {{label: G, unit: '{unit}',"
                " decode: {type: uint8}}\n",
                "u-test",
            )
            rec = live.Recorder(db, chunk=1, interval=0.05)
            rec.set_metadata(MappingRegistry([mapping]).resolve(AllCapabilities()))
            rec.open()
            rec.start_run("VINREDACTED", "gw", "DDE", 0x12)
            time.sleep(0.05)
            rec.write(time.time(), {"good": 10.0})
            rec.close()

        self.assertEqual(session_report.load_run(db, 1)["units"]["good"], "rpm")
        self.assertEqual(session_report.load_run(db, 2)["units"]["good"], "1/min")
