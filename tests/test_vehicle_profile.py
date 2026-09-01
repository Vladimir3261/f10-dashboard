"""
Vehicle configuration as an input to analytics.

A health system must know whether the component it is evaluating exists.
This one did not: the target car's particulate filter was removed, and
the analysis layer went on reporting restriction baselines, soot
accumulation and differential-pressure health as though a filter were
fitted. Those conclusions are not uncertain, they are impossible.

Three states, and the third is the point - `unknown` must behave like
`absent` when deciding whether to conclude, and unlike it when reporting
why.
"""

import json
import os
import re
import tempfile
import time
import unittest

from tests import support  # noqa: F401

import live
import sys
from analysis import session_report
from bmwdiag.vehicle import (
    ABSENT,
    PRESENT,
    UNKNOWN,
    VehicleProfile,
    load_profile,
)

EXAMPLE = os.path.join(support.ROOT, "config", "vehicle-profile.example.yaml")
SQL = os.path.join(support.ROOT, "analysis", "clickhouse", "insights.sql")
DASHBOARD = os.path.join(
    support.ROOT, "infra", "grafana", "dashboards", "f10-health.json"
)


class TheThreeStates(unittest.TestCase):
    def test_a_declared_absence_is_absent(self):
        p = VehicleProfile(hardware={"dpf": False}, source="x")

        self.assertEqual(p.state("dpf"), ABSENT)
        self.assertFalse(p.has("dpf"))
        self.assertTrue(p.is_absent("dpf"))

    def test_a_declared_presence_is_present(self):
        p = VehicleProfile(hardware={"dpf": True}, source="x")

        self.assertEqual(p.state("dpf"), PRESENT)
        self.assertTrue(p.has("dpf"))
        self.assertFalse(p.is_absent("dpf"))

    def test_an_undeclared_subsystem_is_unknown(self):
        p = VehicleProfile(hardware={"egr": True}, source="x")

        self.assertEqual(p.state("dpf"), UNKNOWN)

    def test_unknown_does_not_count_as_present(self):
        #
        # The safety property. An unconfigured checkout must not start
        # asserting the health of a part nobody has confirmed exists.
        #
        self.assertFalse(VehicleProfile().has("dpf"))

    def test_unknown_does_not_count_as_absent_either(self):
        #
        # And it must not claim the part was REMOVED, which is a
        # statement about the car nobody made.
        #
        self.assertFalse(VehicleProfile().is_absent("dpf"))

    def test_the_two_reasons_are_worded_differently(self):
        absent = VehicleProfile(hardware={"dpf": False}, source="x").why_not("dpf")
        unknown = VehicleProfile().why_not("dpf")

        self.assertIn("VOID", absent)
        self.assertIn("no dpf", absent)
        self.assertIn("NOT EVALUATED", unknown)
        self.assertIn("not recorded", unknown)

    def test_word_forms_are_accepted(self):
        for word, expected in (("removed", ABSENT), ("deleted", ABSENT),
                               ("fitted", PRESENT), ("yes", PRESENT),
                               ("maybe", UNKNOWN)):
            with self.subTest(word=word):
                p = VehicleProfile(hardware={"x": word}, source="s")
                self.assertEqual(p.state("x"), expected)


class LoadingAProfile(unittest.TestCase):
    def test_a_missing_file_is_not_an_error(self):
        #
        # The ordinary state of a fresh checkout, of CI, and of anyone
        # analysing someone else's drive file.
        #
        p = load_profile(os.path.join(tempfile.mkdtemp(), "nope.yaml"))

        self.assertFalse(p.configured)
        self.assertEqual(p.state("dpf"), UNKNOWN)

    def test_the_committed_example_describes_this_car(self):
        p = load_profile(EXAMPLE)

        self.assertEqual(p.label, "F10-520d-dev")
        self.assertTrue(p.is_absent("dpf"))
        self.assertTrue(p.has("egr"))

    def test_the_example_carries_no_vin(self):
        #
        # The repo's standing rule. The label is the identifier; the
        # label -> VIN table lives in gitignored local/.
        #
        # Tests for a VIN-SHAPED VALUE, not the word: the file is expected
        # to say "NO VIN" and to point at the label->VIN table, and
        # banning the word would only teach the next person to stop
        # explaining the rule.
        #
        with open(EXAMPLE) as fh:
            text = fh.read()

        #: 17 chars from the VIN alphabet (no I, O or Q), as a whole token
        vin_shaped = re.findall(r"\b[A-HJ-NPR-Z0-9]{17}\b", text)

        self.assertEqual(vin_shaped, [], f"VIN-shaped token in {EXAMPLE}")

    def test_a_modification_is_quoted_in_the_reason(self):
        p = load_profile(EXAMPLE)

        self.assertIn("dpf_removed", p.why_not("dpf"))


