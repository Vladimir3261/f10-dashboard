"""
The car-communication picture for one session.

Three questions had no answer anywhere before this, and all three are the
ones you actually have when a channel is missing:

  * **Did we even ask?** A request nobody made and one that always fails
    are identical in the sample table - both are absent rows.
  * **Did the car answer?** Per-request success rates did not exist.
  * **Was it filtered, and why?** Resolution drops mappings and requests
    silently by design (a file for another variant is skipped, not an
    error), and it threw the reason away.

Offline: a fake transport, a fake OBD reader, no car.
"""

import os
import sys
import unittest

from tests import support  # noqa: F401

import live
from bmwdiag.mapping import (
    MappingExecutor, fault_kind, load_file, load_text,
)
from bmwdiag.mapping.polling import PollingPlan, resolve_classes
from bmwdiag.mapping.registry import (
    AllCapabilities,
    Dropped,
    MappingRegistry,
    ResolutionReport,
)
from bmwdiag.obd import ObdCapabilitySet


def engine_registry():
    return MappingRegistry([load_file(support.OBD_MAPPING)])


class WhyIsThisChannelMissing(unittest.TestCase):
    """Resolution records every decision, including the ones it discards."""

    def test_a_request_the_ecu_does_not_advertise_says_so(self):
        registry = engine_registry()
        profile = registry.resolve(
            ObdCapabilitySet({0x0C}), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

        dropped = {d.id: d for d in profile.report.dropped}

        self.assertIn("obd.mode01.05", dropped)
        self.assertEqual(dropped["obd.mode01.05"].reason, "capability")
        #: Hex, because that is how a mapping file writes it and how you
        #: would grep for it. "=5" is not greppable.
        self.assertIn("0x05", dropped["obd.mode01.05"].detail)

    def test_a_mapping_for_another_variant_names_the_variant(self):
        registry = MappingRegistry([
            load_file(support.OBD_MAPPING),
            load_file("mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml"),
        ])
        profile = registry.resolve(
            ObdCapabilitySet({0x0C}), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

        dropped = {
            d.id: d for d in profile.report.by_reason("ecu_mismatch")
        }

        self.assertIn("candidate-n47-d72-flow", dropped)
        self.assertIn(
            "d72n47a0", dropped["candidate-n47-d72-flow"].detail
        )

    def test_a_derived_channel_names_the_input_it_lost(self):
        registry = engine_registry()
        #: `fuel` unsupported, so `fuel_l` can never produce a value.
        profile = registry.resolve(
            ObdCapabilitySet({0x0C, 0x0B, 0x33}), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

        dropped = {d.id: d for d in profile.report.by_reason("inputs")}

        self.assertIn("fuel_l", dropped)
        self.assertIn("fuel", dropped["fuel_l"].detail)

    def test_a_derived_channel_with_a_fallback_survives(self):
        """
        `boost` needs map and baro, but declares a fallback for baro - so
        losing baro must not remove the channel. This is the difference
        between "input missing" and "input optional".
        """
        registry = engine_registry()
        profile = registry.resolve(
            ObdCapabilitySet({0x0C, 0x0B}), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

        self.assertIn("boost", [d.key for d in profile.derived])
        self.assertNotIn(
            "boost", [d.id for d in profile.report.by_reason("inputs")]
        )

    def test_nothing_is_dropped_when_everything_applies(self):
        profile = engine_registry().resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

        self.assertEqual(profile.report.dropped, ())
        self.assertEqual(profile.report.active, ("sae-obd-engine",))

    def test_a_directly_built_profile_has_an_empty_report(self):
        """Not every profile comes from resolve(); it must not be None."""
        from bmwdiag.mapping.registry import ResolvedProfile

        profile = ResolvedProfile(requests=[], derived=[])

        self.assertIsInstance(profile.report, ResolutionReport)
        self.assertEqual(profile.report.dropped, ())


class PerRequestHealth(unittest.TestCase):
    """
    Which channels are answering, and which are merely being asked.

    "sent 590, ok 0" is the signature of a channel the car does not
    really provide - and it is invisible in the sample table, where it
    looks exactly like a channel nobody polls.
    """

    def setUp(self):
        self.registry = engine_registry()
        self.profile = self.registry.resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

    def test_counters_start_empty(self):
        executor = MappingExecutor(self.profile)

        self.assertEqual(executor.stats(), {})

    def test_a_successful_read_counts_sent_and_ok(self):
        from tests.test_mapping_requests import FakeObdReader

        reader = FakeObdReader({0x0C: b"\x0c\x3c"})
        executor = MappingExecutor(self.profile, obd_reader=reader)
        request = self.profile.request("obd.mode01.0C")

        executor.execute([request])
        st = executor.stats()["obd.mode01.0C"]

        self.assertEqual(st["sent"], 1)
        self.assertEqual(st["ok"], 1)
        self.assertEqual(st["failed"], 0)
        self.assertIsNotNone(st["last_ok"])

    def test_a_pid_the_reader_never_returns_is_counted_as_a_failure(self):
        """
        The OBD session retires PIDs the ECU ignores, so nothing raises -
        the reply simply lacks that PID. Without counting it, the most
        common "channel is silent" case leaves no trace at all.
        """
        from tests.test_mapping_requests import FakeObdReader

        reader = FakeObdReader({0x0C: b"\x0c\x3c"})     # 0x0B absent
        executor = MappingExecutor(self.profile, obd_reader=reader)

        executor.execute([
            self.profile.request("obd.mode01.0C"),
            self.profile.request("obd.mode01.0B"),
        ])
        stats = executor.stats()

        self.assertEqual(stats["obd.mode01.0C"]["ok"], 1)
        self.assertEqual(stats["obd.mode01.0B"]["sent"], 1)
        self.assertEqual(stats["obd.mode01.0B"]["ok"], 0)
        self.assertEqual(stats["obd.mode01.0B"]["failed"], 1)
        self.assertIn("no_response", stats["obd.mode01.0B"]["kinds"])

    def test_faults_are_counted_by_kind_and_keep_the_last_message(self):
        class Nack(Exception):
            pass

        class Refusing:
            def request(self, payload, *, dst, timeout=None):
                raise Nack("gateway will not route to 0x18")

        mapping = load_text(
            "schema_version: 1\n"
            "mapping: {id: t, version: 1, production: false}\n"
            "ecu: {target: 0x18}\n"
            "requests:\n"
            "  probe:\n"
            "    protocol: uds\n"
            "    service: 0x22\n"
            "    did: 0xDA2E\n"
            "    response: {data_length: 2}\n"
            "    signals:\n"
            "      g: {label: G, unit: '', decode: {type: uint8}}\n",
            "test",
        )
        profile = MappingRegistry([mapping]).resolve(AllCapabilities())
        executor = MappingExecutor(profile, transport=Refusing())

        for _ in range(3):
            executor.execute(profile.requests)

        st = executor.stats()["probe"]

        self.assertEqual(st["sent"], 3)
        self.assertEqual(st["ok"], 0)
        self.assertEqual(st["failed"], 3)
        self.assertEqual(st["kinds"], {"transport_nack": 3})
        self.assertIn("will not route", st["last_error"])

    def test_stats_are_a_copy_not_the_live_dict(self):
        """The HTTP thread reads these while the poll loop writes them."""
        from tests.test_mapping_requests import FakeObdReader

        executor = MappingExecutor(
            self.profile, obd_reader=FakeObdReader({0x0C: b"\x0c\x3c"})
        )
        executor.execute([self.profile.request("obd.mode01.0C")])

        snapshot = executor.stats()
        snapshot["obd.mode01.0C"]["ok"] = 999
        snapshot["obd.mode01.0C"]["kinds"]["fake"] = 1

        self.assertEqual(executor.stats()["obd.mode01.0C"]["ok"], 1)
        self.assertEqual(executor.stats()["obd.mode01.0C"]["kinds"], {})


class TheReport(unittest.TestCase):
    def build(self, caps=None, **published):
        registry = engine_registry()
        profile = registry.resolve(
            caps or AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )
        plan = PollingPlan(
            profile.requests, resolve_classes(registry.polling_classes())
        )
        executor = MappingExecutor(profile)
        diag = live.Diagnostics()
        diag.publish(profile=profile, executor=executor, plan=plan,
                     **published)

        return diag, profile, executor

    def test_before_connecting_it_says_so_rather_than_lying(self):
        report = live.Diagnostics().report()

        self.assertFalse(report["ready"])

    def test_a_disconnect_clears_it(self):
        """
        A stale picture is worse than none: the counters would keep
        reading as though the link were live.
        """
        diag, _, _ = self.build()

        self.assertTrue(diag.report()["ready"])

        diag.clear()

        self.assertFalse(diag.report()["ready"])

    def test_every_request_appears_with_where_it_goes(self):
        diag, profile, _ = self.build()
        report = diag.report()

        self.assertEqual(len(report["requests"]), len(profile.requests))

        rpm = next(r for r in report["requests"] if r["id"] == "obd.mode01.0C")

        self.assertEqual(rpm["pid"], "0x0C")
        self.assertEqual(rpm["address"], "0x12")
        self.assertEqual(rpm["class"], "motion")
        self.assertEqual(rpm["signals"], ["rpm"])

    def test_an_unpolled_request_has_no_success_rate_rather_than_zero(self):
        """0% reads as a failure; "not asked yet" is not a failure."""
        diag, _, _ = self.build()
        rpm = next(
            r for r in diag.report()["requests"] if r["id"] == "obd.mode01.0C"
        )

        self.assertEqual(rpm["sent"], 0)
        self.assertIsNone(rpm["success_pct"])

    def test_a_staggered_class_reports_the_per_channel_interval(self):
        """
        Its declared period is the gap between firings of the CLASS; one
        member goes out per firing. Reporting the raw period would
        overstate the DDE reads by ~22x.
        """
        registry = MappingRegistry([
            load_file(support.OBD_MAPPING),
            load_file("mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml"),
        ])
        profile = registry.resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )
        plan = PollingPlan(
            profile.requests, resolve_classes(registry.polling_classes())
        )
        diag = live.Diagnostics()
        diag.publish(profile=profile, executor=MappingExecutor(profile),
                     plan=plan)

        dde = [
            r for r in diag.report()["requests"] if r["class"] == "dde_dyn"
        ]

        self.assertTrue(dde)
        #: 0.5 s per firing x 9 members in this file.
        self.assertAlmostEqual(dde[0]["period_s"], 0.5 * len(dde))

    def test_channels_are_traced_to_their_request(self):
        diag, _, _ = self.build()
        channels = {c["key"]: c for c in diag.report()["channels"]}

        self.assertEqual(channels["rpm"]["request"], "obd.mode01.0C")
        self.assertFalse(channels["rpm"]["derived"])
        self.assertTrue(channels["boost"]["derived"])
        self.assertEqual(channels["boost"]["request"], "")

    def test_the_dropped_list_reaches_the_report(self):
        diag, _, _ = self.build(caps=ObdCapabilitySet({0x0C}))
        dropped = diag.report()["dropped"]

        self.assertTrue(dropped)
        self.assertTrue(all("reason" in d and "detail" in d for d in dropped))

    def test_extra_mappings_are_marked(self):
        """
        The repo's "no proprietary data in the production set" line,
        made visible per run: which files are here only because
        --extra-mappings named them.
        """
        diag, _, _ = self.build(extra_ids={"sae-obd-engine"})

        self.assertTrue(diag.report()["mappings"][0]["extra"])

        diag2, _, _ = self.build(extra_ids=set())

        self.assertFalse(diag2.report()["mappings"][0]["extra"])

    def test_the_mapping_set_fingerprint_includes_the_mode_table(self):
        diag, _, _ = self.build(extra_versions=["drive-modes@1"])

        self.assertIn(
            "drive-modes@1", diag.report()["session"]["mapping_set"]
        )

    def test_totals_aggregate_across_requests(self):
        from tests.test_mapping_requests import FakeObdReader

        registry = engine_registry()
        profile = registry.resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )
        executor = MappingExecutor(
            profile, obd_reader=FakeObdReader({0x0C: b"\x0c\x3c"})
        )
        executor.execute([
            profile.request("obd.mode01.0C"),
            profile.request("obd.mode01.0B"),
        ])

        diag = live.Diagnostics()
        diag.publish(profile=profile, executor=executor)
        totals = diag.report()["totals"]

        self.assertEqual(totals["sent"], 2)
        self.assertEqual(totals["ok"], 1)
        self.assertEqual(totals["failed"], 1)
        self.assertEqual(totals["success_pct"], 50.0)

    def test_the_report_survives_a_missing_executor_or_plan(self):
        """Published in stages; a half-built picture must not 500."""
        registry = engine_registry()
        profile = registry.resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )
        diag = live.Diagnostics()
        diag.publish(profile=profile)

        report = diag.report()

        self.assertTrue(report["ready"])
        self.assertIsNone(report["requests"][0]["period_s"])


class WithoutTheCar(unittest.TestCase):
    """
    What is knowable before a link exists, and must therefore be shown.

    Which files loaded, their versions, which came via --extra-mappings
    and the rates they declare are all settled at boot. Reporting nothing
    until the car answers made the panel unable to distinguish "not
    connected" from "nothing loaded" - and unable to answer "did my extra
    mappings load?", which is a driveway question, not a motorway one.
    """

    def registry(self):
        registry = engine_registry()
        base = {m.id for m in registry.mappings}
        registry.add(load_file(
            "mappings/candidates/bmw/egs/f10_transmission.yaml"
        ))

        return registry, {m.id for m in registry.mappings} - base

    def test_the_mapping_set_is_reported_before_any_connection(self):
        registry, extra = self.registry()
        diag = live.Diagnostics()
        diag.publish(registry=registry, extra_ids=extra)

        report = diag.report()

        self.assertFalse(report["ready"])

        ids = [m["id"] for m in report["loaded"]["mappings"]]

        self.assertIn("sae-obd-engine", ids)
        self.assertIn("candidate-f10-egs-transmission", ids)

    def test_extra_mappings_are_identifiable_without_the_car(self):
        """The whole point: check the launch was right BEFORE driving."""
        registry, extra = self.registry()
        diag = live.Diagnostics()
        diag.publish(registry=registry, extra_ids=extra)

        loaded = {
            m["id"]: m for m in diag.report()["loaded"]["mappings"]
        }

        self.assertTrue(loaded["candidate-f10-egs-transmission"]["extra"])
        self.assertFalse(loaded["sae-obd-engine"]["extra"])

    def test_declared_rates_are_reported_without_the_car(self):
        registry, extra = self.registry()
        diag = live.Diagnostics()
        diag.publish(registry=registry, extra_ids=extra)

        classes = {
            c["name"]: c for c in diag.report()["loaded"]["classes"]
        }

        self.assertEqual(classes["motion"]["period_s"], 0.1)
        self.assertEqual(classes["motion"]["requests"], 4)
        self.assertEqual(classes["rare"]["period_s"], 60.0)

    def test_a_staggered_class_reports_its_per_channel_rate_here_too(self):
        registry = engine_registry()
        registry.add(load_file(
            "mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml"
        ))
        diag = live.Diagnostics()
        diag.publish(registry=registry)

        dde = next(
            c for c in diag.report()["loaded"]["classes"]
            if c["name"] == "dde_dyn"
        )

        self.assertTrue(dde["stagger"])
        #: 0.5s per firing x 9 members in that file.
        self.assertAlmostEqual(dde["period_s"], 0.5 * dde["requests"])

    def test_a_disconnect_keeps_what_does_not_depend_on_the_car(self):
        """
        A stale SESSION reads as live and is worse than none. The mapping
        set is a property of how the process was started, not of the
        link, so it must survive.
        """
        registry, extra = self.registry()
        diag = live.Diagnostics()
        diag.publish(registry=registry, extra_ids=extra)

        profile = registry.resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )
        diag.publish(profile=profile, ecu="DDE")

        self.assertTrue(diag.report()["ready"])

        diag.clear("ConnectionResetError: another tool connected?")
        report = diag.report()

        self.assertFalse(report["ready"])
        self.assertIn("another tool", report["detail"])
        #: session facts gone...
        self.assertNotIn("session", report)
        #: ...loaded facts kept.
        self.assertEqual(len(report["loaded"]["mappings"]), 2)

    def test_a_connected_report_carries_both(self):
        registry, extra = self.registry()
        diag = live.Diagnostics()
        diag.publish(registry=registry, extra_ids=extra)
        diag.publish(profile=registry.resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        ))

        report = diag.report()

        self.assertTrue(report["ready"])
        self.assertIn("loaded", report)
        self.assertIn("mappings", report)

    def test_no_registry_at_all_is_empty_not_a_crash(self):
        self.assertEqual(live.Diagnostics().report()["loaded"]["mappings"], [])


