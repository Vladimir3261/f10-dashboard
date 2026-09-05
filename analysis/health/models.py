"""
The three health models, and the one that declares itself unavailable.

Each model turns eligible trips into `Observation`s per operating-
condition cell and hands them to `baseline.compare`. The model decides
WHAT is measured and under WHICH conditions; the baseline machinery
decides whether there is enough of it to say anything. Neither knows
about the other vehicle, because there is no other vehicle.

Value channels (their number enters the metric) and context channels
(they only select or label samples) are declared per model, because the
mapping-version policy treats them differently - see baseline.py.

All conditioning constants come from the `BaselineDefinition` and are
echoed back in every metric's `condition` and `baseline_definition`.
"""

import bisect
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bmwdiag.vehicle import VehicleEvent

from analysis.alignment import align, pairing_for
from analysis.health.baseline import Observation, Segment, compare, segment
from analysis.health.contract import (
    BaselineDefinition, HealthMetric, ModelResult,
)
from analysis.health.eligibility import QUALITY_FILTER_TEXT, TripData
from analysis.health.stats import (
    first_crossing, least_squares_slope, median,
)

__all__ = [
    "TRACKING",
    "warmup_model",
    "tracking_model",
    "egr_model",
]

Series = List[Tuple[float, float]]

#: The two control loops with a mapped actual/setpoint pair on this car.
TRACKING = {
    "boost": {
        "actual": "n47d_boost_act", "setpoint": "n47d_boost_set", "unit": "hPa",
        "label": "boost tracking (actual - setpoint)",
        #: What the read gap alone injects under transients - the figure
        #: behind the pairing tolerance in `analysis/alignment.py`.
        "transient_error": "up to ~140 hPa p90 on boost, lake measurement "
                           "over 0.56 s (analysis/alignment.py)",
    },
    "rail": {
        "actual": "n47d_rail_act", "setpoint": "n47d_rail_set", "unit": "bar",
        "label": "rail pressure tracking (actual - setpoint)",
        "transient_error": "not measured for rail: the two reads never "
                           "aligned on the lake's clock-synced sessions "
                           "(flow mapping v2 schedule), so the size of the "
                           "gap's own error is unknown",
    },
}

#: Context channel candidates, first present wins, per trip.
RPM_CHANNELS = ("rpm",)
DEMAND_CHANNELS = ("pedal", "n47d_pedal", "load")
CHARGE_AIR_CHANNELS = ("n47d_charge_air_temp",)
COOLANT_CHANNELS = ("n47d_coolant", "coolant")
OIL_CHANNELS = ("n47d_oil_temp", "oil")
AMBIENT_CHANNELS = ("ambient",)
SPEED_CHANNELS = ("speed",)
LOAD_CHANNELS = ("load",)


# --------------------------------------------------------------- helpers


class _Indexed:
    """A sorted series split into parallel time/value lists for bisect."""

    __slots__ = ("times", "values")

    def __init__(self, series: Series):
        self.times = [t for t, _ in series]
        self.values = [v for _, v in series]

    def window(self, ts: float, half: float) -> List[float]:
        """Values within +/- half of ts."""
        lo = bisect.bisect_left(self.times, ts - half)
        hi = bisect.bisect_right(self.times, ts + half)

        return self.values[lo:hi]

    def nearest(self, ts: float, max_age_s: float) -> Optional[float]:
        """The value nearest ts within the window, else None. Ties to earlier."""
        if not self.times:
            return None

        i = bisect.bisect_right(self.times, ts)
        best, best_gap = None, None

        for j in (i - 1, i):
            if 0 <= j < len(self.times):
                gap = abs(self.times[j] - ts)

                if best_gap is None or gap < best_gap:
                    best, best_gap = self.values[j], gap

        return best if best_gap is not None and best_gap <= max_age_s else None


def _bin(value: float, bins: Sequence[Sequence[float]]) -> Optional[str]:
    for lo, hi in bins:
        if lo <= value < hi:
            return f"{lo:g}-{hi:g}"

    return None


def _versions(trips: Sequence[TripData], channels: Sequence[str]) -> Dict[str, List[str]]:
    out: Dict[str, set] = {}

    for trip in trips:
        for ch in channels:
            for v in trip.versions.get(ch, ()):
                out.setdefault(ch, set()).add(v)

    return {ch: sorted(vs) for ch, vs in sorted(out.items())}