class DpfConclusionsAreConditionedOnHardware(unittest.TestCase):
    def _run(self, vehicle, dp_value=-11.0):
        path = os.path.join(tempfile.mkdtemp(), "tele.db")
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12, clock_synced=True)
        time.sleep(0.05)
        base = time.time()

        for i in range(20):
            rec.write(base + i, {
                "n47d_soot_meas": 9.4 + i * 0.01,
                "n47d_soot_model": 9.5,
                "n47d_dpf_dp": dp_value + i,
                "n47d_regen_count": 93.0,
            })

        rec.close()

        return session_report.load_run(path, None, vehicle=vehicle)

    def _findings(self, run):
        return " ".join(session_report.findings(
            run, session_report.warmup(run), session_report.crosschecks(run),
            session_report.load_behaviour(run, None), session_report.dpf(run),
        ))

    def test_no_filter_means_no_health_conclusion(self):
        #
        # The acceptance criterion: never state that differential-pressure
        # sensing is healthy on a car with no filter to sense across.
        #
        run = self._run(VehicleProfile(hardware={"dpf": False}, source="x"))
        dp = session_report.dpf(run)
        text = self._findings(run)

        self.assertTrue(dp["physical_conclusions_void"])
        self.assertNotIn("mean_abs_diff", dp)
        self.assertNotIn("sensing is healthy", text)
        self.assertNotIn("restriction trending", text)

    def test_no_filter_still_reports_the_ecu_model_as_a_model(self):
        #
        # Distinguishing ECU model state from a physical measurement is
        # the point, not hiding the channel.
        #
        run = self._run(VehicleProfile(hardware={"dpf": False}, source="x"))

        self.assertIn("measured", session_report.dpf(run))
        self.assertIn("ECU's internal model", self._findings(run))

    def test_no_filter_says_the_dp_channel_reads_an_empty_pipe(self):
        run = self._run(VehicleProfile(hardware={"dpf": False}, source="x"))

        self.assertIn("empty pipe", self._findings(run))

    def test_regeneration_count_stays_meaningful_without_a_filter(self):
        #
        # Explicitly NOT suppressed. The ECU still commands regens against
        # its model, burning fuel and diluting oil to clean nothing - the
        # one DPF-adjacent number that got more interesting, not less.
        #
        run = self._run(VehicleProfile(hardware={"dpf": False}, source="x"))
        text = self._findings(run)

        self.assertIn("Regeneration count", text)
        self.assertIn("oil dilution", text)

    def test_a_fitted_filter_permits_the_health_analytics(self):
        """The control: the gate must not simply delete the feature."""
        run = self._run(VehicleProfile(hardware={"dpf": True}, source="x"))
        dp = session_report.dpf(run)

        self.assertNotIn("physical_conclusions_void", dp)
        self.assertIn("mean_abs_diff", dp)
        self.assertIn("restriction trending", self._findings(run))

    def test_unknown_configuration_withholds_rather_than_assumes(self):
        run = self._run(VehicleProfile())
        dp = session_report.dpf(run)
        text = self._findings(run)

        self.assertTrue(dp["physical_conclusions_void"])
        self.assertEqual(dp["filter_state"], UNKNOWN)
        self.assertIn("NOT EVALUATED", text)
        self.assertNotIn("sensing is healthy", text)

    def test_the_rendered_report_names_the_configuration(self):
        run = self._run(VehicleProfile(
            label="F10-520d-dev", hardware={"dpf": False}, source="x"))
        md = session_report.render_markdown(
            run, session_report.warmup(run), session_report.crosschecks(run),
            session_report.phase_mask(run),
            session_report.load_behaviour(run, None), session_report.dpf(run),
            session_report.quality(run),
        )

        self.assertIn("F10-520d-dev", md)
        self.assertIn("VOID", md)
        self.assertNotIn("sensing is healthy", md)


