"""
Polling plan.

The plan schedules requests, never signal names, and reproduces the
fast/slow cadence the --rate/--slow-every CLI is defined in terms of.
"""

import unittest

from . import support
from bmwdiag.mapping import load_file, load_text
from bmwdiag.mapping.errors import PollingError
from bmwdiag.mapping.model import PollingClassDef
from bmwdiag.mapping.polling import DEFAULT_POLLING_CLASSES, PollingPlan, resolve_classes
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry

#: Two signals out of one response, so the plan has something to group.
GROUPED = """
schema_version: 1

mapping:
  id: grouped-test

ecu:
  family: test
  target: 0x7E

requests:
  block.one:
    protocol: uds
    service: 0x22
    did: 0xF001
    polling: {class: fast}
    response: {data_length: 4}
    signals:
      first:
        decode: {type: uint16_be, offset: 0}
      second:
        decode: {type: uint16_be, offset: 2}

  block.two:
    protocol: uds
    service: 0x22
    did: 0xF002
    polling: {class: slow}
    response: {data_length: 2}
    signals:
      third:
        decode: {type: uint16_be, offset: 0}
"""


def obd_profile(slow_every=10):
    registry = MappingRegistry([load_file(support.OBD_MAPPING)])
    profile = registry.resolve(
        AllCapabilities(), config={"tank": 70.0},
        targets={"discovered_engine": 0x12},
    )
    classes = resolve_classes(
        registry.polling_classes(),
        {"slow": PollingClassDef("slow", "cycles", float(slow_every), 1)},
    )

    return profile, PollingPlan(profile.requests, classes)


class TestGrouping(unittest.TestCase):
    def test_signals_sharing_a_request_cost_one_request(self):
        mapping = load_text(GROUPED, "test")
        plan = PollingPlan(mapping.requests)

        due = plan.due(0)

        self.assertEqual([r.id for r in due], ["block.one", "block.two"])
        self.assertEqual(
            sorted(plan.signal_keys()), ["first", "second", "third"]
        )
        #
        # Three channels, two exchanges.
        #
        self.assertEqual(len(plan.requests), 2)
        self.assertEqual(len(plan.signal_keys()), 3)

    def test_adding_a_signal_to_an_existing_request_adds_no_traffic(self):
        before = PollingPlan(load_text(GROUPED, "test").requests)
        extended = GROUPED.replace(
            "      second:\n        decode: {type: uint16_be, offset: 2}",
            "      second:\n        decode: {type: uint8, offset: 2}\n"
            "      fourth:\n        decode: {type: uint8, offset: 3}",
        )
        after = PollingPlan(load_text(extended, "test").requests)

        self.assertEqual(len(after.due(0)), len(before.due(0)))
        self.assertEqual(len(after.signal_keys()), len(before.signal_keys()) + 1)

    def test_the_same_pid_is_never_requested_twice(self):
        from bmwdiag.mapping import MappingExecutor
        from tests.test_mapping_requests import FakeObdReader

        profile, plan = obd_profile()
        reader = FakeObdReader({0x0C: b"\x0c\x3c"})
        executor = MappingExecutor(profile, obd_reader=reader)
        request = profile.request("obd.mode01.0C")

        executor.execute([request, request])

        self.assertEqual(reader.calls, [[0x0C]])