def _context_flags(trips: Sequence[TripData], channels: Sequence[str]) -> List[str]:
    return [
        f"{ch} decoded with mapping versions {', '.join(v or '?' for v in vs)} "
        f"across the population"
        for ch, vs in _versions(trips, channels).items() if len(vs) > 1
    ]


def _compatibility(segments: Sequence[Segment], population: Segment,
                   dropped: Sequence[Dict[str, Any]], value_channels: Sequence[str],
                   context_channels: Sequence[str]) -> Dict[str, Any]:
    earlier = [s.as_dict() for s in segments if s is not population]

    return {
        "policy": {
            "value_channels": "refused: a mapping-version change or a vehicle "
                              "event starts a new population; only the newest "
                              "population is compared",
            "context_channels": "flagged: a version change is reported and "
                                "lowers confidence, samples are kept",
        },
        "value_channels": list(value_channels),
        "context_channels": list(context_channels),
        "population": population.as_dict(),
        "earlier_populations_excluded": earlier,
        "trips_dropped": list(dropped),
        "context_versions": _versions(population.trips, context_channels),
    }


def _population(trips: Sequence[TripData], value_channels: Sequence[str],
                events: Sequence[VehicleEvent]):
    segments, dropped = segment(trips, value_channels, events)

    return segments, (segments[-1] if segments else None), dropped


def _refused(result: ModelResult, dropped: Sequence[Dict[str, Any]]) -> ModelResult:
    result.status = "unavailable"
    result.unavailable_reason = (
        "every trip was refused for mapping-version reasons: "
        + "; ".join(d["reason"] for d in dropped)
    )
    result.notes.extend(f"{d['trip_uid']}: {d['reason']}" for d in dropped)

    return result


# --------------------------------------------------------------- warm-up