class TheMechanismGeneralises(unittest.TestCase):
    def test_it_is_not_dpf_specific(self):
        #
        # The same question has to work for a remap, an EGR delete, a
        # replaced sensor or a different battery. Nothing in the profile
        # knows what a DPF is.
        #
        p = VehicleProfile(
            hardware={"egr": False, "swirl_flaps": True}, source="x")

        self.assertTrue(p.is_absent("egr"))
        self.assertTrue(p.has("swirl_flaps"))
        self.assertEqual(p.state("catalyst"), UNKNOWN)

    def test_a_modification_note_is_found_by_subsystem(self):
        p = VehicleProfile(
            hardware={"egr": False},
            modifications=({"type": "egr_delete", "at": "2024"},),
            source="x",
        )

        self.assertIn("egr_delete, 2024", p.why_not("egr"))


class TheLakeConsumersAreGatedToo(unittest.TestCase):
    def sql(self):
        with open(SQL) as fh:
            return fh.read()

    def panels(self):
        with open(DASHBOARD) as fh:
            return json.load(fh)["panels"]

    def test_the_dpf_sections_are_parameterised(self):
        parts = re.split(r"\n-- (\d+[a-z]?)\. ", "\n" + self.sql())
        sections = dict(zip(parts[1::2], parts[2::2]))

        for number in ("2", "4"):
            with self.subTest(section=number):
                self.assertIn("{dpf_present:UInt8} = 1", sections[number])

    def test_the_regeneration_section_is_NOT_gated(self):
        #
        # A commanded regen is something the ECU did. It happens, and
        # costs fuel and oil dilution, whether or not a filter is fitted.
        #
        parts = re.split(r"\n-- (\d+[a-z]?)\. ", "\n" + self.sql())
        sections = dict(zip(parts[1::2], parts[2::2]))

        self.assertIn("8", sections)
        self.assertNotIn("{dpf_present:UInt8}", sections["8"])

    def test_the_dashboard_has_the_variable(self):
        with open(DASHBOARD) as fh:
            names = [v["name"] for v in json.load(fh)["templating"]["list"]]

        self.assertIn("dpf_present", names)

    def test_the_dpf_panels_are_gated_and_say_so(self):
        for panel in self.panels():
            if panel["id"] not in (2, 4):
                continue

            with self.subTest(panel=panel["title"]):
                sql = panel["targets"][0]["rawSql"]
                self.assertIn("$dpf_present=1", sql)
                self.assertIn("VOID", panel["title"] + panel["description"])


