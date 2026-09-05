"""
The output contract: what a health metric is allowed to look like.

A number about the car's health is only as good as the reader's ability
to check it, so the value never travels alone. Every `HealthMetric`
carries the baseline it was compared against, the counts on both sides,
the coverage the eligibility filters left, the alignment tolerance and
the operating-condition window it was computed under, the mapping
versions in force, and a confidence grade with the rules that produced
it. When the metric cannot be computed, `unavailable_reason` says why, in
the same object, so "no number" is never mistaken for "no problem".

The baseline itself is DATA, not code (`BaselineDefinition`): a
versioned description of how the reference and current populations are
chosen and what counts as enough, echoed back inside every result. Two
reports that disagree can be diffed on their definitions; a report
cannot quietly be computed against a baseline nobody can name.

Confidence is a grade with an explicit numeric meaning, evaluated by
`grade_confidence` and stated in docs/HEALTH_MODELS.md. It grades the
STRENGTH OF THE DATA behind the comparison, not the size of the effect:
the effect is reported separately as a drift with its own evidence.
"""

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from analysis.alignment import MIN_USEFUL_COVERAGE

__all__ = [
    "BaselineDefinition",
    "DEFAULT_DEFINITION",
    "Confidence",
    "GRADES",
    "grade_confidence",
    "HealthMetric",
    "ModelResult",
    "HealthReport",
]


@dataclass(frozen=True)
class BaselineDefinition:
    """
    How a baseline is built. Versioned, serialisable, echoed in results.

    `version` is bumped when any rule here changes meaning, so a stored
    result can be told apart from one produced under a different rule.

    Where each constant comes from is stated next to it; the ones taken
    from measurement name the measurement (docs/HEALTH_MODELS.md has the
    tables). None of them is a claim about the car - they are claims
    about how much data it takes before this code will say anything.
    """

    id: str = "f10-health-baseline"
    version: int = 1

    #: The reference population is the FIRST `reference_trips` eligible
    #: trips of the current population segment; the compared population
    #: is the LAST `current_trips`. They must not overlap. Earliest-first
    #: because the question is "has it changed since we started looking",
    #: and a sliding baseline would follow the drift it is meant to catch.
    reference_trips: int = 5
    current_trips: int = 3

    #: Per-sample metrics (tracking residuals): the fewest samples a side
    #: may have. 30 so that the rank-sum normal approximation is honest
    #: (rule of thumb: both sides above ~20) and p10/p90 are at least the
    #: third order statistic rather than the extremes.
    min_samples_per_side: int = 30
    #: And the fewest distinct trips a side may draw those samples from,
    #: so one unusual drive cannot be a whole population.
    min_trips_per_side: int = 2

    #: Per-trip metrics (warm-up): one observation per trip, so the trip
    #: minimum IS the sample minimum. 3 is the smallest count for which a
    #: median is not one of the two extremes.
    min_trip_observations_per_side: int = 3

    #: A shift counts as material when the current median moves by at
    #: least this fraction of the baseline's own p10-p90 spread. Relative
    #: to the car's own scatter by construction: no other vehicle's
    #: tolerance is assumed. Half the spread is the point at which the
    #: current median sits where fewer than a quarter of baseline
    #: samples did.
    material_fraction_of_spread: float = 0.5
    #: And supported when the rank-sum |z| reaches this. 2.0 is the
    #: conventional two-sided ~5% level. Deliberately not lower.
    drift_z: float = 2.0

    #: Steady-state gate for the tracking models: a pair is steady when
    #: RPM and pedal each stayed within these ranges over +/- `window_s`
    #: around the sample. Measured on the lake's clock-synced sessions
    #: (2026-09-05, 623 boost pairs): with an RPM range under 50 over
    #: +/-2 s the residual's p10-p90 spread is 34 hPa; the next band
    #: (50..100) is 283 hPa and everything above is 460-760 hPa. Pedal
    #: under 2% gives 41 hPa against 181+ above it. The gate isolates the
    #: population the misalignment cannot corrupt.
    steady_window_s: float = 2.0
    steady_rpm_range: float = 50.0
    steady_pedal_range_pct: float = 2.0
    #: Fewest context samples inside the window before "steady" can be
    #: judged at all; below this the pair is neither steady nor
    #: transient, it is unassessed.
    steady_min_context_samples: int = 4

    #: Operating-condition cells for the tracking models. RPM x pedal,
    #: because pedal is sampled at 10 Hz beside RPM and is the driver's
    #: demand; `load` and `maf` are fallbacks when pedal is absent.
    rpm_bins: Sequence[Sequence[float]] = (
        (600, 1000), (1000, 1500), (1500, 2000), (2000, 2500),
        (2500, 3000), (3000, 5000),
    )
    demand_bins_pct: Sequence[Sequence[float]] = (
        (0, 3), (3, 15), (15, 40), (40, 100.01),
    )

    #: Warm-up: a trip is a cold start when its first coolant reading is
    #: below this. 40 °C is well under the thermostat and above any
    #: ambient the car sees, so the ramp always contains the 60 °C mark
    #: and starts from the engine block having equalised with the air.
    cold_start_max_c: float = 40.0
    warmup_targets_c: Sequence[float] = (60.0, 80.0, 90.0)
    #: Ambient conditioning band for warm-up comparisons: trips are
    #: comparable when their ambient medians fall in the same band.
    ambient_bin_c: float = 5.0
    #: Stabilised coolant: median over samples from this long after the
    #: 80 °C crossing, while moving. Two minutes lets the thermostat
    #: settle after it opens.
    stabilised_after_s: float = 120.0
    #: "Moving" for idle-vs-moving context, km/h. Same figure the session
    #: report uses (DRIVING_SPEED).
    moving_speed_kmh: float = 3.0

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}

        for name in self.__dataclass_fields__:
            value = getattr(self, name)

            if isinstance(value, tuple):
                value = [list(v) if isinstance(v, tuple) else v for v in value]

            out[name] = copy.deepcopy(value)

        return out

    def replace(self, **changes: Any) -> "BaselineDefinition":
        current = self.as_dict()
        current.update(changes)

        return BaselineDefinition(**current)


