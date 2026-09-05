"""
The health models (analysis/health) on SYNTHETIC drives.

Every drive here is generated in this file from a seeded generator: no
recorded data, no fixture from the car, no VIN. The generator imitates
the shape of a real session (a motion tier, a ~12 s DDE
round-robin with the actual/setpoint reads 0.56 s apart, a cold-start
warm-up curve) so the eligibility and alignment contracts see what they
see on the car - but every number is invented, and nothing in these
tests is a statement about the vehicle.

The cases are the issue's list: a stable baseline, a controlled gradual
drift that must be detected with a stated confidence, bad-quality
samples that must be excluded and counted, insufficient coverage that
must produce a reason and not a number, a mapping change (refused for a
value channel, flagged for a context channel), a declared maintenance
event that resets the baseline, and a mixed transient/steady drive whose
steady metric must ignore the transients.
"""

import json
import math
import os
import random
import sqlite3
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tests import support  # noqa: F401

import live
from analysis.alignment import PAIRINGS, pairing_for
from analysis.health import (
    BaselineDefinition, DEFAULT_DEFINITION, RowSource, SqliteSource,
    build_report, render_text,
)
from analysis.health.__main__ import main as health_main
from analysis.health.contract import GRADES
from bmwdiag.vehicle import VehicleEvent

# ------------------------------------------------------------ generator

#: An arbitrary epoch in 2025. The value does not matter; the spacing does.
BASE_T = 1_760_000_000.0

#: Mapping versions stamped on the synthetic rows. The flow file carries
#: boost/rail/coolant/charge-air, the dynamic file carries oil, the OBD
#: file carries the motion tier - mirroring which file owns what on the
#: car, so a "version change" test changes the right group.
DEFAULT_VERSIONS = {
    "rpm": "5", "pedal": "5", "speed": "5", "load": "5", "ambient": "5",
    "n47d_boost_act": "3", "n47d_boost_set": "3",
    "n47d_rail_act": "3", "n47d_rail_set": "3",
    "n47d_coolant": "3", "n47d_charge_air_temp": "3",
    "n47d_oil_temp": "2",
}

IDLE_S = 60.0          # idle at the start of every drive
BLOCK_S = 240.0        # steady A, ramp up, steady B, ramp down
RAMP_S = 10.0          # steep, so the steady gate and the ramp agree


def operating_point(t: float) -> Tuple[float, float, float, bool]:
    """(rpm, pedal %, speed km/h, in_ramp) for a second into the drive."""
    if t < IDLE_S:
        return 800.0, 0.0, 0.0, False

    u = (t - IDLE_S) % BLOCK_S

    if u < 90:
        return 1800.0, 20.0, 50.0, False

    if u < 90 + RAMP_S:
        f = (u - 90) / RAMP_S
        return 1800 + 400 * f, 20 + 10 * f, 50 + 30 * f, True

    if u < 210:
        return 2200.0, 30.0, 80.0, False

    f = (u - 210) / RAMP_S
    return 2200 - 400 * f, 30 - 10 * f, 80 - 30 * f, True


def boost_setpoint(rpm: float, pedal: float) -> float:
    return 1000.0 + 0.25 * rpm + 12.0 * pedal          # hPa, invented


def rail_setpoint(rpm: float, pedal: float) -> float:
    return 400.0 + 0.3 * rpm + 5.0 * pedal             # bar, invented


