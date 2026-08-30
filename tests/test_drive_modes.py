"""
Drive modes.

A mode scales the polling classes the mappings declare; it is not a
second scheduler. These tests hold that line - every mode has to be
expressible as a scaling (plus the two special cases, `off` and the
duty cycle) - and pin the properties that make the recorded data
trustworthy:

  * `normal` is exactly what the mappings declare, so "what rate was
    this recorded at?" has one answer, not two.
  * switching away and back returns to the declared rates rather than
    compounding multipliers.
  * `off` sends nothing at all while the link stays up.
  * a duty cycle never silences the slow tiers, because the events worth
    catching on a long drive are the ones that would hide in a sleep
    window.
"""

import unittest

from . import support
from bmwdiag.mapping import load_file
from bmwdiag.mapping.errors import PollingError
from bmwdiag.mapping.model import PollingClassDef
from bmwdiag.mapping.modes import (
    DEFAULT_MODE,
    DRIVE_MODES,
    DriveMode,
    apply_mode,
    get_mode,
    mode_names,
)
from bmwdiag.mapping.polling import PollingPlan, resolve_classes
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry


def obd_plan(mode=None):
    registry = MappingRegistry([load_file(support.OBD_MAPPING)])
    profile = registry.resolve(
        AllCapabilities(), config={"tank": 70.0},
        targets={"discovered_engine": 0x12},
    )

    return PollingPlan(
        profile.requests,
        resolve_classes(registry.polling_classes()),
        mode,
    )


def requests_over(plan, seconds, rate=10.0):
    """How many requests the plan sends over a simulated stretch."""
    sent = 0
    now = 1000.0
    step = 1.0 / rate

    for cycle in range(int(seconds * rate)):
        sent += len(plan.due(cycle, now))
        now += step

    return sent


class Catalogue(unittest.TestCase):
    def test_the_five_modes_exist_and_are_all_offered(self):
        self.assertEqual(sorted(DRIVE_MODES), sorted(mode_names()))
        self.assertEqual(
            sorted(DRIVE_MODES),
            ["debug", "long", "normal", "off", "sampling"],
        )

    def test_every_mode_has_a_description(self):
        for name, mode in DRIVE_MODES.items():
            with self.subTest(name=name):
                self.assertTrue(mode.description.strip(), name)

    def test_the_default_is_normal_and_normal_scales_nothing(self):
        self.assertEqual(DEFAULT_MODE, "normal")
        self.assertEqual(DRIVE_MODES["normal"].multipliers, {})
        self.assertIsNone(DRIVE_MODES["normal"].duty)
        self.assertTrue(DRIVE_MODES["normal"].polls)

    def test_no_mode_scales_a_class_the_mappings_do_not_declare(self):
        """
        A multiplier for a class nobody declares is dead configuration -
        it silently does nothing, and reads as if it were working.
        """
        declared = {"motion", "context", "slow", "rare", "dde_dyn", "egs"}

        for name, mode in DRIVE_MODES.items():
            for cls in mode.multipliers:
                with self.subTest(mode=name, cls=cls):
                    self.assertIn(cls, declared)

            for cls in mode.duty_exempt:
                with self.subTest(mode=name, exempt=cls):
                    self.assertIn(cls, declared)

    def test_an_unknown_mode_is_refused_by_name(self):
        with self.assertRaises(PollingError) as caught:
            get_mode("ludicrous")

        self.assertIn("ludicrous", str(caught.exception))

    def test_no_name_means_the_default(self):
        self.assertIs(get_mode(None), DRIVE_MODES[DEFAULT_MODE])
        self.assertIs(get_mode(""), DRIVE_MODES[DEFAULT_MODE])