DEFAULT_DEFINITION = BaselineDefinition()


# ------------------------------------------------------------ confidence

#: Ordered weakest to strongest. `none` is reserved for an unavailable
#: metric, so a grade never has to be read alongside a missing value to
#: know which it is.
GRADES = ("none", "low", "moderate", "high")


@dataclass
class Confidence:
    """A grade and every rule that was evaluated to reach it."""

    grade: str
    rules: Dict[str, bool] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"grade": self.grade, "rules": dict(self.rules),
                "reasons": list(self.reasons)}


def grade_confidence(*, baseline_n: int, current_n: int,
                     baseline_trips: int, current_trips: int,
                     coverage_pct: Optional[float],
                     same_modes: bool, context_flags: Sequence[str],
                     per_trip: bool,
                     definition: BaselineDefinition) -> Confidence:
    """
    Grade the strength of the data behind a comparison.

    The rules, all deterministic, all echoed in the result:

      high      both sides have >= 100 samples from >= 3 trips
                (per-trip metrics: >= 5 trips), alignment coverage
                >= 80 %, both sides recorded in the same drive mode(s),
                no context-channel version flag;
      moderate  both sides meet the definition's minimums, coverage
                >= MIN_USEFUL_COVERAGE (50 %), same modes;
      low       the minimums are met but a moderate rule fails - the
                comparison is computed and reported, and the reader is
                told not to lean on it.

    Below the minimums nothing is graded: the metric is unavailable and
    says so. Coverage is None for metrics without an alignment step
    (per-trip warm-up figures) and that rule is then vacuous.
    """
    if per_trip:
        min_n = definition.min_trip_observations_per_side
        high_n, high_trips = 5, 5
        min_trips = min_n
    else:
        min_n = definition.min_samples_per_side
        min_trips = definition.min_trips_per_side
        high_n, high_trips = 100, 3

    rules = {
        "min_samples": baseline_n >= min_n and current_n >= min_n,
        "min_trips": baseline_trips >= min_trips and current_trips >= min_trips,
        "coverage_usable": coverage_pct is None or coverage_pct >= MIN_USEFUL_COVERAGE,
        "same_drive_modes": same_modes,
        "no_context_version_flags": not context_flags,
        "high_samples": baseline_n >= high_n and current_n >= high_n,
        "high_trips": baseline_trips >= high_trips and current_trips >= high_trips,
        "high_coverage": coverage_pct is None or coverage_pct >= 80.0,
    }
    reasons: List[str] = []

    if not rules["min_samples"] or not rules["min_trips"]:
        return Confidence("none", rules, ["below the definition's minimum counts"])

    moderate = rules["coverage_usable"] and rules["same_drive_modes"]

    if not rules["coverage_usable"]:
        reasons.append(
            f"alignment coverage {coverage_pct}% is under the "
            f"{MIN_USEFUL_COVERAGE:.0f}% the alignment contract calls usable"
        )

    if not rules["same_drive_modes"]:
        reasons.append("baseline and current were recorded in different "
                       "drive modes (sampling configuration differs)")

    if not moderate:
        return Confidence("low", rules, reasons)

    high = all(rules[k] for k in
               ("high_samples", "high_trips", "high_coverage",
                "no_context_version_flags"))

    if high:
        return Confidence("high", rules, ["all rules met"])

    for key, text in (
        ("high_samples", "fewer than 100 samples on a side"),
        ("high_trips", f"fewer than {high_trips} trips on a side"),
        ("high_coverage", "alignment coverage under 80%"),
        ("no_context_version_flags", "a context channel changed mapping "
                                     "version across the population"),
    ):
        if not rules[key]:
            reasons.append(text)

    return Confidence("moderate", rules, reasons)


