"""
Polling plan.

The plan schedules requests, never signal names. Scheduling is
wall-clock, in seconds, and that is the only unit the format has.
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
  version: 1

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


def obd_profile():
    registry = MappingRegistry([load_file(support.OBD_MAPPING)])
    profile = registry.resolve(
        AllCapabilities(), config={"tank": 70.0},
        targets={"discovered_engine": 0x12},
    )

    return profile, PollingPlan(
        profile.requests, resolve_classes(registry.polling_classes())
    )


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


class TestObdPollingTiers(unittest.TestCase):
    """
    The four tiers of OBD mapping v2.

    v1 had two cycle-based classes (`fast` every cycle, `slow` every Nth)
    inherited from the hand-written dashboard. v2 replaced them with
    wall-clock tiers chosen per channel, because the census showed the
    11 fast PIDs were 83% of stored rows at 0.1-3.8% distinct values.

    These tests assert the tiers exist and behave; the point of the
    change - that far fewer requests are sent - is asserted in
    tests/test_drive_modes.py against the actual schedule.
    """

    @staticmethod
    def declared_plan():
        """
        A plan on the rates the mapping declares.

        Kept separate from `obd_profile()` so these tests read as being
        about the declared tiers specifically.
        """
        registry = MappingRegistry([load_file(support.OBD_MAPPING)])
        profile = registry.resolve(
            AllCapabilities(), config={"tank": 70.0},
            targets={"discovered_engine": 0x12},
        )

        return PollingPlan(
            profile.requests, resolve_classes(registry.polling_classes())
        )

    def test_every_request_is_in_one_of_the_declared_tiers(self):
        plan = self.declared_plan()

        self.assertEqual(
            plan.counts(),
            {"motion": 4, "control_ctx": 2, "context": 5, "slow": 8, "rare": 5},
        )

    def test_motion_runs_every_cycle_at_the_loop_rate(self):
        """10 Hz against a 10 Hz loop means every cycle."""
        plan = self.declared_plan()
        motion = {r.id for r in plan.by_class("motion")}
        now = 1000.0

        for cycle in range(0, 25):
            due = {r.id for r in plan.due(cycle, now)}

            self.assertTrue(
                motion <= due, f"motion request missing at cycle {cycle}"
            )
            now += 0.1

    def test_the_slower_tiers_wait_out_their_period(self):
        plan = self.declared_plan()
        start = 1000.0

        #: Everything is due the first time it is asked for.
        self.assertEqual(len(plan.due(0, start)), 24)

        #
        # Just under a second later, only the 10 Hz tier is. The bound is
        # 0.9 s rather than 1.0 s on purpose: `control_ctx` has a 1 s
        # period, so at exactly 1.0 s it is legitimately due and this
        # would be asserting the wrong thing - and relying on its phase
        # offset to hide that would be luck, not a test.
        #
        for i in range(1, 10):
            due = plan.due(i, start + i * 0.1)

            self.assertEqual(
                [r.polling_class for r in due], ["motion"] * 4, f"cycle {i}"
            )

        #
        # At ten seconds context and slow come round again - but no longer
        # all at the same instant. Phase spreading gives each request its
        # own offset inside the period, so they arrive across the window
        # rather than as one burst. Collected over the full period, the
        # set is unchanged; `rare` still does not appear.
        #
        seen = set()

        for i in range(101):
            seen |= {r.polling_class for r in plan.due(100 + i,
                                                       start + 10.0 + i * 0.1)}

        self.assertEqual(seen, {"motion", "control_ctx", "context", "slow"})

        #
        # At sixty, everything - again gathered across the period rather
        # than demanded at one instant, because `rare` is phased too. The
        # tier still comes round; it simply no longer piles onto the same
        # wall-clock tick as context and slow, which is the burst this
        # change removes.
        #
        seen = set()

        for i in range(601):
            seen |= {r.polling_class for r in plan.due(600 + i,
                                                       start + 60.0 + i * 0.1)}

        self.assertEqual(
            seen, {"motion", "control_ctx", "context", "slow", "rare"}
        )

    def test_due_order_is_by_tier_then_declaration(self):
        """The OBD session batches in list order; keep it deterministic."""
        plan = self.declared_plan()
        due = plan.due(0, 1000.0)

        self.assertEqual(
            [r.polling_class for r in due],
            ["motion"] * 4
            #: control_ctx shares priority 1 with context, so the two
            #: interleave by declaration order. Still deterministic,
            #: which is all the OBD batching needs.
            + ["control_ctx", "context", "control_ctx"] + ["context"] * 4
            + ["slow"] * 8 + ["rare"] * 5,
        )
        self.assertEqual(due[0].id, "obd.mode01.0C")

    def test_the_mapping_file_declares_the_rates(self):
        """
        There is no CLI rate override any more. A rate is a property of
        what the channel measures, so it lives in the mapping; wanting
        everything faster for one drive is what a mode is for - and
        unlike a flag, a mode is recorded with the data.
        """
        registry = MappingRegistry([load_file(support.OBD_MAPPING)])
        declared = {c.name: c for c in registry.polling_classes()}

        self.assertEqual(declared["motion"].period, 0.1)
        self.assertEqual(declared["slow"].period, 10.0)
        self.assertEqual(declared["rare"].period, 60.0)


class TestWallClockPeriods(unittest.TestCase):
    """
    One unit: seconds. `hz`, `every` and `cycles` were retired on
    2026-08-30 - the first two were the same thing spelled differently,
    and `cycles` silently rescaled every class when --rate changed.
    """

    def plan(self, class_def):
        text = GROUPED.replace("{class: fast}", "{class: paced}").replace(
            "{class: slow}", "{class: paced}"
        )
        mapping = load_text(text, "test")
        classes = resolve_classes([class_def])

        return PollingPlan(mapping.requests, classes)

    def test_a_fast_period_respects_wall_clock(self):
        plan = self.plan(PollingClassDef("paced", 0.2, 0))

        self.assertEqual(len(plan.due(0, now=1000.0)), 2)
        self.assertEqual(len(plan.due(1, now=1000.1)), 0)
        self.assertEqual(len(plan.due(2, now=1000.2)), 2)

    def test_a_slow_period_respects_wall_clock(self):
        #
        # Phase spreading offsets the FIRST interval, so a request is not
        # guaranteed to come round at exactly t=period any more - it
        # comes round at period + its own phase, which is at most one
        # further period. What must still hold, and is what this pins, is
        # that nothing fires EARLY: phase can only ever delay a request,
        # so it cannot raise request volume. See PHASE_MIN_PERIOD.
        #
        plan = self.plan(PollingClassDef("paced", 5.0, 0))

        self.assertEqual(len(plan.due(0, now=0.0)), 2)
        self.assertEqual(len(plan.due(1, now=4.9)), 0)

        #: one period's worth of cycles: phase is < period, so each
        #: request comes round exactly once inside [period, 2*period)
        fired = 0

        for i, t in enumerate([5.0 + 0.1 * k for k in range(50)]):
            fired += len(plan.due(2 + i, now=t))

        self.assertEqual(fired, 2, "each request comes round exactly once")

    def test_hz_is_available_for_display_only(self):
        self.assertEqual(PollingClassDef("paced", 0.1, 0).hz, 10.0)
        self.assertEqual(PollingClassDef("paced", 60.0, 0).hz, 1 / 60)

    def test_the_retired_units_are_refused_by_name(self):
        """
        A file still carrying `hz: 10` must not load with a default
        period and poll at the wrong rate in silence.
        """
        from bmwdiag.mapping.errors import InvalidFieldError

        for retired in ("hz: 5.0", "every: 5", "cycles: 5"):
            with self.subTest(retired=retired):
                text = GROUPED.replace(
                    "requests:",
                    "polling_classes:\n  fast: {" + retired + "}\n\nrequests:",
                    1,
                )

                with self.assertRaises(InvalidFieldError) as caught:
                    load_text(text, "test")

                self.assertIn(retired.split(":")[0], str(caught.exception))

    def test_the_example_fixture_declares_periods(self):
        mapping = load_file(support.EXAMPLE_MAPPING)
        classes = {c.name: c for c in mapping.polling_classes}

        self.assertEqual(classes["medium_hz"].period, 0.2)
        self.assertEqual(classes["trickle"].period, 5.0)


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
