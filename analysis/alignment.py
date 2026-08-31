"""
The time-alignment contract: when two observations may be compared.

Every cross-channel metric in this project is a comparison between two
signals that were never sampled at the same instant. The channels are
polled on different schedules - the proprietary DDE reads share one
staggered round-robin, so a member refreshes only every ~11 s - and
nothing in the data says how far apart two values were when something
subtracts one from the other.

Unbounded nearest-value matching therefore produces a number for every
pair, however stale, and the number looks exactly like a measurement. A
boost actual minus a setpoint taken twelve seconds earlier is not control
error; it is mostly the engine having changed. A plausible graph built
from mismatched observations is worse than no graph, because nobody
checks a plausible graph.

This module makes the tolerance explicit, per pair, and reports how much
of the data actually satisfied it. A metric with 5% coverage is not a
metric, and the point of returning coverage alongside the value is that
the report can say so instead of printing a confident average.

Measured on the lake, 2026-08-31, with the SAME symmetric-nearest rule
this module implements:

    pair                        median gap   p10..p90       within 1 s
    boost actual / setpoint         0.56 s   0.52..0.59        98.9 %
    rail actual / setpoint          4.32 s                     (see below)
    DDE coolant / OBD coolant       2.75 s                     100 %

The boost pair is far better aligned than a backward-only match suggests:
measuring it with ClickHouse's one-directional ASOF reports a 12.3 s
median, because it can only ever look BACKWARD to the previous
round-robin visit. Looking both ways finds the setpoint 0.56 s away,
consistently - p10 to p90 spans 70 ms. That is a real property of the
schedule, and it is why this module and the lake queries must use the
same rule or they will disagree about the same data.

Windows are therefore set from measurement, not taste. A 0.5 s window for
boost sits just BELOW the actual 0.56 s separation and rejects 95% of the
data - an artifact of the threshold, not a finding about the car.

What the residual 0.56 s costs, measured against 10 Hz MAP as a proxy for
how fast manifold pressure really moves:

    change over 0.56 s:   median 0 hPa,  p90 140 hPa,  p99 672 hPa

So the pair is comparable at steady state and increasingly noisy under
hard transients, where the misalignment alone can inject more error than
the deviation being measured. Reported deviations are trustworthy in
aggregate; a single large excursion may be sampling, not the actuator.
Co-scheduling the pair would remove that residual entirely - see the
acquisition follow-up in `docs/ALIGNMENT.md`.
"""

import statistics
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

__all__ = [
    "Pairing",
    "PAIRINGS",
    "AlignmentResult",
    "align",
    "pairing_for",
    "MIN_USEFUL_COVERAGE",
]

Series = Sequence[Tuple[float, float]]

#: Below this share of matched samples, a comparison is reported as
#: unusable rather than averaged. Not a statistical threshold - a
#: judgement that a metric covering under half its own inputs is
#: describing the schedule more than the car.
MIN_USEFUL_COVERAGE = 50.0


class Pairing(NamedTuple):
    """How temporally compatible two channels must be to be compared."""

    max_age_s: float
    why: str