# ---------------------------------------------------------------- results


@dataclass
class HealthMetric:
    """
    One conclusion about the car, with everything needed to doubt it.

    `value` is the current population's statistic (a median, or the
    per-trip figure) and is None exactly when `unavailable_reason` is
    set. `baseline` and `current` are `stats.describe()` summaries plus
    their span; `drift` compares them. `coverage` is a flat dict of
    counts - what was recorded, what the filters removed, what aligned -
    because a metric that hides how much it threw away is the failure
    this contract exists to prevent.
    """

    model: str
    metric: str
    unit: str
    condition: Dict[str, Any]
    value: Optional[float] = None
    baseline: Optional[Dict[str, Any]] = None
    current: Optional[Dict[str, Any]] = None
    drift: Optional[Dict[str, Any]] = None
    sample_count: int = 0
    coverage: Dict[str, Any] = field(default_factory=dict)
    quality_filters: List[str] = field(default_factory=list)
    alignment: Dict[str, Any] = field(default_factory=dict)
    confidence: Confidence = field(default_factory=lambda: Confidence("none"))
    compatibility: Dict[str, Any] = field(default_factory=dict)
    baseline_definition: Dict[str, Any] = field(default_factory=dict)
    unavailable_reason: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    #: Per-trip figures behind a per-trip metric (warm-up): what each
    #: eligible trip contributed, with its context, whether or not the
    #: comparison could be made. The evidence stays visible when the
    #: verdict is "not enough of it".
    observations: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "metric": self.metric,
            "unit": self.unit,
            "condition": self.condition,
            "value": self.value,
            "baseline": self.baseline,
            "current": self.current,
            "drift": self.drift,
            "sample_count": self.sample_count,
            "coverage": self.coverage,
            "quality_filters": list(self.quality_filters),
            "alignment": self.alignment,
            "confidence": self.confidence.as_dict(),
            "compatibility": self.compatibility,
            "baseline_definition": self.baseline_definition,
            "unavailable_reason": self.unavailable_reason,
            "notes": list(self.notes),
            "observations": list(self.observations),
        }

    def sentence(self) -> str:
        """The metric as the one line a person would say about it."""
        where = ", ".join(f"{k}={v}" for k, v in self.condition.items()
                          if k not in ("window",))
        head = f"{self.model}/{self.metric}" + (f" [{where}]" if where else "")

        if not self.available:
            return f"{head}: unavailable - {self.unavailable_reason}"

        b, c, d = self.baseline or {}, self.current or {}, self.drift or {}
        text = (
            f"{head}: median {b.get('median')} -> {c.get('median')} "
            f"{self.unit} (baseline n={b.get('n')} over {b.get('trips')} "
            f"trips, current n={c.get('n')} over {c.get('trips')} trips)"
        )

        if d:
            verdict = "DRIFT" if d.get("detected") else "no detected change"
            text += (f"; {verdict}: delta {d.get('delta')} {self.unit}, "
                     f"z={d.get('z')}, P(current > baseline)={d.get('p_exceed')}")

        cov = self.coverage.get("alignment_pct")

        if cov is not None:
            text += f"; {cov}% of attempted pairs aligned"

        return text + f". Confidence: {self.confidence.grade}."


@dataclass
class ModelResult:
    model: str
    status: str                       # "computed" | "unavailable"
    metrics: List[HealthMetric] = field(default_factory=list)
    unavailable_reason: Optional[str] = None
    channels: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "status": self.status,
            "unavailable_reason": self.unavailable_reason,
            "channels": self.channels,
            "notes": list(self.notes),
            "metrics": [m.as_dict() for m in self.metrics],
        }


@dataclass
class HealthReport:
    source: str
    vehicle_label: str
    definition: Dict[str, Any]
    eligibility: Dict[str, Any]
    models: List[ModelResult] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "contract": "analysis.health/1",
            "source": self.source,
            "vehicle_label": self.vehicle_label,
            "baseline_definition": self.definition,
            "eligibility": self.eligibility,
            "models": [m.as_dict() for m in self.models],
        }