class TestFastSlowCompatibility(unittest.TestCase):
    def test_class_membership_matches_the_historical_table(self):
        _, plan = obd_profile()
        counts = plan.counts()

        self.assertEqual(counts, {"fast": 11, "slow": 13})

    def test_fast_runs_every_cycle(self):
        _, plan = obd_profile()
        fast = {r.id for r in plan.by_class("fast")}

        for cycle in range(0, 25):
            due = {r.id for r in plan.due(cycle)}

            self.assertTrue(fast <= due, f"fast request missing at cycle {cycle}")

    def test_slow_runs_every_nth_cycle(self):
        for slow_every in (1, 2, 5, 10, 30):
            with self.subTest(slow_every=slow_every):
                _, plan = obd_profile(slow_every)
                slow = {r.id for r in plan.by_class("slow")}

                for cycle in range(0, 60):
                    due = {r.id for r in plan.due(cycle)}
                    expected = cycle % slow_every == 0

                    self.assertEqual(
                        slow <= due, expected,
                        f"slow_every={slow_every} cycle={cycle}",
                    )

    def test_cycle_zero_reads_everything(self):
        _, plan = obd_profile()

        self.assertEqual(len(plan.due(0)), 24)
        self.assertEqual(len(plan.due(1)), 11)

    def test_due_order_is_fast_then_slow(self):
        """The OBD session batches in list order; keep it deterministic."""
        _, plan = obd_profile()
        due = plan.due(0)
        classes = [r.polling_class for r in due]

        self.assertEqual(classes, ["fast"] * 11 + ["slow"] * 13)
        self.assertEqual(due[0].id, "obd.mode01.0C")

    def test_cli_slow_every_overrides_the_mapping_file(self):
        registry = MappingRegistry([load_file(support.OBD_MAPPING)])
        declared = {c.name: c for c in registry.polling_classes()}

        self.assertEqual(declared["slow"].value, 10.0)

        classes = resolve_classes(
            registry.polling_classes(),
            {"slow": PollingClassDef("slow", "cycles", 3.0, 1)},
        )

        self.assertEqual(classes["slow"].value, 3.0)


class TestRateBasedClasses(unittest.TestCase):
    """Future mappings must be able to ask for 5 Hz or 0.2 Hz."""

    def plan(self, class_def):
        text = GROUPED.replace("{class: fast}", "{class: paced}").replace(
            "{class: slow}", "{class: paced}"
        )
        mapping = load_text(text, "test")
        classes = resolve_classes([class_def])

        return PollingPlan(mapping.requests, classes)

    def test_hz_class_respects_wall_clock(self):
        plan = self.plan(PollingClassDef("paced", "hz", 5.0, 0))

        self.assertEqual(len(plan.due(0, now=1000.0)), 2)
        self.assertEqual(len(plan.due(1, now=1000.1)), 0)
        self.assertEqual(len(plan.due(2, now=1000.2)), 2)

    def test_slow_hz_class(self):
        plan = self.plan(PollingClassDef("paced", "hz", 0.2, 0))

        self.assertEqual(len(plan.due(0, now=0.0)), 2)
        self.assertEqual(len(plan.due(1, now=4.9)), 0)
        self.assertEqual(len(plan.due(2, now=5.0)), 2)

    def test_seconds_class(self):
        plan = self.plan(PollingClassDef("paced", "seconds", 2.0, 0))

        self.assertEqual(len(plan.due(0, now=100.0)), 2)
        self.assertEqual(len(plan.due(1, now=101.0)), 0)
        self.assertEqual(len(plan.due(2, now=102.0)), 2)

    def test_example_fixture_declares_rate_based_classes(self):
        mapping = load_file(support.EXAMPLE_MAPPING)
        classes = {c.name: c for c in mapping.polling_classes}

        self.assertEqual(classes["medium_hz"].kind, "hz")
        self.assertEqual(classes["medium_hz"].value, 5.0)
        self.assertEqual(classes["trickle"].kind, "seconds")


class TestPlanValidation(unittest.TestCase):
    def test_unknown_class_is_reported(self):
        mapping = load_text(GROUPED.replace("class: fast", "class: nope"), "test")

        with self.assertRaises(PollingError) as ctx:
            PollingPlan(mapping.requests)

        self.assertIn("nope", str(ctx.exception))

    def test_defaults_cover_fast_and_slow(self):
        names = {c.name for c in DEFAULT_POLLING_CLASSES}

        self.assertEqual(names, {"fast", "slow"})


if __name__ == "__main__":
    unittest.main()