def warmup_model(trips: Sequence[TripData], definition: BaselineDefinition,
                 events: Sequence[VehicleEvent] = ()) -> ModelResult:
    """
    Cold-start warm-up per trip, compared across trips at like ambient.

    Per cold-start trip: time from the first coolant sample to 60/80/90
    °C, the least-squares warm-up slope to 80 °C, the oil-vs-coolant lag
    at 60 °C, and the stabilised coolant temperature; with the ambient,
    the share of the ramp spent moving, and the mean OBD load as context.

    Cooling after shutdown is NOT modelled: recording stops with the
    ignition, so it is never observed.
    """
    context_channels = AMBIENT_CHANNELS + SPEED_CHANNELS + LOAD_CHANNELS
    with_coolant = [t for t in trips if t.first_present(COOLANT_CHANNELS)]

    def value_channels(trip: TripData) -> List[str]:
        """The coolant and oil channels this trip actually uses."""
        return [ch for ch in (trip.first_present(COOLANT_CHANNELS),
                              trip.first_present(OIL_CHANNELS)) if ch]
    result = ModelResult(model="warmup", status="computed", channels={
        "coolant": list(COOLANT_CHANNELS), "oil": list(OIL_CHANNELS),
        "ambient": list(AMBIENT_CHANNELS), "speed": list(SPEED_CHANNELS),
        "load": list(LOAD_CHANNELS),
    })

    if not with_coolant:
        result.status = "unavailable"
        result.unavailable_reason = "no eligible trip has a usable coolant channel"

        return result

    segments, population, dropped = _population(with_coolant, value_channels, events)

    if population is None:
        return _refused(result, dropped)

    compatibility = _compatibility(segments, population, dropped,
                                   sorted(population.versions), context_channels)
    flags = _context_flags(population.trips, context_channels)
    per_trip: List[Dict[str, Any]] = []
    not_cold = 0

    for trip in population.trips:
        figures = _warmup_trip(trip, definition)

        if figures is None:
            not_cold += 1
            continue

        per_trip.append(figures)

    result.notes.append(
        f"{len(per_trip)} cold start(s) among {len(population.trips)} trips "
        f"in the current population ({not_cold} started warm)"
    )
    cells = sorted({f["ambient_band"] for f in per_trip})
    metrics = (
        [(f"time_to_{int(t)}c", "s", f"time_to_{int(t)}c_s") for t in definition.warmup_targets_c]
        + [("warmup_slope", "°C/min", "warmup_slope_c_per_min"),
           ("oil_lag_to_60c", "s", "oil_lag_to_60c_s"),
           ("stabilised_coolant", "°C", "stabilised_coolant_c")]
    )

    for band in cells:
        in_band = [f for f in per_trip if f["ambient_band"] == band]

        for name, unit, key in metrics:
            observations = [
                Observation(trip=f["_trip"], values=[f[key]],
                            context={"moving_fraction": f["moving_fraction"],
                                     "load_mean_pct": f["load_mean_pct"],
                                     "ambient_c": f["ambient_c"]})
                for f in in_band if f.get(key) is not None
            ]
            comparison = compare(observations, definition, per_trip=True,
                                 context_flags=flags, digits=1)
            #
            # Survivorship: a crossing metric only sees the trips that
            # reached its target, and a SLOWER warm-up is exactly what
            # makes a trip fail to reach it - so the metric under-detects
            # slowing. The fraction is reported per cell so the effect is
            # visible, and noted when it is not 1.
            #
            reached = len(observations)
            reached_fraction = round(reached / len(in_band), 2) if in_band else None
            survivorship = (
                [f"{len(in_band) - reached} of {len(in_band)} cold start(s) in "
                 f"this band never produced {name} (trip ended first); a "
                 f"slower warm-up is the likeliest reason, so a slowing "
                 f"trend is under-detected here"]
                if reached_fraction is not None and reached_fraction < 1.0 else []
            )
            metric = HealthMetric(
                model="warmup", metric=name, unit=unit,
                condition={
                    "ambient_c": band,
                    "start": f"cold (first coolant < {definition.cold_start_max_c:g} °C)",
                    "window": {"ambient_bin_c": definition.ambient_bin_c},
                },
                value=comparison.value,
                baseline=comparison.baseline, current=comparison.current,
                drift=comparison.drift,
                sample_count=sum(len(o.values) for o in observations),
                coverage=dict(comparison.coverage, cold_starts_in_band=len(in_band),
                              cold_starts_reached=reached,
                              reached_fraction=reached_fraction,
                              trips_in_population=len(population.trips),
                              not_cold_starts=not_cold),
                quality_filters=list(QUALITY_FILTER_TEXT),
                alignment={
                    "note": "interval aggregates; no pairwise subtraction, "
                            "so no pair tolerance applies. Crossing times are "
                            "resolved to the coolant sampling interval "
                            "(see observations[].resolution_s)",
                },
                confidence=comparison.confidence,
                compatibility=compatibility,
                baseline_definition=definition.as_dict(),
                unavailable_reason=comparison.unavailable_reason,
                observations=[
                    {k: v for k, v in f.items() if not k.startswith("_")}
                    for f in in_band
                ],
            )

            metric.notes.extend(survivorship)

            if flags:
                metric.notes.extend(flags)

            result.metrics.append(metric)

    if not result.metrics:
        result.status = "unavailable"
        result.unavailable_reason = (
            f"no cold start in the current population: every trip's first "
            f"coolant reading was >= {definition.cold_start_max_c:g} °C"
        )

    return result


