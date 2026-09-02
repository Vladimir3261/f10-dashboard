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

The synchronized burst that #19 also names was measured and left alone:
in physical exchanges it is 11, not 26, because `ObdSession.read` packs
six PIDs into one Mode 01 request. Spreading it would have cost +16.9%
of `long` mode's wire traffic while barely moving that worst cycle - it
is dominated by a paired `dde_dyn` slot at six exchanges, which phasing
the OBD side cannot touch. `TheWireCostIsAccountedFor` pins that
reasoning so it is not re-litigated from the logical count.

The figures above are the CORRECTED ones. An earlier version of this file
quoted 7 exchanges and +26.8%, from a model that counted each generic
request as one exchange and so missed F303 setup re-arming entirely -
which is the exact accounting mistake these tests exist to prevent, left
sitting in their own docstring.

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
from bmwdiag.mapping.polling import PollingPlan, resolve_classes
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

    def test_rail_actual_and_setpoint_land_in_the_same_scheduler_firing(self):
        #
        # The defect this change exists for: 1.5 s apart in the rotation,
        # 0% coverage inside the 1.0 s window the contract declares.
        #
        # NOTE WHAT THIS DOES AND DOES NOT SAY. It measures SCHEDULER
        # geometry - the two requests are selected in the same firing.
        # It is not a claim about the physical gap on the wire: the
        # executor runs the requests sequentially, and each F303 read may
        # carry setup frames. The real separation is one exchange, which
        # only a car can measure. Recording that separation is what
        # per-signal timestamps exist for; see TheAcquisitionTimeSurvives.
        #
        med, cov, window = self._pair("n47d_rail_act", "n47d_rail_set")

        self.assertEqual(med, 0.0, "same firing in scheduler time")
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


class TheWireCostIsAccountedFor(unittest.TestCase):
    """
    Logical requests are not wire exchanges, and #19's constraint is
    about the wire.

    `MappingExecutor._run_obd` hands every OBD request due in one cycle
    to `ObdSession.read`, which packs SIX PIDs into one Mode 01 exchange
    while `multi_ok` holds. So a cycle with 19 OBD requests is four
    exchanges, not nineteen - and a scheduler change that reduces the
    logical count while splitting batches can INCREASE wire traffic.

    That is not hypothetical: per-request phase spreading was implemented
    here, measured, and removed because it did exactly that. These pin
    the accounting so the next change is judged in the right unit.
    """

    #: same rule as ObdSession.read()
    PIDS_PER_EXCHANGE = 6

    def exchanges(self, plan, profile, seconds=600.0, rate=0.1):
        """
        Physical exchanges, modelled on what the executor actually sends.

        Three details, each of which changes the number materially:

        * `_run_obd` DEDUPES by PID before batching - two mappings wanting
          signals out of one PID cost one slot, not two;
        * six PIDs go into one Mode 01 exchange while `multi_ok` holds;
        * `_run_generic` re-arms an F303 definition whenever the armed
          setup for that destination differs, so a `dde_dyn` member is
          normally TWO setup frames plus the poll - three exchanges, not
          one - and a paired slot is two such sequences.

        An earlier version of this test counted every generic request as
        one exchange. It undercounted the DDE traffic by roughly a third
        of the total and made pairing look cheaper than it is.
        """
        import math

        armed = {}
        total = worst = 0
        now, cycle = 0.0, 0

        while now < seconds:
            due = plan.due(cycle, now)
            obd = [r for r in due
                   if r.protocol == "obd" and r.payload is None]
            other = [r for r in due if r not in obd]

            pids = []

            for request in obd:
                if request.pid is not None and request.pid not in pids:
                    pids.append(request.pid)

            count = (
                math.ceil(len(pids) / self.PIDS_PER_EXCHANGE) if pids else 0
            )

            for request in other:
                dst = request.target.resolve(profile.targets) or 0x12

                if request.setup and armed.get(dst) != request.setup:
                    count += len(request.setup)
                    armed[dst] = request.setup

                count += 1

            total += count

            if cycle:
                worst = max(worst, count)

            now += rate
            cycle += 1

        return total / seconds * 60.0, worst

    def test_the_worst_cycle_is_a_handful_of_exchanges_not_dozens(self):
        #
        # The measurement that decided against phase spreading: the
        # "26-request burst" is seven exchanges. Batching already
        # absorbed it.
        #
        profile, plan = car_plan()
        _, worst = self.exchanges(plan, profile)

        #
        # 11: a paired dde_dyn slot is two setup+poll sequences (6), plus
        # the OBD batch and the rest of the cycle. The "26-request burst"
        # is nowhere near 26 exchanges - batching absorbs the OBD half -
        # but it is not the 7 an earlier version of this test claimed
        # either, because that one ignored F303 re-arming.
        #
        self.assertLessEqual(worst, 12)

    def test_pairing_does_not_inflate_wire_traffic(self):
        #
        # A pair shares one rotation slot, so it costs no extra FIRINGS -
        # only a shorter rotation. Compared against the same plan with
        # pairing suppressed, the increase must stay small.
        #
        profile, plan = car_plan()
        paired, _ = self.exchanges(plan, profile)

        import dataclasses

        unpaired_requests = [
            dataclasses.replace(r, polling_pair="") for r in profile.requests
        ]
        flat = PollingPlan(unpaired_requests, plan.declared)
        unpaired, _ = self.exchanges(flat, profile)

        #
        # Measured +3.2% in `normal` with F303 setup counted. Pairing is
        # not free: a paired slot re-arms twice in one firing. It is
        # bounded, which is the property worth pinning.
        #
        self.assertLess(
            paired, unpaired * 1.08,
            "pairing must not materially increase wire exchanges",
        )

    def test_every_drive_mode_stays_within_its_wire_budget(self):
        #
        # `long` is the mode that exists to reduce link load, and it is
        # the one a scheduling change is most likely to spoil - phase
        # spreading raised it 16.9% before it was removed.
        #
        table = load_modes()
        #
        # Measured with F303 setup counted; master is 1098 / 340 / 518 /
        # 3320. Headroom is deliberately tight so a change that adds a
        # fifth to the wire traffic fails here rather than on the car.
        #
        budgets = {"normal": 1250, "long": 400, "sampling": 620, "debug": 3800}

        for name, budget in budgets.items():
            with self.subTest(mode=name):
                profile, plan = car_plan(mode=table.get(name))
                per_minute, _ = self.exchanges(plan, profile, seconds=600.0)

                self.assertLess(per_minute, budget)


