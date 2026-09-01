"""
Pair scheduling, phase spreading, and what they cost.

Two acquisition defects, both measured against the alignment contract
that #7 already established rather than against a fresh opinion:

  * `n47d_rail_act` and `n47d_rail_set` landed three rotation slots
    apart - 1.5 s - which is outside the 1.0 s window their pairing
    declares, so rail act-vs-setpoint had ~0% usable coverage. Boost
    landed in adjacent slots and worked. Neither outcome was chosen:
    both fell out of the order the loader happened to produce across
    files, and a reordering could have swapped which pair worked.

  * every non-staggered request fired on the same wall-clock instant,
    so `context` and `slow` (both 10 s) and `rare` (60 s) piled into one
    cycle - 26 requests at every minute boundary against a baseline of 4.

The alignment numbers here come from `analysis.alignment.align`, the same
matcher the analysis layer uses. There is deliberately no second
implementation: a scheduler that looked good under its own private
definition of "close enough" would prove nothing.
"""

import unittest

from tests import support  # noqa: F401

from analysis.alignment import align, pairing_for
from bmwdiag.mapping import MappingRegistry, load_file
from bmwdiag.mapping.modes import load_modes
from bmwdiag.mapping.polling import (
    PHASE_MIN_PERIOD,
    PollingPlan,
    resolve_classes,
)
from bmwdiag.mapping.registry import AllCapabilities

CAR_FILES = ["mappings/obd/engine.yaml"] + [
    "mappings/candidates/bmw/dde/n47/d72n47a0_%s.yaml" % name
    for name in ("dynamic", "flow", "dpf_egr", "gearbox")
] + ["mappings/candidates/bmw/egs/f10_transmission.yaml"]


def car_plan(mode=None):
    """The full set the car actually runs, as ./run_car.sh loads it."""
    import os

    registry = MappingRegistry([
        load_file(os.path.join(support.ROOT, f)) for f in CAR_FILES
    ])
    profile = registry.resolve(
        AllCapabilities(), config={"tank": 70.0},
        targets={"discovered_engine": 0x12},
    )
    plan = PollingPlan(
        profile.requests, resolve_classes(registry.polling_classes()),
        mode=mode,
    )

    return profile, plan


def simulate(plan, seconds=600.0, rate=0.1):
    """Run the scheduler on a synthetic clock; return firings per request."""
    fired = {}
    per_cycle = []
    now, cycle = 0.0, 0

    while now < seconds:
        due = plan.due(cycle, now)
        per_cycle.append(len(due))

        for request in due:
            fired.setdefault(request.id, []).append(now)

        now += rate
        cycle += 1

    return fired, per_cycle


def owner_of(profile):
    return {s.key: r.id for r in profile.requests for s in r.signals}


def gap_and_coverage(fired, a_id, b_id, window):
    """Median gap and coverage, via the SHARED alignment matcher."""
    a = [(t, 0.0) for t in fired.get(a_id, [])]
    b = [(t, 0.0) for t in fired.get(b_id, [])]

    if not a or not b:
        return None, 0.0

    return align(a, b, 1e9).median_gap_s, align(a, b, window).coverage_pct


