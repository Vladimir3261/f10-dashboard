"""
Baselining: which trips may be pooled, which are the reference, and how
the current population is compared with it.

A baseline is a claim about ONE configuration of ONE car decoded ONE way.
Three things break that and each one segments the history here:

  * a declared vehicle event (oil change, sensor swap, remap - see
    `bmwdiag.vehicle.RESETS_BASELINE`; an unknown kind segments too,
    because "something was done to the car" is safest read as mattering);
  * a mapping-version change on a VALUE channel - one whose number enters
    the metric. A version bump means the file's content changed, and
    nothing in the version says whether that was the decode, so the
    populations are REFUSED to pool: the comparison happens inside the
    newest segment only, and the report says what was cut off and why;
  * a mapping-version change on a CONTEXT channel - one used only to
    select samples (RPM, pedal, speed). That is FLAGGED, not refused: the
    values still describe the car, but a bin edge may have moved, and the
    confidence grade carries the flag.

The comparison is always earliest-vs-latest inside the segment: the
reference is the first `reference_trips` eligible trips, the current
population is the last `current_trips`, and they may not overlap. Fewer
trips than that is not a weaker answer, it is no answer, with a reason.

Drift is stated against the baseline's own scatter (a fraction of its
p10-p90 spread) and backed by a rank-sum statistic, both from the
definition, both echoed. It is only declared when the data grade is at
least moderate: the project prefers a missed change to an invented one.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from bmwdiag.vehicle import VehicleEvent, events_between

from analysis.health.contract import (
    BaselineDefinition, Confidence, grade_confidence,
)
from analysis.health.eligibility import TripData
from analysis.health.stats import describe, quantile, rank_sum_z

__all__ = [
    "Segment",
    "segment",
    "Observation",
    "Comparison",
    "compare",
]


@dataclass
class Segment:
    """A run of trips that may be pooled, and why the run starts."""

    trips: List[TripData]
    reason: str
    #: value channel -> its single version across the segment.
    versions: Dict[str, str] = field(default_factory=dict)

    @property
    def started(self) -> float:
        return self.trips[0].started

    @property
    def ended(self) -> float:
        return self.trips[-1].ended

    def as_dict(self) -> Dict[str, Any]:
        return {
            "trips": len(self.trips),
            "from": self.started,
            "to": self.ended,
            "reason": self.reason,
            "versions": dict(self.versions),
        }


ValueChannels = Union[Sequence[str], Callable[[TripData], Sequence[str]]]


def segment(trips: Sequence[TripData], value_channels: ValueChannels,
            events: Sequence[VehicleEvent] = ()) -> Tuple[List[Segment], List[Dict[str, Any]]]:
    """
    Split trips (chronological) into poolable segments.

    `value_channels` is the list of channels whose numbers enter the
    metric, or a function of the trip returning them - a model that
    prefers the DDE coolant read and falls back to the OBD one must
    segment on the channel it actually USED, and a switch from one to
    the other is itself a population break (different sensor path,
    different resolution).

    Returns the segments and the trips dropped outright: a trip whose
    own runs decoded a value channel with two versions cannot be placed
    in either population.
    """
    segments: List[Segment] = []
    dropped: List[Dict[str, Any]] = []
    current: Optional[Segment] = None

    for trip in sorted(trips, key=lambda t: (t.started, t.uid)):
        used = (value_channels(trip) if callable(value_channels)
                else value_channels)
        versions = {ch: trip.version_of(ch) for ch in used
                    if ch in trip.versions}
        mixed = {ch: v for ch, v in versions.items() if v.startswith("mixed:")}

        if mixed:
            dropped.append({
                "trip_uid": trip.uid,
                "reason": "value channel decoded with more than one mapping "
                          "version inside the trip: "
                          + ", ".join(f"{ch}={v[6:]}" for ch, v in sorted(mixed.items())),
            })
            continue

        reason = None

        if current is not None:
            between = events_between(events, current.ended, trip.started)

            if between:
                reason = "vehicle event: " + "; ".join(e.describe() for e in between)
            elif set(versions) != set(current.versions):
                reason = (
                    "value channels changed "
                    f"{','.join(sorted(current.versions)) or '-'} -> "
                    f"{','.join(sorted(versions)) or '-'}"
                )
            else:
                for ch, version in sorted(versions.items()):
                    before = current.versions.get(ch)

                    if before is not None and before != version:
                        reason = (f"mapping version of {ch} changed "
                                  f"{before or '?'} -> {version or '?'}")
                        break

        if current is None or reason is not None:
            current = Segment(trips=[], reason=reason or "first eligible trip")
            segments.append(current)

        current.trips.append(trip)

        for ch, version in versions.items():
            current.versions.setdefault(ch, version)

    return segments, dropped


# ------------------------------------------------------------ comparison


@dataclass
class Observation:
    """What one trip contributed to one metric cell."""

    trip: TripData
    values: List[float]
    #: Alignment bookkeeping for per-sample metrics; zero for per-trip.
    attempted: int = 0
    matched: int = 0
    #: Extra context the model wants echoed per side (e.g. charge-air).
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Comparison:
    baseline: Optional[Dict[str, Any]]
    current: Optional[Dict[str, Any]]
    drift: Optional[Dict[str, Any]]
    confidence: Confidence
    coverage: Dict[str, Any]
    unavailable_reason: Optional[str]
    value: Optional[float]


def _side(observations: Sequence[Observation], digits: int) -> Dict[str, Any]:
    values = [v for o in observations for v in o.values]
    out = describe(values, digits)
    out.update({
        "trips": len(observations),
        "from": observations[0].trip.started,
        "to": observations[-1].trip.ended,
        "trip_uids": [o.trip.uid for o in observations],
        "modes": sorted({m for o in observations for m in o.trip.modes}),
    })
    numeric = {}

    for o in observations:
        for k, v in o.context.items():
            if isinstance(v, (int, float)):
                numeric.setdefault(k, []).append(float(v))

    if numeric:
        out["context"] = {k: round(quantile(v, 0.5), digits)
                          for k, v in sorted(numeric.items())}

    return out


def compare(observations: Sequence[Observation], definition: BaselineDefinition,
            *, per_trip: bool, context_flags: Sequence[str] = (),
            digits: int = 2) -> Comparison:
    """
    Reference (earliest trips) versus current (latest trips), or why not.

    `observations` are one per trip that contributed anything, in trip
    order. For per-sample metrics each carries the trip's samples and
    its alignment counts; for per-trip metrics each carries one value.
    """
    contributing = [o for o in observations if o.values]
    attempted = sum(o.attempted for o in observations)
    matched = sum(o.matched for o in observations)
    coverage: Dict[str, Any] = {
        "trips_with_data": len(contributing),
        "samples_usable": sum(len(o.values) for o in contributing),
    }

    if attempted:
        coverage["pairs_attempted"] = attempted
        coverage["pairs_matched"] = matched
        coverage["alignment_pct"] = round(100.0 * matched / attempted, 1)

    coverage_pct = coverage.get("alignment_pct")
    need = definition.reference_trips + definition.current_trips

    def unavailable(reason: str) -> Comparison:
        return Comparison(None, None, None, Confidence("none", {}, [reason]),
                          coverage, reason, None)

    if len(contributing) < need:
        return unavailable(
            f"insufficient trips: {len(contributing)} with data, the baseline "
            f"definition needs {definition.reference_trips} reference + "
            f"{definition.current_trips} current, non-overlapping"
        )

    reference = contributing[:definition.reference_trips]
    current = contributing[-definition.current_trips:]
    base_values = [v for o in reference for v in o.values]
    cur_values = [v for o in current for v in o.values]
    min_n = (definition.min_trip_observations_per_side if per_trip
             else definition.min_samples_per_side)

    if len(base_values) < min_n or len(cur_values) < min_n:
        return unavailable(
            f"insufficient samples: baseline {len(base_values)}, current "
            f"{len(cur_values)}, need {min_n} per side"
        )

    #
    # Coverage is judged BEFORE a number is produced, exactly as the
    # alignment contract does for a single session: under the usable
    # threshold the pairs that did match describe the schedule more than
    # the car.
    #
    same_modes = ({m for o in reference for m in o.trip.modes}
                  == {m for o in current for m in o.trip.modes})
    confidence = grade_confidence(
        baseline_n=len(base_values), current_n=len(cur_values),
        baseline_trips=len(reference), current_trips=len(current),
        coverage_pct=coverage_pct, same_modes=same_modes,
        context_flags=context_flags, per_trip=per_trip,
        definition=definition,
    )

    if confidence.grade == "none":
        return unavailable("; ".join(confidence.reasons))

    if not confidence.rules.get("coverage_usable", True):
        reason = (f"alignment coverage {coverage_pct}% is below the usable "
                  f"threshold; the matched pairs are not representative")

        # The rules were evaluated; keep them so the reader sees which
        # one refused, not just that one did.
        return Comparison(None, None, None,
                          Confidence("none", confidence.rules, [reason]),
                          coverage, reason, None)

    baseline_side = _side(reference, digits)
    current_side = _side(current, digits)
    z, p_exceed = rank_sum_z(base_values, cur_values)
    delta = current_side["median"] - baseline_side["median"]
    spread = baseline_side["p90"] - baseline_side["p10"]
    material_threshold = definition.material_fraction_of_spread * spread
    material = (abs(delta) >= material_threshold) if spread > 0 else delta != 0
    supported = abs(z) >= definition.drift_z
    detected = material and supported and confidence.grade in ("moderate", "high")
    drift = {
        "delta": round(delta, digits),
        "delta_fraction_of_baseline_spread": (
            round(delta / spread, 3) if spread > 0 else None
        ),
        "material_threshold": round(material_threshold, digits),
        "material": material,
        "z": round(z, 2),
        "p_exceed": round(p_exceed, 3),
        "supported": supported,
        "detected": detected,
        "direction": "up" if delta > 0 else ("down" if delta < 0 else "flat"),
        "span_s": round(current_side["to"] - baseline_side["from"], 0),
        "rule": (
            f"detected iff |delta| >= {definition.material_fraction_of_spread} x "
            f"baseline (p90-p10) and |z| >= {definition.drift_z} and "
            f"confidence >= moderate"
        ),
    }

    return Comparison(baseline_side, current_side, drift, confidence,
                      coverage, None, current_side["median"])