class TheAcquisitionTimeSurvives(unittest.TestCase):
    """
    A paired read must not be graded by a timestamp that erased the thing
    being graded.

    Pair slots put two requests in one poll cycle. The executor runs them
    SEQUENTIALLY, so they are not simultaneous - but the recorder used to
    stamp everything in a cycle with one `time.time()`, which would make
    the alignment matcher report exactly 0 ms no matter how far apart the
    two exchanges really were. That is measuring the recorder.

    Each response now carries its own completion time, and the recorder
    stores it.
    """

    def test_the_executor_stamps_each_response_separately(self):
        import os
        import time as clock

        from bmwdiag.mapping import MappingRegistry, load_text
        from bmwdiag.mapping.execute import MappingExecutor
        from bmwdiag.mapping.registry import AllCapabilities

        mapping = load_text(
            "schema_version: 1\n"
            "mapping: {id: t, version: 1, production: false}\n"
            "ecu: {target: 0x12}\n"
            "polling_classes: {p: {seconds: 0.5, priority: 0, stagger: true}}\n"
            "requests:\n"
            "  a:\n"
            "    protocol: uds\n    service: 0x22\n    did: 0xDA01\n"
            "    polling: {class: p, pair: x}\n"
            "    response: {data_length: 1}\n"
            "    signals: {sa: {label: A, unit: '', decode: {type: uint8}}}\n"
            "  b:\n"
            "    protocol: uds\n    service: 0x22\n    did: 0xDA02\n"
            "    polling: {class: p, pair: x}\n"
            "    response: {data_length: 1}\n"
            "    signals: {sb: {label: B, unit: '', decode: {type: uint8}}}\n",
            "t",
        )
        profile = MappingRegistry([mapping]).resolve(AllCapabilities())

        class Slow:
            def request(self, payload, *, dst, timeout=None):
                clock.sleep(0.02)

                return bytes([0x62, payload[1], payload[2], 7])

        executor = MappingExecutor(profile, transport=Slow())
        readings, stamps = executor.execute_readings_at(profile.requests)

        self.assertEqual(set(stamps), {"sa", "sb"})
        self.assertNotEqual(
            stamps["sa"], stamps["sb"],
            "sequential exchanges must not share one timestamp",
        )
        self.assertGreater(abs(stamps["sb"] - stamps["sa"]), 0.01)

    def test_the_recorder_stores_the_per_signal_time(self):
        import os
        import sqlite3
        import tempfile
        import time as clock

        import live

        path = os.path.join(tempfile.mkdtemp(), "t.db")
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12, clock_synced=True)
        clock.sleep(0.05)

        cycle = 1000.0
        rec.write(cycle, {"a": 1.0, "b": 2.0}, None,
                  {"a": cycle - 0.30, "b": cycle - 0.10})
        rec.close()

        con = sqlite3.connect(path)

        try:
            rows = dict(con.execute(
                "SELECT p.key, s.ts FROM samples s "
                "JOIN params p ON p.id = s.param_id"
            ).fetchall())
        finally:
            con.close()

        self.assertAlmostEqual(rows["a"], cycle - 0.30, places=6)
        self.assertAlmostEqual(rows["b"], cycle - 0.10, places=6)

    def test_an_unstamped_signal_keeps_the_cycle_time(self):
        #: derived channels have no exchange of their own
        import os
        import sqlite3
        import tempfile
        import time as clock

        import live

        path = os.path.join(tempfile.mkdtemp(), "t.db")
        rec = live.Recorder(path, chunk=1, interval=0.05)
        rec.open()
        rec.start_run("VINREDACTED", "gw", "DDE", 0x12, clock_synced=True)
        clock.sleep(0.05)
        rec.write(2000.0, {"derived": 5.0}, None, {})
        rec.close()

        con = sqlite3.connect(path)

        try:
            ts = con.execute("SELECT ts FROM samples").fetchone()[0]
        finally:
            con.close()

        self.assertEqual(ts, 2000.0)