class CriticalPairsAreScheduledTogether(unittest.TestCase):
    def setUp(self):
        self.profile, self.plan = car_plan()
        self.fired, self.per_cycle = simulate(self.plan)
        self.owner = owner_of(self.profile)

    def _pair(self, a, b):
        rule = pairing_for(a, b)
        med, cov = gap_and_coverage(
            self.fired, self.owner[a], self.owner[b], rule.max_age_s
        )

        return med, cov, rule.max_age_s

    def test_rail_actual_and_setpoint_land_in_the_same_cycle(self):
        #
        # The defect this change exists for: 1.5 s apart, 0% coverage
        # inside the 1.0 s window the contract declares for a control
        # loop. Same cycle means the recorder stamps them with one
        # timestamp, so the gap is zero rather than merely small.
        #
        med, cov, window = self._pair("n47d_rail_act", "n47d_rail_set")

        self.assertEqual(med, 0.0)
        self.assertEqual(cov, 100.0)
        self.assertEqual(window, 1.0)

    def test_boost_is_no_worse_than_it_already_was(self):
        #
        # Boost already met its contract by accident of ordering. The
        # requirement is that pairing does not regress it; that it also
        # closes the residual 0.5 s is a bonus, not the justification.
        #
        med, cov, _ = self._pair("n47d_boost_act", "n47d_boost_set")

        self.assertEqual(cov, 100.0)
        self.assertLessEqual(med, 0.5)

    def test_paired_requests_are_emitted_in_one_firing(self):
        """Directly, rather than inferred from the timing."""
        #: a FRESH plan - the one in setUp has already been advanced to
        #: t=600 by the simulation, so replaying from zero sees nothing
        _, plan = car_plan()
        rail = {self.owner["n47d_rail_act"], self.owner["n47d_rail_set"]}
        together = False

        for cycle in range(200):
            ids = {r.id for r in plan.due(cycle, cycle * 0.1)}

            if rail & ids:
                self.assertEqual(
                    rail & ids, rail,
                    "one half of a pair went out without the other",
                )
                together = True

        self.assertTrue(together, "the pair never fired at all")

    def test_an_unpaired_staggered_member_still_goes_out_alone(self):
        #
        # The round-robin for everything else must be untouched: pairing
        # is an exception for declared pairs, not a new default.
        #
        _, plan = car_plan()
        solo = self.owner["n47d_oil_temp"]
        seen_alone = False

        for cycle in range(200):
            due = [r for r in plan.due(cycle, cycle * 0.1)
                   if r.polling_class == "dde_dyn"]

            if any(r.id == solo for r in due):
                self.assertEqual(len(due), 1)
                seen_alone = True

        self.assertTrue(seen_alone)

    def test_the_pair_costs_one_slot_not_one_firing(self):
        #
        # A pair shortens the rotation by one slot; it does NOT make the
        # class fire more often. That is what keeps the extra wire cost
        # to a few requests a minute rather than doubling the class.
        #
        members = len(self.plan.by_class("dde_dyn"))
        slots = len(self.plan._slots["dde_dyn"])

        self.assertEqual(members, 23)
        self.assertEqual(slots, 21, "two pairs collapse four slots into two")


class PhaseSpreadingRemovesTheBurst(unittest.TestCase):
    def setUp(self):
        self.profile, self.plan = car_plan()
        self.fired, self.per_cycle = simulate(self.plan)

    def test_the_steady_state_burst_is_small(self):
        #
        # Before: 26 requests on every minute boundary, against a
        # baseline of 4. The first cycle is excluded deliberately - every
        # channel's opening value is wanted at startup, and deferring a
        # 60 s class by up to a minute to avoid one burst would cost real
        # data to buy tidiness.
        #
        steady = self.per_cycle[1:]

        self.assertLessEqual(max(steady), 10)

    def test_the_first_cycle_still_reads_everything(self):
        self.assertGreater(self.per_cycle[0], 20)

    def test_phase_is_deterministic_across_plans(self):
        #: same configuration, same schedule - not jitter
        _, second = car_plan()
        fired_b, _ = simulate(second)

        self.assertEqual(self.fired, fired_b)

    def test_phase_never_makes_a_request_fire_early(self):
        #
        # Phase lengthens the first interval, never shortens it, so it
        # cannot raise request volume. Every observed interval must be at
        # least the declared period.
        #
        classes = self.plan.classes

        for request in self.profile.requests:
            cls = classes[request.polling_class]

            if cls.stagger:
                continue

            times = self.fired.get(request.id, [])
            gaps = [b - a for a, b in zip(times, times[1:])]

            for gap in gaps:
                self.assertGreaterEqual(
                    round(gap, 3), cls.period - 0.01,
                    f"{request.id} fired early",
                )

    def test_fast_classes_are_not_phased(self):
        #
        # Phasing a class at or near the loop rate would push members
        # onto alternate cycles and halve their rate - the bug
        # SCHEDULE_SLACK exists to prevent.
        #
        self.assertEqual(self.plan._phase("anything", 0.1), 0.0)
        self.assertEqual(self.plan._phase("anything", PHASE_MIN_PERIOD - 0.01), 0.0)
        self.assertGreater(self.plan._phase("obd.mode01.05", 10.0), 0.0)

    def test_phase_stays_inside_the_period(self):
        for request_id in ("a", "obd.mode01.05", "n47.d72.dyn.4746", "zzz"):
            for period in (1.0, 10.0, 60.0):
                phase = self.plan._phase(request_id, period)

                self.assertGreaterEqual(phase, 0.0)
                self.assertLess(phase, period)


