"""
The few statistics the health models need, written out so they can be
read. Everything here is deterministic, order-independent and stdlib.

Nothing parametric: a control residual is not normal (it has a hard
floor at ambient and a long tail under transients), and a per-trip
warm-up time is a handful of points. Medians, quantiles, the median
absolute deviation and a rank-sum comparison are all explainable to
someone looking at the raw samples, which is the standard the project
sets for a conclusion about the car.
"""

import math
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "quantile",
    "median",
    "mad",
    "describe",
    "rank_sum_z",
    "least_squares_slope",
    "first_crossing",
]


def quantile(values: Sequence[float], p: float) -> float:
    """
    Linear-interpolation quantile (type 7, what numpy and R default to).

    Sorted copy each call; the inputs here are hundreds of samples at
    most, and a shared sort would couple the callers for nothing.
    """
    if not values:
        raise ValueError("quantile of nothing")

    xs = sorted(values)
    k = (len(xs) - 1) * p
    lo = int(math.floor(k))
    hi = min(lo + 1, len(xs) - 1)

    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def median(values: Sequence[float]) -> float:
    return quantile(values, 0.5)


def mad(values: Sequence[float]) -> float:
    """Median absolute deviation from the median. Robust spread."""
    m = median(values)

    return median([abs(v - m) for v in values])


def describe(values: Sequence[float], digits: int = 2) -> Dict[str, float]:
    """The reference distribution a metric echoes back: robust summary."""
    return {
        "n": len(values),
        "median": round(median(values), digits),
        "p10": round(quantile(values, 0.10), digits),
        "p90": round(quantile(values, 0.90), digits),
        "mad": round(mad(values), digits),
        "min": round(min(values), digits),
        "max": round(max(values), digits),
    }


def rank_sum_z(baseline: Sequence[float],
               current: Sequence[float]) -> Tuple[float, float]:
    """
    Mann-Whitney / Wilcoxon rank-sum comparison of two samples.

    Returns (z, p_exceed): `z` is the normal-approximated statistic with
    tie correction, signed so that a POSITIVE z means the current sample
    tends to be larger than the baseline; `p_exceed` is the probability
    that a random current value exceeds a random baseline value (the
    common-language effect size, 0.5 = indistinguishable).

    Chosen because it needs no distributional assumption, it is
    insensitive to the transient tail that a residual carries, and its
    meaning survives translation to a sentence: "a current sample is
    larger than a baseline sample 81% of the time".

    The normal approximation is what the confidence rules gate on: the
    baseline definition's minimum sample counts exist so that the
    approximation is honest (rule of thumb: both sides above ~20).
    """
    n1, n2 = len(baseline), len(current)

    if n1 == 0 or n2 == 0:
        raise ValueError("rank sum needs both samples")

    tagged = [(v, 0) for v in baseline] + [(v, 1) for v in current]
    tagged.sort(key=lambda t: t[0])

    ranks = [0.0] * len(tagged)
    tie_term = 0.0
    i = 0

    while i < len(tagged):
        j = i

        while j + 1 < len(tagged) and tagged[j + 1][0] == tagged[i][0]:
            j += 1

        rank = (i + j + 2) / 2.0   # average of 1-based ranks i+1..j+1

        for k in range(i, j + 1):
            ranks[k] = rank

        size = j - i + 1

        if size > 1:
            tie_term += size ** 3 - size

        i = j + 1

    rank_sum_current = sum(r for r, (_v, side) in zip(ranks, tagged) if side)
    u_current = rank_sum_current - n2 * (n2 + 1) / 2.0
    p_exceed = u_current / (n1 * n2)

    n = n1 + n2
    mean_u = n1 * n2 / 2.0
    var_u = (n1 * n2 / 12.0) * ((n + 1) - tie_term / (n * (n - 1)))

    if var_u <= 0:
        #
        # Every value identical on both sides: no ranking information at
        # all. Zero evidence of a difference, reported as exactly that.
        #
        return 0.0, 0.5

    return (u_current - mean_u) / math.sqrt(var_u), p_exceed


def least_squares_slope(points: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Slope of y over x by ordinary least squares; None if degenerate."""
    n = len(points)

    if n < 2:
        return None

    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)

    if sxx == 0:
        return None

    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)

    return sxy / sxx


def first_crossing(series: Sequence[Tuple[float, float]],
                   threshold: float) -> Optional[float]:
    """
    Timestamp of the first sample at or above `threshold`.

    The FIRST crossing, not an interpolation: a thermal ramp sampled every
    ~12 s puts the true crossing anywhere inside the interval, and the
    sample time is the only thing actually observed. The interval width
    is the resolution, and the metric contract reports it.
    """
    for ts, value in series:
        if value >= threshold:
            return ts

    return None