class ConfigurationIsRunProvenance(unittest.TestCase):
    """
    A run keeps the configuration that was true WHEN IT WAS RECORDED.

    The profile file describes the car today. Interpreting an old drive
    through it relabels history: a run recorded with the filter fitted
    would have its differential-pressure readings declared void the
    moment the filter comes off and the profile is updated - a statement
    about hardware that did exist at the time. The reverse is as bad
    after a part is restored.

    Same defect as params.mapping_ver in #5, one layer over, and fixed
    the same way: snapshot at run start, resolve through the run.
    """

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "tele.db")

    def _record(self, profile, value):
        rec = live.Recorder(self.db, chunk=1, interval=0.05)
        rec.set_vehicle(profile)
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12, clock_synced=True)
        time.sleep(0.05)
        base = time.time()

        for i in range(10):
            rec.write(base + i, {"n47d_dpf_dp": value,
                                 "n47d_soot_meas": 9.4,
                                 "n47d_soot_model": 9.5})

        rec.close()

    def _record_both(self):
        """Run 1 with a filter fitted; run 2, same DB, after removal."""
        self._record(VehicleProfile(label="F10-520d-dev",
                                    hardware={"dpf": True}, source="x"), 12.0)
        self._record(VehicleProfile(label="F10-520d-dev",
                                    hardware={"dpf": False}, source="x"), -11.0)

    def test_the_snapshot_is_stored_on_the_run(self):
        self._record_both()

        import sqlite3
        con = sqlite3.connect(self.db)

        try:
            rows = con.execute(
                "SELECT id, vehicle_label, vehicle_hardware FROM runs ORDER BY id"
            ).fetchall()
        finally:
            con.close()

        self.assertEqual(rows, [
            (1, "F10-520d-dev", "dpf=present"),
            (2, "F10-520d-dev", "dpf=absent"),
        ])

    def test_the_earlier_run_keeps_present_semantics(self):
        self._record_both()
        run = session_report.load_run(self.db, 1)

        self.assertEqual(run["vehicle_provenance"], "run")
        self.assertTrue(run["vehicle"].has("dpf"))
        self.assertNotIn("physical_conclusions_void", session_report.dpf(run))

    def test_the_later_run_is_void(self):
        self._record_both()
        run = session_report.load_run(self.db, 2)

        self.assertTrue(run["vehicle"].is_absent("dpf"))
        self.assertTrue(session_report.dpf(run)["physical_conclusions_void"])

    def test_todays_profile_cannot_relabel_an_old_run(self):
        #
        # The regression in one assertion. Analysing run 1 while the
        # CURRENT profile says the filter is gone must still yield
        # filter-present semantics, because that is what was true then.
        #
        self._record_both()
        today = VehicleProfile(hardware={"dpf": False}, source="today")

        run = session_report.load_run(self.db, 1, vehicle=today)

        self.assertTrue(run["vehicle"].has("dpf"))
        self.assertEqual(run["vehicle_provenance"], "run")

    def test_nor_can_it_relabel_in_the_other_direction(self):
        #
        # The mirror case, after a part is restored: today's profile
        # saying the filter is BACK must not resurrect conclusions for a
        # run recorded while it was missing.
        #
        self._record_both()
        today = VehicleProfile(hardware={"dpf": True}, source="today")

        run = session_report.load_run(self.db, 2, vehicle=today)

        self.assertTrue(run["vehicle"].is_absent("dpf"))

    def test_a_run_predating_the_field_is_labelled_as_such(self):
        #
        # The fallback is allowed, but must never read as historical
        # truth. `source` says where the answer came from and the report
        # quotes it.
        #
        import sqlite3
        self._record(VehicleProfile(hardware={"dpf": True}, source="x"), 12.0)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE runs SET vehicle_hardware = '' WHERE id = 1")
        con.commit()
        con.close()

        today = VehicleProfile(label="now", hardware={"dpf": False},
                               source="today")
        run = session_report.load_run(self.db, 1, vehicle=today)

        self.assertEqual(run["vehicle_provenance"], "current")
        self.assertIn("TODAY", run["vehicle"].source)

    def test_the_report_warns_when_configuration_is_not_the_runs_own(self):
        import sqlite3
        self._record(VehicleProfile(hardware={"dpf": True}, source="x"), 12.0)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE runs SET vehicle_hardware = '' WHERE id = 1")
        con.commit()
        con.close()

        run = session_report.load_run(
            self.db, 1,
            vehicle=VehicleProfile(hardware={"dpf": False}, source="today"))
        md = session_report.render_markdown(
            run, session_report.warmup(run), session_report.crosschecks(run),
            session_report.phase_mask(run),
            session_report.load_behaviour(run, None), session_report.dpf(run),
            session_report.quality(run),
        )

        self.assertIn("TODAY'S", md)

    def test_the_lake_representation_preserves_the_distinction(self):
        #
        # Requirement 8: the per-session distinction has to survive the
        # wire and the ingest, or lake analytics is back to a global
        # toggle that reinterprets every historical drive.
        #
        sys.path.insert(0, os.path.join(support.ROOT, "infra"))
        from sync import agent as sync_agent
        from ingest import server as ingest_server
        from common import wire

        self._record_both()

        rows = sync_agent.read_sessions(self.db, 0)
        built = ingest_server.build_sessions(
            wire.decode(wire.encode(wire.columnar(
                "sessions",
                [{k: v for k, v in r.items() if k != "_id"} for r in rows],
            )))
        )

        self.assertEqual(
            sorted((b["vehicle_hardware"] for b in built)),
            ["dpf=absent", "dpf=present"],
        )
        self.assertTrue(all(b["vehicle_label"] == "F10-520d-dev" for b in built))

    def test_an_old_database_without_the_columns_still_syncs(self):
        import sqlite3
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
        con.execute("INSERT INTO runs(id, started_at, vin) VALUES(1, 1e9, 'V')")
        con.commit()
        con.close()

        sys.path.insert(0, os.path.join(support.ROOT, "infra"))
        from sync import agent as sync_agent

        rows = sync_agent.read_sessions(path, 0)

        self.assertEqual(rows[0]["vehicle_hardware"], "")