def _warmup_trip(trip: TripData, d: BaselineDefinition) -> Optional[Dict[str, Any]]:
    coolant_key = trip.first_present(COOLANT_CHANNELS)
    coolant = trip.series[coolant_key]
    t0, start_c = coolant[0]

    if start_c >= d.cold_start_max_c:
        return None

    oil_key = trip.first_present(OIL_CHANNELS)
    oil = trip.series.get(oil_key, []) if oil_key else []
    speed = trip.series.get(trip.first_present(SPEED_CHANNELS) or "", [])
    ambient = trip.series.get(trip.first_present(AMBIENT_CHANNELS) or "", [])
    load = trip.series.get(trip.first_present(LOAD_CHANNELS) or "", [])
    out: Dict[str, Any] = {
        "_trip": trip,
        "trip_uid": trip.uid,
        "started": t0,
        "coolant_channel": coolant_key,
        "oil_channel": oil_key,
        "start_coolant_c": round(start_c, 1),
    }
    crossings: Dict[float, Optional[float]] = {}

    for target in d.warmup_targets_c:
        at = first_crossing(coolant, target)
        crossings[target] = at
        key = f"time_to_{int(target)}c_s"

        if at is None:
            out[key] = None
            continue

        out[key] = round(at - t0, 1)
        #
        # The crossing is known to the sampling interval, not better.
        #
        idx = [t for t, _ in coolant].index(at)
        out[f"resolution_{int(target)}c_s"] = (
            round(at - coolant[idx - 1][0], 1) if idx > 0 else None
        )

    #
    # The slope is defined over the ramp to the 80 °C crossing and ONLY
    # there. A trip that ended before 80 °C has a ramp that stops
    # wherever the trip did, and the slope of a truncated exponential is
    # steeper than the slope of the whole of it - pooling the two would
    # make "drift" a function of trip length, and short winter trips are
    # routine on this car. So: no crossing, no slope, and the flag is
    # echoed so the reader can see which trips were complete.
    #
    t80 = crossings.get(80.0)
    out["ramp_complete"] = t80 is not None
    ramp_end = t80 if t80 is not None else coolant[-1][0]
    ramp = [(t - t0, v) for t, v in coolant if t <= ramp_end]
    slope = (
        least_squares_slope(ramp)
        if t80 is not None and len(ramp) >= 3 else None
    )
    out["warmup_slope_c_per_min"] = None if slope is None else round(slope * 60.0, 2)

    oil60 = first_crossing(oil, 60.0) if oil else None
    cool60 = crossings.get(60.0)
    out["oil_lag_to_60c_s"] = (
        round(oil60 - cool60, 1) if oil60 is not None and cool60 is not None else None
    )

    #
    # Stabilised: after the thermostat has had time to settle, and moving,
    # so a long idle at a light is not mistaken for the cruising plateau.
    #
    stabilised = None

    if t80 is not None:
        tolerance = pairing_for(coolant_key, "speed").max_age_s
        speed_index = _Indexed(speed)
        plateau = [
            v for t, v in coolant
            if t >= t80 + d.stabilised_after_s
            and (speed_index.nearest(t, tolerance) or 0.0) > d.moving_speed_kmh
        ]

        if len(plateau) >= 5:
            stabilised = round(median(plateau), 1)

    out["stabilised_coolant_c"] = stabilised

    # Context over the ramp: interval aggregates, no pairing needed.
    ramp_speed = [v for t, v in speed if t0 <= t <= ramp_end]
    out["moving_fraction"] = (
        round(sum(1 for v in ramp_speed if v > d.moving_speed_kmh) / len(ramp_speed), 2)
        if ramp_speed else None
    )
    ramp_load = [v for t, v in load if t0 <= t <= ramp_end]
    out["load_mean_pct"] = round(sum(ramp_load) / len(ramp_load), 1) if ramp_load else None
    ramp_ambient = [v for t, v in ambient if t0 <= t <= ramp_end] or [v for _, v in ambient]
    ambient_c = round(median(ramp_ambient), 1) if ramp_ambient else None
    out["ambient_c"] = ambient_c

    if ambient_c is None:
        out["ambient_band"] = "unknown"
    else:
        lo = (ambient_c // d.ambient_bin_c) * d.ambient_bin_c
        out["ambient_band"] = f"{lo:g}-{lo + d.ambient_bin_c:g}"

    return out


# -------------------------------------------------------------- tracking


def tracking_model(kind: str, trips: Sequence[TripData],
                   definition: BaselineDefinition,
                   events: Sequence[VehicleEvent] = ()) -> ModelResult:
    """
    Actual minus setpoint for a control loop, at steady operating points,
    per RPM x demand cell; plus the transient population pooled and
    labelled as such.

    "Steady" is judged from the 10 Hz motion tier around each aligned
    pair (RPM and pedal ranges over +/- `steady_window_s`), which is
    what makes the residual attributable to the actuator rather than to
    the 0.56 s the two reads are apart.
    """
    spec = TRACKING[kind]
    actual, setpoint, unit = spec["actual"], spec["setpoint"], spec["unit"]
    value_channels = (actual, setpoint)
    context_channels = RPM_CHANNELS + DEMAND_CHANNELS + CHARGE_AIR_CHANNELS
    result = ModelResult(model=kind, status="computed", channels={
        "actual": actual, "setpoint": setpoint,
        "rpm": list(RPM_CHANNELS), "demand": list(DEMAND_CHANNELS),
        "charge_air": list(CHARGE_AIR_CHANNELS),
    })
    with_pair = [t for t in trips if t.series.get(actual) and t.series.get(setpoint)]

    if not with_pair:
        result.status = "unavailable"
        result.unavailable_reason = (
            f"no eligible trip has usable samples of both {actual} and {setpoint}"
        )

        return result

    segments, population, dropped = _population(with_pair, value_channels, events)

    if population is None:
        return _refused(result, dropped)

    compatibility = _compatibility(segments, population, dropped,
                                   value_channels, context_channels)
    flags = _context_flags(population.trips, context_channels)
    pairing = pairing_for(actual, setpoint)
    charge_pairing = pairing_for(actual, CHARGE_AIR_CHANNELS[0])
    #
    # cell -> per-trip observation, in trip order. The pooled transient
    # population is one more "cell".
    #
    cells: Dict[Tuple[str, str, str], List[Observation]] = {}
    transient: List[Observation] = []
    unassessed = 0
    gaps: List[float] = []
    alignment_attempted = alignment_matched = 0
    rpm_tolerance = pairing_for(actual, RPM_CHANNELS[0]).max_age_s

    for trip in population.trips:
        aligned = align(trip.series[actual], trip.series[setpoint], pairing.max_age_s)
        alignment_attempted += aligned.attempted
        alignment_matched += aligned.matched

        if aligned.median_gap_s is not None:
            gaps.append(aligned.median_gap_s)

        rpm = _Indexed(trip.series.get(trip.first_present(RPM_CHANNELS) or "", []))
        demand_key = trip.first_present(DEMAND_CHANNELS)
        demand = _Indexed(trip.series.get(demand_key or "", []))
        charge = _Indexed(trip.series.get(trip.first_present(CHARGE_AIR_CHANNELS) or "", []))
        per_cell: Dict[Tuple[str, str, str], Observation] = {}
        trip_transient = Observation(trip=trip, values=[],
                                     attempted=aligned.attempted,
                                     matched=aligned.matched)

        for ts, a, s in aligned.pairs:
            residual = a - s
            rpm_window = rpm.window(ts, definition.steady_window_s)
            demand_window = demand.window(ts, definition.steady_window_s)
            enough = definition.steady_min_context_samples

            if len(rpm_window) < enough or len(demand_window) < enough:
                unassessed += 1
                continue

            steady = (
                max(rpm_window) - min(rpm_window) <= definition.steady_rpm_range
                and max(demand_window) - min(demand_window)
                <= definition.steady_pedal_range_pct
            )

            if not steady:
                trip_transient.values.append(residual)
                continue

            rpm_now = rpm.nearest(ts, rpm_tolerance)
            demand_now = demand.nearest(ts, rpm_tolerance)

            if rpm_now is None or demand_now is None:
                unassessed += 1
                continue

            rpm_bin = _bin(rpm_now, definition.rpm_bins)
            demand_bin = _bin(demand_now, definition.demand_bins_pct)

            if rpm_bin is None or demand_bin is None:
                unassessed += 1
                continue

            key = (rpm_bin, demand_key or "", demand_bin)
            obs = per_cell.get(key)

            if obs is None:
                obs = per_cell[key] = Observation(
                    trip=trip, values=[], attempted=aligned.attempted,
                    matched=aligned.matched, context={},
                )
                obs.context["_charge"] = []

            obs.values.append(residual)
            charge_now = charge.nearest(ts, charge_pairing.max_age_s)

            if charge_now is not None:
                obs.context["_charge"].append(charge_now)

        for key, obs in per_cell.items():
            samples = obs.context.pop("_charge")

            if samples:
                obs.context["charge_air_c"] = round(median(samples), 1)

            cells.setdefault(key, []).append(obs)

        if trip_transient.values:
            transient.append(trip_transient)

    alignment = {
        "pair": [actual, setpoint],
        "max_age_s": pairing.max_age_s,
        "why": pairing.why,
        "median_gap_s": round(median(gaps), 3) if gaps else None,
        "context": {
            "rpm_and_demand_max_age_s": rpm_tolerance,
            "charge_air_max_age_s": charge_pairing.max_age_s,
        },
        "population_alignment_pct": (
            round(100.0 * alignment_matched / alignment_attempted, 1)
            if alignment_attempted else 0.0
        ),
    }
    steady_window = {
        "steady_window_s": definition.steady_window_s,
        "steady_rpm_range": definition.steady_rpm_range,
        "steady_pedal_range_pct": definition.steady_pedal_range_pct,
        "steady_min_context_samples": definition.steady_min_context_samples,
    }
    steady_total = sum(len(o.values) for obs in cells.values() for o in obs)
    transient_total = sum(len(o.values) for o in transient)
    result.notes.append(
        f"{alignment_matched} of {alignment_attempted} actual samples had a "
        f"setpoint within {pairing.max_age_s:g} s; of those {steady_total} "
        f"steady, {transient_total} transient, {unassessed} unassessed "
        f"(no motion-tier context in the window)"
    )

    if alignment_attempted and not alignment_matched:
        result.notes.append(
            f"the pair never aligned: the two reads sit further apart than "
            f"the {pairing.max_age_s:g} s tolerance on every eligible trip "
            f"(a schedule fact, not a car fact)"
        )

    def build(name: str, condition: Dict[str, Any], observations: List[Observation],
              extra_notes: Sequence[str] = ()) -> HealthMetric:
        comparison = compare(observations, definition, per_trip=False,
                             context_flags=flags, digits=1)
        metric = HealthMetric(
            model=kind, metric=name, unit=unit, condition=condition,
            value=comparison.value,
            baseline=comparison.baseline, current=comparison.current,
            drift=comparison.drift,
            sample_count=sum(len(o.values) for o in observations),
            coverage=dict(comparison.coverage,
                          steady_samples_total=steady_total,
                          transient_samples_total=transient_total,
                          unassessed_samples=unassessed,
                          trips_in_population=len(population.trips)),
            quality_filters=list(QUALITY_FILTER_TEXT),
            alignment=alignment,
            confidence=comparison.confidence,
            compatibility=compatibility,
            baseline_definition=definition.as_dict(),
            unavailable_reason=comparison.unavailable_reason,
            notes=list(extra_notes) + list(flags),
        )

        return metric

    for key in sorted(cells):
        rpm_bin, demand_key, demand_bin = key
        result.metrics.append(build(
            "residual_steady",
            {"rpm": rpm_bin, "demand": f"{demand_key} {demand_bin} %",
             "state": "steady", "window": steady_window},
            cells[key],
        ))

    if transient:
        result.metrics.append(build(
            "residual_transient",
            {"rpm": "any", "demand": "any", "state": "transient",
             "window": steady_window},
            transient,
            extra_notes=[
                "transient pairs pooled across operating points: the "
                f"{alignment.get('median_gap_s')} s between the two reads "
                f"injects error of its own here ({spec['transient_error']}); "
                "descriptive only"
            ],
        ))

    if not result.metrics:
        result.status = "unavailable"
        result.unavailable_reason = (
            "no aligned pair could be placed in a steady or transient "
            "population: " + result.notes[-1]
        )

    return result


# ------------------------------------------------------------------- egr


def egr_model(trips: Sequence[TripData], definition: BaselineDefinition,
              events: Sequence[VehicleEvent] = ()) -> ModelResult:
    """
    Declared unavailable, deliberately.

    The car exposes `n47d_egr_deviation` (a control deviation the ECU
    already computed) and the OBD `egr` / `egrerr` percentages, but no
    requested/actual pair of the kind boost and rail have. A model over
    the ECU's own deviation would restate the ECU, not check it, and a
    residual cannot be built from one side of a loop.
    """
    return ModelResult(
        model="egr", status="unavailable",
        unavailable_reason="requested/actual not yet mapped: only a control "
                           "deviation (n47d_egr_deviation) and OBD commanded/"
                           "error percentages exist; no residual can be built "
                           "from one side of the loop",
        channels={"present_but_insufficient": ["n47d_egr_deviation", "egr", "egrerr"]},
    )