class TwoViewsMustAgree(unittest.TestCase):
    """
    `/api/diagnostics` and `telemetry.channel_errors` count the same
    faults.

    Found on drive 10: the view reported 6 failed requests and the table
    held 3. The missing four were OBD PIDs that `ObdSession` absorbs
    under its own three-strikes policy - the executor counted them but
    never called `on_error`, so nothing reached the database.

    That is worse than either number alone. The table is what analysis
    queries, so it was the under-reporting one, and "how often does this
    channel fail?" - the question channel_errors exists to answer - came
    back wrong for every OBD channel.
    """

    def setUp(self):
        self.registry = engine_registry()
        self.profile = self.registry.resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

    def test_a_dropped_pid_reaches_on_error(self):
        from tests.test_mapping_requests import FakeObdReader

        seen = []
        executor = MappingExecutor(
            self.profile,
            obd_reader=FakeObdReader({0x0C: b"\x0c\x3c"}),   # 0x0B absent
            on_error=lambda rid, exc: seen.append((rid, exc)),
        )

        executor.execute([
            self.profile.request("obd.mode01.0C"),
            self.profile.request("obd.mode01.0B"),
        ])

        self.assertEqual([rid for rid, _ in seen], ["obd.mode01.0B"])

    def test_the_two_counts_match(self):
        """The property, stated directly: reported == counted."""
        from tests.test_mapping_requests import FakeObdReader

        reported = []
        executor = MappingExecutor(
            self.profile,
            obd_reader=FakeObdReader({0x0C: b"\x0c\x3c"}),
            on_error=lambda rid, exc: reported.append(rid),
        )

        for _ in range(3):
            executor.execute([
                self.profile.request("obd.mode01.0C"),
                self.profile.request("obd.mode01.0B"),
                self.profile.request("obd.mode01.0D"),
            ])

        counted = sum(s["failed"] for s in executor.stats().values())

        self.assertEqual(len(reported), counted)
        self.assertEqual(counted, 6)          # two PIDs x three cycles

    def test_a_dropped_pid_keeps_its_own_fault_kind(self):
        """
        `no_response` stays distinguishable from a nack or a timeout: a
        PID the ECU ignores and a gateway that refused to route are
        different diagnoses, and the kind column exists to be grouped by.
        """
        from bmwdiag.mapping.execute import NoResponse
        from tests.test_mapping_requests import FakeObdReader

        kinds = []
        executor = MappingExecutor(
            self.profile,
            obd_reader=FakeObdReader({0x0C: b"\x0c\x3c"}),
            on_error=lambda rid, exc: kinds.append(fault_kind(exc)),
        )

        executor.execute([self.profile.request("obd.mode01.0B")])

        self.assertEqual(kinds, ["no_response"])
        self.assertIsInstance(NoResponse("x"), Exception)