class Synth:
    """A seeded generator of drives shaped like the recorder's rows."""

    def __init__(self, seed: int = 14):
        self.rng = random.Random(seed)

    def trip(self, index: int, *,
             boost_offset: float = -30.0, boost_noise: float = 10.0,
             transient_error: float = -400.0,
             rail_offset: float = -20.0, rail_gap_s: float = 0.56,
             boost_gap_s: float = 0.56,
             tau_s: float = 360.0, oil_lag_s: float = 90.0,
             ambient_c: float = 12.0, cold: bool = True,
             duration_s: float = 1800.0,
             versions: Optional[Dict[str, str]] = None,
             mode: str = "normal", clock_synced: Optional[int] = 1,
             bad_boost_fraction: float = 0.0,
             drop_setpoint_fraction: float = 0.0,
             ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        rng = self.rng
        ver = dict(DEFAULT_VERSIONS, **(versions or {}))
        t0 = BASE_T + index * 86400.0
        session = {
            "session_id": index + 1,
            "session_uid": f"synthetic-{index:02d}",
            "started": t0, "ended": t0 + duration_s,
            "boot_id": f"boot-{index}", "mode": mode,
            "vehicle_hardware": "dpf=removed", "vehicle_label": "SYNTH-F10",
            "clock_synced": clock_synced, "mappings": "synthetic@1",
        }
        rows: List[Dict[str, Any]] = []

        def emit(t: float, channel: str, value: float, quality: str = "ok") -> None:
            rows.append({
                "session_id": index + 1, "ts": t0 + t, "channel_raw": channel,
                "value": value, "quality": quality, "mapping_ver": ver[channel],
            })

        # Motion tier at 1 Hz - the car's is 10 Hz, but the steady gate
        # needs only >= 4 samples in +/- 2 s, and the tests stay quick.
        for i in range(int(duration_s)):
            t = float(i)
            rpm, pedal, speed, _ = operating_point(t)
            emit(t, "rpm", rpm + rng.uniform(-5, 5))
            emit(t, "pedal", max(0.0, pedal + rng.uniform(-0.3, 0.3)))
            emit(t, "speed", max(0.0, speed + rng.uniform(-0.5, 0.5)))
            emit(t, "load", 20.0 + 0.6 * pedal + rng.gauss(0, 1.0))

        # Thermal channels on the ~12 s round-robin.
        start_c = ambient_c + 2.0 if cold else 85.0
        asymptote = 93.0

        def coolant_at(t: float) -> float:
            return min(91.0, asymptote - (asymptote - start_c) * math.exp(-t / tau_s))

        def oil_at(t: float) -> float:
            if t < oil_lag_s:
                return start_c
            return min(95.0, asymptote - (asymptote - start_c)
                       * math.exp(-(t - oil_lag_s) / tau_s))

        k = 0

        while 12 * k + 10 < duration_s:
            base = 12.0 * k
            emit(base + 2, "n47d_coolant", round(coolant_at(base + 2) + rng.uniform(-0.25, 0.25), 2))
            emit(base + 4, "n47d_oil_temp", round(oil_at(base + 4) + rng.uniform(-0.25, 0.25), 2))
            emit(base + 9, "n47d_charge_air_temp",
                 25.0 + 0.005 * operating_point(base + 9)[0] + rng.gauss(0, 0.3))

            # Boost: actual first, setpoint boost_gap_s later, as on the car.
            ta = base + 6.3          # off the block grid on purpose
            rpm, pedal, _, _ = operating_point(ta)
            transient = operating_point(ta)[3]      # strictly inside a ramp
            setpoint = boost_setpoint(rpm, pedal)
            actual = (setpoint + boost_offset + rng.gauss(0, boost_noise)
                      + (transient_error if transient else 0.0))

            if rng.random() < bad_boost_fraction:
                emit(ta, "n47d_boost_act", 65535.0, "sentinel")
            else:
                emit(ta, "n47d_boost_act", actual)

            if rng.random() >= drop_setpoint_fraction:
                rpm2, pedal2, _, _ = operating_point(ta + boost_gap_s)
                emit(ta + boost_gap_s, "n47d_boost_set", boost_setpoint(rpm2, pedal2))

            # Rail: same shape, its own gap.
            tr = base + 7.3
            rpm, pedal, _, _ = operating_point(tr)
            setpoint = rail_setpoint(rpm, pedal)
            emit(tr, "n47d_rail_act", setpoint + rail_offset + rng.gauss(0, 5.0)
                 + (-150.0 if operating_point(tr)[3] else 0.0))
            rpm2, pedal2, _, _ = operating_point(tr + rail_gap_s)
            emit(tr + rail_gap_s, "n47d_rail_set", rail_setpoint(rpm2, pedal2))
            k += 1

        for m in range(int(duration_s // 60)):
            emit(60.0 * m + 30, "ambient", ambient_c + rng.uniform(-0.3, 0.3))

        return session, rows


def build(trips: Sequence[Tuple[Dict[str, Any], List[Dict[str, Any]]]]) -> RowSource:
    sessions = [s for s, _ in trips]
    rows = [r for _, rs in trips for r in rs]

    return RowSource(sessions, rows)


def stable_drives(n: int = 8, **overrides: Any) -> RowSource:
    synth = Synth()

    return build([synth.trip(i, **overrides) for i in range(n)])


def metrics_of(report, model: str, name: Optional[str] = None) -> list:
    for result in report.models:
        if result.model == model:
            return [m for m in result.metrics if name is None or m.metric == name]

    raise AssertionError(f"no model {model}")


def model_of(report, model: str):
    return next(r for r in report.models if r.model == model)


def steady_cell(report, rpm: str = "1500-2000", model: str = "boost"):
    for metric in metrics_of(report, model, "residual_steady"):
        if metric.condition["rpm"] == rpm and "15-40" in metric.condition["demand"]:
            return metric

    raise AssertionError(f"no steady cell rpm={rpm}: "
                         + str([m.condition for m in metrics_of(report, model)]))


# ------------------------------------------------------------------ tests


class StableBaseline(unittest.TestCase):
    """Eight alike drives: numbers come out, no drift is declared."""

    @classmethod
    def setUpClass(cls):
        cls.report = build_report(stable_drives())

    def test_boost_residual_is_computed_per_steady_cell(self):
        cell = steady_cell(self.report)
        self.assertTrue(cell.available, cell.unavailable_reason)
        self.assertAlmostEqual(cell.value, -30.0, delta=6.0)
        self.assertEqual(cell.unit, "hPa")
        self.assertEqual(cell.condition["state"], "steady")
        self.assertFalse(cell.drift["detected"])
        self.assertLess(abs(cell.drift["delta"]), cell.drift["material_threshold"])
        self.assertIn(cell.confidence.grade, ("moderate", "high"))
        self.assertEqual(cell.baseline["trips"], DEFAULT_DEFINITION.reference_trips)
        self.assertEqual(cell.current["trips"], DEFAULT_DEFINITION.current_trips)
        self.assertGreaterEqual(cell.coverage["alignment_pct"], 95.0)

    def test_the_contract_travels_with_the_number(self):
        cell = steady_cell(self.report).as_dict()

        for key in ("value", "baseline", "current", "drift", "sample_count",
                    "coverage", "quality_filters", "alignment", "confidence",
                    "compatibility", "baseline_definition", "unavailable_reason"):
            self.assertIn(key, cell)

        self.assertEqual(cell["baseline_definition"]["id"], "f10-health-baseline")
        self.assertEqual(cell["baseline_definition"]["version"], 1)
        self.assertEqual(cell["alignment"]["max_age_s"], pairing_for(
            "n47d_boost_act", "n47d_boost_set").max_age_s)
        self.assertIn("sessions.clock_synced = 1 (every run of the trip)",
                      cell["quality_filters"])
        self.assertIn("window", cell["condition"])
        self.assertEqual(cell["condition"]["window"]["steady_rpm_range"],
                         DEFAULT_DEFINITION.steady_rpm_range)
        self.assertIn(cell["confidence"]["grade"], GRADES)
        self.assertTrue(cell["confidence"]["rules"])
        self.assertIsNone(cell["unavailable_reason"])
        self.assertEqual(cell["baseline"]["context"]["charge_air_c"],
                         cell["baseline"]["context"]["charge_air_c"])
        self.assertIn("charge_air_c", cell["baseline"]["context"])

    def test_warmup_figures_are_computed_and_stable(self):
        t60 = metrics_of(self.report, "warmup", "time_to_60c")
        self.assertEqual(len(t60), 1, "one ambient band")
        metric = t60[0]
        self.assertTrue(metric.available, metric.unavailable_reason)
        # tau=360, from 12 to an asymptote of 93: 360*ln(81/33) = 323 s.
        self.assertAlmostEqual(metric.value, 323.0, delta=15.0)
        self.assertFalse(metric.drift["detected"])
        self.assertEqual(metric.condition["ambient_c"], "10-15")
        self.assertEqual(len(metric.observations), 8)

        for obs in metric.observations:
            self.assertLess(obs["moving_fraction"], 1.0)   # the idle minute
            self.assertIsNotNone(obs["oil_lag_to_60c_s"])
            self.assertIsNotNone(obs["stabilised_coolant_c"])

        slope = metrics_of(self.report, "warmup", "warmup_slope")[0]
        self.assertTrue(slope.available)
        self.assertGreater(slope.value, 0)
        stabilised = metrics_of(self.report, "warmup", "stabilised_coolant")[0]
        self.assertAlmostEqual(stabilised.value, 91.0, delta=1.0)

    def test_rail_is_computed_when_the_reads_are_close_enough(self):
        cell = steady_cell(self.report, model="rail")
        self.assertTrue(cell.available, cell.unavailable_reason)
        self.assertAlmostEqual(cell.value, -20.0, delta=4.0)
        self.assertEqual(cell.unit, "bar")

    def test_report_is_deterministic_and_json(self):
        first = build_report(stable_drives()).as_dict()
        second = build_report(stable_drives()).as_dict()
        self.assertEqual(first, second)
        self.assertEqual(json.loads(json.dumps(first)), first)
        self.assertEqual(first["contract"], "analysis.health/1")
        self.assertEqual(first["vehicle_label"], "SYNTH-F10")

    def test_text_report_says_something_per_model(self):
        text = render_text(self.report)

        for model in ("## warmup", "## boost", "## rail", "## egr"):
            self.assertIn(model, text)

        self.assertIn("no detected change", text)
        self.assertIn("unavailable - requested/actual not yet mapped", text)


class ShortColdTrip(unittest.TestCase):
    """
    A cold trip that ends before 80 °C has no warm-up slope, and says
    so. The slope of a truncated ramp is steeper than the slope of the
    whole ramp (the exponential flattens), so pooling it unflagged with
    complete ramps would make "drift" a function of trip length - and
    short winter trips are routine on this car.
    """

    @classmethod
    def setUpClass(cls):
        synth = Synth(seed=3)
        trips = [synth.trip(i) for i in range(8)]
        #: two 150 s cold trips inside the current window
        trips[5] = synth.trip(5, duration_s=150.0)
        trips[7] = synth.trip(7, duration_s=150.0)
        cls.report = build_report(build(trips))

    def observation(self, metric, uid):
        return next(o for o in metric.observations if o["trip_uid"] == uid)

    def test_a_truncated_ramp_has_no_slope(self):
        slope = metrics_of(self.report, "warmup", "warmup_slope")[0]
        short = self.observation(slope, "synthetic-05")

        self.assertFalse(short["ramp_complete"])
        self.assertIsNone(short["time_to_80c_s"])
        self.assertIsNone(short["warmup_slope_c_per_min"],
                          "a slope with no 80 °C crossing is a slope of "
                          "the trip length, not of the engine")
        full = self.observation(slope, "synthetic-04")
        self.assertTrue(full["ramp_complete"])
        self.assertIsNotNone(full["warmup_slope_c_per_min"])

    def test_the_short_trips_do_not_reach_the_pooled_figure(self):
        slope = metrics_of(self.report, "warmup", "warmup_slope")[0]
        #: 8 cold starts in the band, 6 reached 80 °C: not the 5 + 3
        #: the definition needs, so the metric is unavailable rather
        #: than computed on a mix of complete and truncated ramps
        self.assertEqual(slope.coverage["cold_starts_in_band"], 8)
        self.assertEqual(slope.coverage["cold_starts_reached"], 6)
        self.assertAlmostEqual(slope.coverage["reached_fraction"], 0.75)
        self.assertFalse(slope.available)
        self.assertIn("insufficient trips", slope.unavailable_reason)
        self.assertTrue(any("under-detected" in n for n in slope.notes), slope.notes)

    def test_survivorship_is_reported_per_target(self):
        t60 = metrics_of(self.report, "warmup", "time_to_60c")[0]
        t90 = metrics_of(self.report, "warmup", "time_to_90c")[0]
        #: from 14 °C at tau 360 the 60 °C crossing is at 360*ln(79/33)
        #: = 314 s, so a 150 s trip reaches none of the targets
        self.assertEqual(t60.coverage["cold_starts_reached"], 6)
        self.assertEqual(t90.coverage["cold_starts_reached"], 6)
        self.assertEqual(t60.coverage["reached_fraction"], 0.75)
        #: a complete band carries the fraction too, at 1, with no note
        complete = metrics_of(build_report(stable_drives()), "warmup", "time_to_60c")[0]
        self.assertEqual(complete.coverage["reached_fraction"], 1.0)
        self.assertFalse(any("under-detected" in n for n in complete.notes))


class GradualDrift(unittest.TestCase):
    """A controlled drift across the eight drives is detected, and graded."""

    @classmethod
    def setUpClass(cls):
        synth = Synth(seed=7)
        cls.report = build_report(build([
            synth.trip(i, boost_offset=-30.0 - 8.0 * i, tau_s=360.0 + 20.0 * i)
            for i in range(8)
        ]))

    def test_boost_drift_is_detected_with_evidence(self):
        cell = steady_cell(self.report)
        self.assertTrue(cell.available, cell.unavailable_reason)
        drift = cell.drift
        self.assertTrue(drift["detected"], drift)
        self.assertEqual(drift["direction"], "down")
        self.assertLessEqual(drift["z"], -DEFAULT_DEFINITION.drift_z)
        self.assertTrue(drift["material"])
        self.assertGreaterEqual(abs(drift["delta"]), drift["material_threshold"])
        # p_exceed is the common-language effect size, P(current > baseline).
        self.assertLess(drift["p_exceed"], 0.25)
        self.assertIn(cell.confidence.grade, ("moderate", "high"))
        self.assertIn("confidence >= moderate", drift["rule"])
        # Baseline offsets -30..-62 (median -46), current -70..-86 (median -78).
        self.assertAlmostEqual(cell.baseline["median"], -46.0, delta=8.0)
        self.assertAlmostEqual(cell.current["median"], -78.0, delta=8.0)

    def test_warmup_drift_is_detected_per_trip(self):
        metric = metrics_of(self.report, "warmup", "time_to_60c")[0]
        self.assertTrue(metric.available, metric.unavailable_reason)
        self.assertTrue(metric.drift["detected"], metric.drift)
        self.assertEqual(metric.drift["direction"], "up")
        # Per-trip: 5 vs 3 observations, fully separated, gives |z| = 2.24.
        self.assertGreaterEqual(metric.drift["z"], DEFAULT_DEFINITION.drift_z)
        self.assertEqual(metric.confidence.grade, "moderate")
        self.assertIn("fewer than 5 trips on a side", metric.confidence.reasons)

    def test_the_sentence_names_the_drift(self):
        text = steady_cell(self.report).sentence()
        self.assertIn("DRIFT", text)
        self.assertIn("z=", text)
        self.assertIn("Confidence:", text)


class BadQualitySamples(unittest.TestCase):
    """Sentinel boost samples are excluded, counted, and cannot move the number."""

    def test_excluded_and_reflected_in_coverage(self):
        clean = build_report(stable_drives())
        dirty = build_report(stable_drives(bad_boost_fraction=0.3))
        excluded = dirty.eligibility["samples_excluded_by_quality"]
        self.assertGreater(excluded.get("sentinel", 0), 0)
        self.assertEqual(clean.eligibility["samples_excluded_by_quality"], {})

        good, bad = steady_cell(clean), steady_cell(dirty)
        self.assertTrue(bad.available, bad.unavailable_reason)
        # 65535 hPa sentinels in the population would put the median far
        # from -30; excluded, it stays there.
        self.assertAlmostEqual(bad.value, -30.0, delta=6.0)
        self.assertLess(bad.coverage["pairs_attempted"], good.coverage["pairs_attempted"] * 0.85)
        self.assertLess(bad.sample_count, good.sample_count)
        self.assertIn("samples.quality = 'ok' (NULL = pre-labelling, used and counted)",
                      bad.quality_filters)


class InsufficientCoverage(unittest.TestCase):
    """Not enough data is a reason, never a number."""

    def test_too_few_trips_is_unavailable_with_reason(self):
        report = build_report(stable_drives(n=3))

        for model in ("warmup", "boost", "rail"):
            metrics = metrics_of(report, model)
            self.assertTrue(metrics, model)

            for metric in metrics:
                self.assertIsNone(metric.value)
                self.assertFalse(metric.available)
                self.assertTrue(metric.unavailable_reason.startswith("insufficient trips"),
                                metric.unavailable_reason)
                self.assertEqual(metric.confidence.grade, "none")
                self.assertIsNone(metric.drift)

        text = render_text(report)
        self.assertIn("unavailable", text)
        self.assertNotIn("DRIFT", text)

    def test_low_alignment_coverage_is_refused_not_averaged(self):
        report = build_report(stable_drives(drop_setpoint_fraction=0.7))
        cell = steady_cell(report)
        self.assertFalse(cell.available)
        self.assertIn("alignment coverage", cell.unavailable_reason)
        self.assertLess(cell.coverage["alignment_pct"], 50.0)
        self.assertFalse(cell.confidence.rules["coverage_usable"])

    def test_a_pair_the_schedule_never_aligns_is_a_schedule_fact(self):
        #
        # The lake's real flow-mapping-v2 sessions place the rail reads
        # ~1.6 s apart, outside the 1.0 s contract. Reproduced here with
        # an invented drive: the model must say so, not loosen anything.
        #
        report = build_report(stable_drives(rail_gap_s=1.6))
        rail = model_of(report, "rail")
        self.assertEqual(rail.status, "unavailable")
        self.assertTrue(any("never aligned" in n for n in rail.notes), rail.notes)
        self.assertEqual(pairing_for("n47d_rail_act", "n47d_rail_set").max_age_s, 1.0)
        # Boost, on its own 0.56 s gap, is unaffected.
        self.assertTrue(steady_cell(report).available)

    def test_no_cold_start_means_no_warmup(self):
        report = build_report(stable_drives(cold=False))
        warm = model_of(report, "warmup")
        self.assertEqual(warm.status, "unavailable")
        self.assertIn("no cold start", warm.unavailable_reason)


class MappingChange(unittest.TestCase):
    """A version change on a value channel refuses to pool; on a context channel it flags."""

    def _drives(self, channel: str, first: str, second: str) -> RowSource:
        synth = Synth(seed=3)

        return build([
            synth.trip(i, versions={channel: first if i < 4 else second})
            for i in range(8)
        ])

    def test_value_channel_change_is_refused(self):
        source = self._drives("n47d_boost_act", "2", "3")
        report = build_report(source)
        cell = steady_cell(report)
        self.assertFalse(cell.available)
        self.assertTrue(cell.unavailable_reason.startswith("insufficient trips: 4"),
                        cell.unavailable_reason)
        compat = cell.compatibility
        self.assertEqual(compat["population"]["trips"], 4)
        self.assertIn("mapping version of n47d_boost_act changed 2 -> 3",
                      compat["population"]["reason"])
        self.assertEqual(len(compat["earlier_populations_excluded"]), 1)
        self.assertEqual(compat["earlier_populations_excluded"][0]["trips"], 4)
        self.assertIn("refused", compat["policy"]["value_channels"])

        # Inside the newest population the comparison works, and it never
        # reaches back across the change.
        narrow = build_report(source, DEFAULT_DEFINITION.replace(
            reference_trips=2, current_trips=2))
        cell = steady_cell(narrow)
        self.assertTrue(cell.available, cell.unavailable_reason)
        self.assertGreaterEqual(cell.baseline["from"], BASE_T + 4 * 86400.0)
        self.assertEqual(cell.compatibility["population"]["versions"]["n47d_boost_act"], "3")

    def test_context_channel_change_is_flagged_and_caps_confidence(self):
        report = build_report(self._drives("rpm", "4", "5"))
        cell = steady_cell(report)
        self.assertTrue(cell.available, cell.unavailable_reason)
        self.assertEqual(cell.confidence.grade, "moderate")
        self.assertFalse(cell.confidence.rules["no_context_version_flags"])
        self.assertTrue(any("rpm decoded with mapping versions 4, 5" in n
                            for n in cell.notes), cell.notes)
        self.assertEqual(cell.compatibility["context_versions"]["rpm"], ["4", "5"])
        self.assertEqual(cell.compatibility["population"]["trips"], 8)

    def test_a_version_change_inside_one_trip_drops_that_trip(self):
        synth = Synth(seed=5)
        drives = [synth.trip(i) for i in range(8)]
        # Two runs of the same trip decoded boost with different versions:
        # stamp the second half of trip 3's boost rows with another version.
        session, rows = drives[3]
        mid = session["started"] + 900

        for row in rows:
            if row["channel_raw"] == "n47d_boost_act" and row["ts"] > mid:
                row["mapping_ver"] = "4"

        report = build_report(build(drives))
        cell = steady_cell(report)
        dropped = cell.compatibility["trips_dropped"]
        self.assertEqual([d["trip_uid"] for d in dropped], ["synthetic-03"])
        self.assertIn("more than one mapping version inside the trip", dropped[0]["reason"])
        self.assertEqual(cell.compatibility["population"]["trips"], 7)


class MaintenanceEvent(unittest.TestCase):
    """A declared event starts a new baseline; nothing before it is compared."""

    def test_event_resets_the_baseline(self):
        source = stable_drives()
        event = VehicleEvent("sensor_replacement", BASE_T + 7200.0,
                             "boost pressure sensor replaced (synthetic)")
        report = build_report(source, events=[event])
        self.assertEqual(report.eligibility["vehicle_events_declared"], [event.describe()])
        cell = steady_cell(report)
        self.assertFalse(cell.available)
        self.assertTrue(cell.unavailable_reason.startswith("insufficient trips: 7"))
        self.assertEqual(cell.compatibility["population"]["trips"], 7)
        self.assertEqual(cell.compatibility["earlier_populations_excluded"][0]["trips"], 1)
        self.assertTrue(cell.compatibility["population"]["reason"].startswith("vehicle event"))
        self.assertIn("boost pressure sensor replaced", cell.compatibility["population"]["reason"])

        narrow = build_report(source, DEFAULT_DEFINITION.replace(
            reference_trips=3, current_trips=3), events=[event])
        cell = steady_cell(narrow)
        self.assertTrue(cell.available, cell.unavailable_reason)
        self.assertGreater(cell.baseline["from"], event.at)

        # Warm-up segments on the same event: the pre-event trip is gone.
        t60 = metrics_of(narrow, "warmup", "time_to_60c")[0]
        self.assertTrue(t60.available, t60.unavailable_reason)
        self.assertEqual(len(t60.observations), 7)
        self.assertGreater(min(o["started"] for o in t60.observations), event.at)

    def test_without_the_event_the_same_drives_pool(self):
        cell = steady_cell(build_report(stable_drives()))
        self.assertTrue(cell.available)
        self.assertEqual(cell.compatibility["population"]["reason"], "first eligible trip")
        self.assertEqual(cell.compatibility["earlier_populations_excluded"], [])


class MixedTransientAndSteady(unittest.TestCase):
    """The steady metric does not see the transients; the transient one is labelled."""

    def test_steady_metric_ignores_transients(self):
        report = build_report(stable_drives(transient_error=-400.0))
        steady = steady_cell(report)
        self.assertAlmostEqual(steady.value, -30.0, delta=6.0)
        transient = metrics_of(report, "boost", "residual_transient")
        self.assertEqual(len(transient), 1)
        transient = transient[0]
        self.assertTrue(transient.available, transient.unavailable_reason)
        self.assertLess(transient.value, -300.0)
        self.assertEqual(transient.condition["state"], "transient")
        self.assertTrue(any("descriptive only" in n for n in transient.notes))
        self.assertGreater(steady.coverage["steady_samples_total"], 0)
        self.assertGreater(steady.coverage["transient_samples_total"], 0)
        # Cells are keyed by the operating point, and only the steady ones.
        rpm_bins = {m.condition["rpm"] for m in metrics_of(report, "boost", "residual_steady")}
        self.assertEqual(rpm_bins, {"600-1000", "1500-2000", "2000-2500"})

    def test_a_transient_only_error_does_not_register_as_steady_drift(self):
        synth = Synth(seed=11)
        report = build_report(build([
            synth.trip(i, transient_error=-200.0 - 100.0 * i) for i in range(8)
        ]))
        self.assertFalse(steady_cell(report).drift["detected"])
        transient = metrics_of(report, "boost", "residual_transient")[0]
        self.assertEqual(transient.drift["direction"], "down")


class EgrAndEligibility(unittest.TestCase):

    def test_egr_is_declared_unavailable_not_faked(self):
        egr = model_of(build_report(stable_drives(n=2)), "egr")
        self.assertEqual(egr.status, "unavailable")
        self.assertTrue(egr.unavailable_reason.startswith("requested/actual not yet mapped"))
        self.assertEqual(egr.metrics, [])

    def test_unsynced_clock_trip_is_excluded_and_listed(self):
        synth = Synth(seed=2)
        drives = [synth.trip(i, clock_synced=0 if i == 2 else 1) for i in range(8)]
        report = build_report(build(drives))
        excluded = report.eligibility["excluded_trips"]
        self.assertEqual([e["trip_uid"] for e in excluded], ["synthetic-02"])
        self.assertIn("clock_synced != 1", excluded[0]["reason"])
        self.assertEqual(report.eligibility["trips_eligible"], 7)
        self.assertEqual(report.eligibility["sessions_clock_synced"], 7)
        self.assertIn("synthetic-02", render_text(report))

    def test_different_drive_modes_lower_confidence(self):
        synth = Synth(seed=9)
        drives = [synth.trip(i, mode="normal" if i < 5 else "long") for i in range(8)]
        cell = steady_cell(build_report(build(drives)))
        self.assertTrue(cell.available, cell.unavailable_reason)
        self.assertEqual(cell.confidence.grade, "low")
        self.assertFalse(cell.confidence.rules["same_drive_modes"])
        self.assertFalse(cell.drift["detected"])

    def test_charge_air_context_pairings_are_declared(self):
        for actual in ("n47d_boost_act", "n47d_rail_act"):
            pairing = PAIRINGS[(actual, "n47d_charge_air_temp")]
            self.assertEqual(pairing.max_age_s, 15.0)
            self.assertIn("never subtracted", PAIRINGS[("n47d_boost_act", "n47d_charge_air_temp")].why)

        # And the control-loop tolerance is untouched by the additions.
        self.assertEqual(PAIRINGS[("n47d_boost_act", "n47d_boost_set")].max_age_s, 1.0)


# ------------------------------------------------------- sqlite + cli


def write_sqlite(path: str, trips) -> None:
    """Write synthetic drives through the recorder's own SCHEMA."""
    db = sqlite3.connect(path)
    db.executescript(live.SCHEMA)
    params: Dict[str, int] = {}

    for session, rows in trips:
        db.execute(
            "INSERT INTO runs (id, started_at, ended_at, mapping_set, mode, "
            "clock_synced, vehicle_label, vehicle_hardware, session_uid, boot_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (session["session_id"], session["started"], session["ended"],
             session["mappings"], session["mode"], session["clock_synced"],
             session["vehicle_label"], session["vehicle_hardware"],
             session["session_uid"], session["boot_id"]),
        )
        seen: Dict[str, str] = {}

        for row in rows:
            key = row["channel_raw"]

            if key not in params:
                cur = db.execute(
                    "INSERT INTO params (key, label, unit, mapping_ver) VALUES (?,?,?,?)",
                    (key, key, "", "stale"),   # the fallback must NOT be used
                )
                params[key] = cur.lastrowid

            if key not in seen:
                seen[key] = row["mapping_ver"]
                db.execute(
                    "INSERT INTO run_channels (run_id, param_id, mapping_id, "
                    "mapping_version, label, unit) VALUES (?,?,?,?,?,?)",
                    (session["session_id"], params[key], "synthetic",
                     row["mapping_ver"], key, ""),
                )

        db.executemany(
            "INSERT INTO samples (run_id, ts, param_id, value, quality) "
            "VALUES (?,?,?,?,?)",
            [(session["session_id"], row["ts"], params[row["channel_raw"]],
              row["value"], row["quality"]) for row in rows],
        )

    db.commit()
    db.close()


class SqliteAndCli(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "telemetry.db")
        synth = Synth(seed=21)
        self.trips = [synth.trip(i, versions={"n47d_boost_act": "2" if i < 4 else "3"})
                      for i in range(8)]
        write_sqlite(self.path, self.trips)

    def test_sqlite_source_and_row_source_agree(self):
        from_db = build_report(SqliteSource(self.path)).as_dict()
        from_rows = build_report(build(self.trips)).as_dict()
        self.assertEqual(from_db["eligibility"], from_rows["eligibility"])
        self.assertEqual(from_db["models"], from_rows["models"])
        self.assertTrue(from_db["source"].startswith("sqlite:"))
        # run_channels provenance was read, not the stale params fallback.
        boost = next(m for m in from_db["models"] if m["model"] == "boost")
        self.assertEqual(boost["metrics"][0]["compatibility"]["population"]["versions"],
                         {"n47d_boost_act": "3", "n47d_boost_set": "3"})

    def test_source_opens_read_only(self):
        source = SqliteSource(self.path)
        db = source._connect()

        with self.assertRaises(sqlite3.OperationalError):
            db.execute("DELETE FROM samples")

        db.close()

    def test_cli_prints_the_report_and_writes_the_contract(self):
        out = os.path.join(self.dir, "health.json")
        import io
        import contextlib
        buffer = io.StringIO()

        with contextlib.redirect_stdout(buffer):
            code = health_main(["--db", self.path, "--json", out, "--no-events",
                                "--reference-trips", "2", "--current-trips", "2"])

        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("# Health models - SYNTH-F10", text)
        self.assertIn("## boost", text)

        with open(out) as fh:
            data = json.load(fh)

        self.assertEqual(data["contract"], "analysis.health/1")
        self.assertEqual(data["baseline_definition"]["reference_trips"], 2)
        self.assertEqual({m["model"] for m in data["models"]},
                         {"warmup", "boost", "rail", "egr"})
        # Nothing VIN-shaped: the row shape has no field for one.
        import re
        self.assertIsNone(re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", json.dumps(data)))

    def test_cli_can_select_models(self):
        import io
        import contextlib
        buffer = io.StringIO()

        with contextlib.redirect_stdout(buffer):
            health_main(["--db", self.path, "--no-events", "--models", "egr"])

        self.assertIn("## egr", buffer.getvalue())
        self.assertNotIn("## boost", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