#: (channel a, channel b) -> tolerance. Deliberately per pair: one global
#: number would be wrong in both directions at once, too tight for
#: coolant and far too loose for a control loop.
PAIRINGS: Dict[Tuple[str, str], Pairing] = {
    ("n47d_boost_act", "n47d_boost_set"): Pairing(
        1.0,
        "Boost tracking is a control-loop error, and the turbo moves in "
        "hundreds of milliseconds - but the two channels are actually "
        "sampled 0.56 s apart (p10..p90 = 0.52..0.59), so 1.0 s captures "
        "98.9% of pairs while 0.5 s captures 4.6%. The tighter number "
        "would measure the threshold, not the car. The residual 0.56 s is "
        "worth up to ~140 hPa (p90) of spurious deviation under transients.",
    ),
    ("n47d_rail_act", "n47d_rail_set"): Pairing(
        1.0,
        "Same shape as boost: the fastest loop on the engine, but bounded "
        "by how close the schedule actually places the two reads.",
    ),
    ("n47d_coolant", "coolant"): Pairing(
        15.0,
        "A decode cross-check between two reads of the same physical "
        "sensor. Coolant changes by well under a degree in 15 s, so a "
        "wide window costs nothing and keeps the sample count useful.",
    ),
    ("n47d_ambient_press", "baro"): Pairing(
        60.0,
        "Ambient pressure is effectively constant over a minute except "
        "on a mountain pass. Both are polled rarely.",
    ),
    ("n47d_oil_temp", "coolant"): Pairing(
        15.0,
        "Warm-up lag between two slow thermal masses. The quantity of "
        "interest is minutes wide.",
    ),
    ("n47d_soot_meas", "n47d_soot_model"): Pairing(
        15.0,
        "Two ECU model outputs on the same slow round-robin. Neither "
        "changes quickly. (Both describe a filter this car does not "
        "have - see docs/DPF_SOOT.md.)",
    ),
}

#: Used when a pair has no declared tolerance. Deliberately strict: an
#: undeclared comparison should be visibly poor rather than quietly
#: permissive, so it gets a real window rather than inheriting a
#: convenient one.
DEFAULT_PAIRING = Pairing(
    1.0, "No declared tolerance for this pair; strict default applied."
)


def pairing_for(a: str, b: str) -> Pairing:
    """The declared tolerance for a pair, in either order."""
    return PAIRINGS.get((a, b)) or PAIRINGS.get((b, a)) or DEFAULT_PAIRING


class AlignmentResult(NamedTuple):
    """Matched pairs, plus how much of the input had to be thrown away."""

    pairs: List[Tuple[float, float, float]]   # (ts of a, a value, b value)
    attempted: int
    matched: int
    max_age_s: float
    median_gap_s: Optional[float]

    @property
    def coverage_pct(self) -> float:
        if not self.attempted:
            return 0.0

        return round(100.0 * self.matched / self.attempted, 1)

    @property
    def usable(self) -> bool:
        """Whether enough of the data was comparable to conclude anything."""
        return bool(self.matched) and self.coverage_pct >= MIN_USEFUL_COVERAGE


def align(a: Series, b: Series, max_age_s: float) -> AlignmentResult:
    """
    Pair each sample of `a` with the nearest `b` within `max_age_s`.

    Unmatched samples of `a` are counted, not silently dropped: the
    difference between "we compared everything" and "we compared one
    sample in twenty" is the whole point.

    `b` is scanned in order and the series are assumed sorted by time,
    which is how the recorder writes them and how `load_run` reads them.
    """
    pairs: List[Tuple[float, float, float]] = []
    gaps: List[float] = []
    index = 0
    b_len = len(b)

    for ts, av in a:
        #
        # Advance to the last b at or before ts, then consider that one
        # and its successor: the nearest sample is always one of the two.
        #
        while index + 1 < b_len and b[index + 1][0] <= ts:
            index += 1

        best_gap, best_value = None, None

        for candidate in (index, index + 1):
            if 0 <= candidate < b_len:
                gap = abs(b[candidate][0] - ts)

                if best_gap is None or gap < best_gap:
                    best_gap, best_value = gap, b[candidate][1]

        if best_gap is not None and best_gap <= max_age_s:
            pairs.append((ts, av, best_value))
            gaps.append(best_gap)

    #
    # statistics.median, not the upper-middle element: the gap is part of
    # the evidence a reader weighs, so it should be the conventional
    # statistic rather than a subtly different one.
    #
    median = round(statistics.median(gaps), 3) if gaps else None

    return AlignmentResult(
        pairs=pairs,
        attempted=len(a),
        matched=len(pairs),
        max_age_s=max_age_s,
        median_gap_s=median,
    )