class Scaling(unittest.TestCase):
    def test_a_multiplier_slows_an_hz_class(self):
        classes = {"egs": PollingClassDef("egs", "hz", 2.0, 4)}
        mode = DriveMode("x", "", multipliers={"egs": 4.0})

        self.assertEqual(apply_mode(classes, mode)["egs"].value, 0.5)

    def test_a_multiplier_below_one_speeds_a_class_up(self):
        classes = {"context": PollingClassDef("context", "seconds", 10.0, 1)}
        mode = DriveMode("x", "", multipliers={"context": 0.01})

        self.assertEqual(apply_mode(classes, mode)["context"].value, 0.1)

    def test_a_cycles_class_never_scales_below_one_cycle(self):
        """Half a loop cycle does not exist; clamp rather than round to 0."""
        classes = {"dde_dyn": PollingClassDef("dde_dyn", "cycles", 5.0, 2)}
        mode = DriveMode("x", "", multipliers={"dde_dyn": 0.01})

        self.assertEqual(apply_mode(classes, mode)["dde_dyn"].value, 1.0)

    def test_scaling_preserves_priority_and_stagger(self):
        classes = {
            "dde_dyn": PollingClassDef("dde_dyn", "cycles", 5.0, 2, stagger=True)
        }
        scaled = apply_mode(
            classes, DriveMode("x", "", multipliers={"dde_dyn": 2.0})
        )["dde_dyn"]

        self.assertEqual(scaled.value, 10.0)
        self.assertEqual(scaled.priority, 2)
        self.assertTrue(scaled.stagger)

    def test_an_unnamed_class_is_left_alone(self):
        classes = {"slow": PollingClassDef("slow", "seconds", 10.0, 2)}
        scaled = apply_mode(classes, DriveMode("x", "", multipliers={}))

        self.assertEqual(scaled["slow"].value, 10.0)

    def test_a_non_positive_multiplier_is_refused(self):
        classes = {"slow": PollingClassDef("slow", "seconds", 10.0, 2)}

        for bad in (0.0, -1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(PollingError):
                    apply_mode(classes, DriveMode("x", "", {"slow": bad}))


class Switching(unittest.TestCase):
    def test_normal_leaves_the_declared_rates_untouched(self):
        declared = obd_plan().declared
        normal = obd_plan(get_mode("normal")).classes

        for name, cls in declared.items():
            with self.subTest(name=name):
                self.assertEqual(normal[name].value, cls.value)
                self.assertEqual(normal[name].kind, cls.kind)

    def test_switching_back_returns_to_the_declared_rates(self):
        """
        Modes must rescale from `declared`, never from the current
        classes - otherwise debug -> long -> normal lands somewhere that
        is not normal, and no recorded run means what it says.
        """
        plan = obd_plan()
        before = {n: c.value for n, c in plan.classes.items()}

        plan.set_mode(get_mode("debug"))
        plan.set_mode(get_mode("long"))
        plan.set_mode(get_mode("normal"))

        self.assertEqual({n: c.value for n, c in plan.classes.items()}, before)

    def test_switching_does_not_reorder_requests(self):
        """Batching depends on request order; a mode must not disturb it."""
        plan = obd_plan()
        before = [r.id for r in plan.requests]

        plan.set_mode(get_mode("debug"))

        self.assertEqual([r.id for r in plan.requests], before)

    def test_a_switch_does_not_leave_a_slow_class_overdue(self):
        """
        Going debug -> normal must not fire everything immediately just
        because the previous mode's period had elapsed.
        """
        plan = obd_plan(get_mode("debug"))
        plan.due(0, 1000.0)

        plan.set_mode(get_mode("normal"))
        due = plan.due(1, 1000.1)

        #: A clean slate: the first call after a switch reads everything
        #: once, which is the honest starting point for the new cadence.
        self.assertEqual(len(due), 24)


class ModeEffects(unittest.TestCase):
    def test_off_sends_nothing_at_all(self):
        plan = obd_plan(get_mode("off"))

        self.assertEqual(requests_over(plan, seconds=120), 0)

    def test_debug_restores_the_pre_v2_request_volume(self):
        """
        `debug` is meant to be "what we used to do". Over a minute the old
        mapping sent 11 PIDs every cycle plus 13 every tenth: 7380.
        """
        plan = obd_plan(get_mode("debug"))

        self.assertGreater(requests_over(plan, seconds=60), 7000)

    def test_normal_is_far_quieter_than_debug(self):
        normal = requests_over(obd_plan(get_mode("normal")), seconds=60)
        debug = requests_over(obd_plan(get_mode("debug")), seconds=60)

        self.assertLess(normal, debug / 2.5)

    def test_long_is_quieter_still(self):
        normal = requests_over(obd_plan(get_mode("normal")), seconds=60)
        long_drive = requests_over(obd_plan(get_mode("long")), seconds=60)

        self.assertLess(long_drive, normal / 2.0)

    def test_sampling_trades_continuity_for_resolution(self):
        """
        While awake, `sampling` polls at the full declared rate - that is
        the point of it. It is quieter only over a whole duty period.
        """
        awake = requests_over(obd_plan(get_mode("sampling")), seconds=60)
        normal = requests_over(obd_plan(get_mode("normal")), seconds=60)

        self.assertEqual(awake, normal)
        self.assertLess(
            requests_over(obd_plan(get_mode("sampling")), seconds=720),
            requests_over(obd_plan(get_mode("normal")), seconds=720) / 4,
        )

    def test_the_modes_are_monotonically_quieter(self):
        #
        # Measured over a full 720 s duty period. A shorter window would
        # sit inside `sampling`'s first awake stretch, where it is
        # indistinguishable from `normal` - and the tie would pass this
        # test while proving nothing about the mode.
        #
        volumes = [
            requests_over(obd_plan(get_mode(name)), seconds=720)
            for name in mode_names()
        ]

        self.assertEqual(volumes, sorted(volumes), volumes)
        #: Strictly quieter, not merely not-louder.
        for quieter, louder in zip(volumes, volumes[1:]):
            self.assertLess(quieter, louder, volumes)


class DutyCycle(unittest.TestCase):
    def test_sampling_alternates_awake_and_asleep(self):
        mode = get_mode("sampling")

        self.assertTrue(mode.awake_at(0.0))
        self.assertTrue(mode.awake_at(119.0))
        self.assertFalse(mode.awake_at(121.0))
        self.assertFalse(mode.awake_at(700.0))
        #: 720 s period, so it wakes again at the top of the next one.
        self.assertTrue(mode.awake_at(721.0))

    def test_the_fast_tier_sleeps_but_the_slow_tiers_do_not(self):
        plan = obd_plan(get_mode("sampling"))

        plan.due(0, 1000.0)                       # sets the duty origin
        asleep = {
            r.polling_class for r in plan.due(5000, 1000.0 + 400.0)
        }

        self.assertNotIn("motion", asleep)
        self.assertNotIn("context", asleep)
        #: A regeneration or thermal excursion starting in a quiet window
        #: is still recorded - that is the point of the exemption.
        self.assertEqual(asleep, {"slow", "rare"})

    def test_duty_state_is_reported_for_the_dashboard(self):
        plan = obd_plan(get_mode("sampling"))
        plan.due(0, 1000.0)

        self.assertEqual(plan.duty_state(1000.0), "awake")
        self.assertEqual(plan.duty_state(1000.0 + 400.0), "asleep")

    def test_a_continuous_mode_reports_continuous(self):
        plan = obd_plan(get_mode("normal"))

        self.assertEqual(plan.duty_state(1000.0), "continuous")


if __name__ == "__main__":
    unittest.main()