class PairDeclarationsAreValidated(unittest.TestCase):
    """
    A scheduling primitive that silently does nothing is worse than one
    that fails: the mapping looks co-scheduled, the data is not, and the
    alignment coverage sits at zero exactly as it did before.
    """

    def plan_for(self, extra):
        from bmwdiag.mapping import MappingRegistry, load_text
        from bmwdiag.mapping.registry import AllCapabilities

        mapping = load_text(
            "schema_version: 1\n"
            "mapping: {id: t, version: 1, production: false}\n"
            "ecu: {target: 0x12}\n"
            "polling_classes:\n"
            "  st: {seconds: 0.5, priority: 0, stagger: true}\n"
            "  flat: {seconds: 10, priority: 1}\n"
            "requests:\n" + extra,
            "t",
        )
        profile = MappingRegistry([mapping]).resolve(AllCapabilities())

        return PollingPlan(profile.requests,
                           resolve_classes(mapping.polling_classes))

    def _request(self, name, did, cls, pair=None):
        tag = "" if pair is None else ", pair: %s" % pair

        return (
            "  %s:\n    protocol: uds\n    service: 0x22\n"
            "    did: %s\n    polling: {class: %s%s}\n"
            "    response: {data_length: 1}\n"
            "    signals: {s%s: {label: X, unit: '', decode: {type: uint8}}}\n"
            % (name, did, cls, tag, name)
        )

    def test_a_three_member_pair_is_refused(self):
        #
        # Three members in one slot is not a pair: it would make a single
        # firing cost three re-arm sequences, which is a different cost
        # model wearing the same name.
        #
        from bmwdiag.mapping.errors import PollingError

        with self.assertRaises(PollingError) as caught:
            self.plan_for(self._request("a", "0xDA01", "st", "x")
                          + self._request("b", "0xDA02", "st", "x")
                          + self._request("c", "0xDA03", "st", "x"))

        self.assertIn("exactly", str(caught.exception))

    def test_a_lone_pair_tag_is_refused(self):
        from bmwdiag.mapping.errors import PollingError

        with self.assertRaises(PollingError) as caught:
            self.plan_for(self._request("a", "0xDA01", "st", "x")
                          + self._request("b", "0xDA02", "st"))

        self.assertIn("has 1 members", str(caught.exception))

    def test_a_pair_on_an_unstaggered_class_is_refused(self):
        from bmwdiag.mapping.errors import PollingError

        with self.assertRaises(PollingError) as caught:
            self.plan_for(self._request("a", "0xDA01", "flat", "x")
                          + self._request("b", "0xDA02", "flat", "x"))

        self.assertIn("not staggered", str(caught.exception))

    def test_a_pair_spanning_two_classes_is_refused(self):
        from bmwdiag.mapping.errors import PollingError

        with self.assertRaises(PollingError) as caught:
            self.plan_for(self._request("a", "0xDA01", "st", "x")
                          + self._request("b", "0xDA02", "flat", "x"))

        self.assertIn("cannot cross classes", str(caught.exception))

    def test_a_valid_pair_is_accepted(self):
        plan = self.plan_for(self._request("a", "0xDA01", "st", "x")
                             + self._request("b", "0xDA02", "st", "x"))

        self.assertEqual(len(plan._slots["st"]), 1)

    def test_the_shipped_mappings_pass_validation(self):
        #: the production set must not be the thing that trips this
        car_plan()