class SuccessRateIsNeverFlattering(unittest.TestCase):
    """
    A success rate is never rounded UP to 100% while a failure exists.

    6963/6964 rounds to 100.0, and drive 10 rendered "100%" beside
    "failed: 1". Not wrong arithmetically, and precisely what makes
    someone stop trusting a panel whose only job is telling them what is
    broken.
    """

    def rate(self, ok, sent):
        registry = engine_registry()
        profile = registry.resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )
        executor = MappingExecutor(profile)
        rid = "obd.mode01.0C"
        st = executor._stat(rid)
        st["sent"], st["ok"], st["failed"] = sent, ok, sent - ok

        diag = live.Diagnostics()
        diag.publish(profile=profile, executor=executor)

        return next(
            r for r in diag.report()["requests"] if r["id"] == rid
        )["success_pct"]

    def test_one_failure_in_seven_thousand_is_not_one_hundred_percent(self):
        self.assertLess(self.rate(6963, 6964), 100.0)

    def test_a_clean_run_still_reads_one_hundred(self):
        """The floor must not make a perfect run look imperfect."""
        self.assertEqual(self.rate(6964, 6964), 100.0)

    def test_ordinary_rates_are_unaffected(self):
        self.assertEqual(self.rate(1, 2), 50.0)
        self.assertEqual(self.rate(0, 3), 0.0)


class NotOnTheShareSurface(unittest.TestCase):
    def test_diagnostics_is_not_share_visible(self):
        """
        It names file paths, ECU addresses and the mapping set. A share
        link is for showing someone your revs, not your box.
        """
        self.assertNotIn("/api/diagnostics", live.SHARE_ALLOWED)


if __name__ == "__main__":
    unittest.main()