class ContextIsFastEnoughToConditionControlLoops(unittest.TestCase):
    def setUp(self):
        self.profile, self.plan = car_plan()
        self.fired, _ = simulate(self.plan)
        self.owner = owner_of(self.profile)

    def test_load_and_maf_can_condition_a_control_loop_metric(self):
        #
        # At 10 s these gave 19.2% coverage inside a 1 s window - below
        # the 50% the alignment contract calls usable, so a baseline
        # conditioned on load reported "cannot be concluded".
        #
        for ctx in ("load", "maf"):
            with self.subTest(channel=ctx):
                _, cov = gap_and_coverage(
                    self.fired, self.owner["n47d_boost_act"],
                    self.owner[ctx], 1.0,
                )

                self.assertGreaterEqual(cov, 90.0)

    def test_only_the_two_justified_channels_were_promoted(self):
        #
        # The tier is not a dumping ground. Anything else joining it
        # should be a deliberate edit with its own measured reason.
        #
        promoted = {
            s.key for r in self.plan.by_class("control_ctx") for s in r.signals
        }

        self.assertEqual(promoted, {"load", "maf"})

    def test_the_rest_of_context_is_unchanged(self):
        rest = {
            s.key for r in self.plan.by_class("context") for s in r.signals
        }

        self.assertEqual(
            rest, {"throttle", "rail", "torque", "relthr", "lambda"}
        )


class TheLinkLoadDidNotGoBackUp(unittest.TestCase):
    def test_normal_stays_far_below_the_old_global_fast_loop(self):
        #
        # The pre-2026-08-30 loop was 7,740 requests/min. The point of
        # this change is control-loop alignment, not a return to that.
        #
        _, plan = car_plan()
        _, per_cycle = simulate(plan, seconds=600.0)
        per_minute = sum(per_cycle) / 600.0 * 60.0

        self.assertLess(per_minute, 3200)
        self.assertGreater(per_minute, 2000)

    def test_the_increase_over_the_previous_plan_is_small(self):
        #
        # Measured: 2,735 -> 2,854 requests/min, +4.4%. Pinned loosely so
        # a future change that quietly doubles the load fails here.
        #
        _, plan = car_plan()
        _, per_cycle = simulate(plan, seconds=600.0)

        self.assertLess(sum(per_cycle) / 600.0 * 60.0, 2735 * 1.10)


class DriveModesStillBehave(unittest.TestCase):
    def setUp(self):
        self.table = load_modes()

    def test_every_mode_still_resolves_and_polls_as_declared(self):
        for name in ("normal", "long", "sampling", "debug", "off"):
            with self.subTest(mode=name):
                mode = self.table.get(name)
                _, plan = car_plan(mode=mode)
                _, per_cycle = simulate(plan, seconds=120.0)
                total = sum(per_cycle)

                if name == "off":
                    self.assertEqual(total, 0)
                else:
                    self.assertGreater(total, 0)

    def test_long_is_lighter_than_normal_and_debug_heavier(self):
        counts = {}

        for name in ("long", "normal", "debug"):
            _, plan = car_plan(mode=self.table.get(name))
            _, per_cycle = simulate(plan, seconds=120.0)
            counts[name] = sum(per_cycle)

        self.assertLess(counts["long"], counts["normal"])
        self.assertGreater(counts["debug"], counts["normal"])

    def test_the_new_tier_is_scaled_by_the_modes_that_scale_context(self):
        #
        # A class the mode table does not mention is silently left at its
        # declared rate, which would make `long` quietly heavier than
        # intended. Both modes that scale `context` must scale this too.
        #
        for name in ("long", "debug"):
            with self.subTest(mode=name):
                mode = self.table.get(name)

                self.assertIn("control_ctx", mode.multipliers)

    def test_pairs_survive_a_mode_change(self):
        #: rescaling must not rebuild the rotation into single slots
        _, plan = car_plan(mode=self.table.get("normal"))
        plan.set_mode(self.table.get("long"))

        self.assertEqual(len(plan._slots["dde_dyn"]), 21)


class TheConfigurationChangeIsRecorded(unittest.TestCase):
    """
    Sampling configuration is provenance. A run recorded under the new
    cadence must be distinguishable from one recorded under the old, or
    the two get pooled into one baseline.
    """

    def test_the_mode_table_version_moved(self):
        #: control_ctx multipliers are new numbers in that file
        self.assertGreaterEqual(load_modes().version, 2)

    def test_the_engine_mapping_version_moved(self):
        import os

        mapping = load_file(os.path.join(support.ROOT, "mappings/obd/engine.yaml"))

        self.assertGreaterEqual(int(mapping.version), 5)

    def test_the_flow_mapping_version_moved(self):
        import os

        mapping = load_file(os.path.join(
            support.ROOT,
            "mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml",
        ))

        self.assertGreaterEqual(int(mapping.version), 3)

    def test_the_fingerprint_names_both_changed_files(self):
        #
        # One string identifies the whole sampling configuration. If the
        # cadence changes but the fingerprint does not, two differently
        # sampled drives compare as equal.
        #
        profile, _ = car_plan()
        fingerprint = profile.mapping_set(["drive-modes@2"])

        self.assertIn("sae-obd-engine@5", fingerprint)
        self.assertIn("drive-modes@2", fingerprint)
