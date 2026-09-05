"""
What is allowed in: the eligibility filters, applied once, and counted.

The filters are the contract the rest of the project already enforces,
reused rather than restated:

  * clock trust      - every run in a trip has `clock_synced = 1`.
                       Trip grouping (analysis/trips.py) already refuses
                       to reason across an undisciplined clock, so an
                       untrusted run is always a trip of its own; here it
                       is dropped and listed with the reason;
  * quality          - only samples labelled `ok` are measurements. A
                       sentinel, a saturated byte and a clipped value
                       keep their number in the database and lose it
                       here, counted per label so coverage can say what
                       was left out. Samples recorded before labelling
                       existed carry NULL; they are used (the decoder of
                       the day accepted them) and counted separately as
                       `unlabelled`, matching analysis/session_report.py;
  * trips            - the unit of longitudinal comparison is the physical
                       drive, from `group_trips`, never the acquisition
                       run.

Nothing here decides anything about the car. It decides what the models
are permitted to look at, and it keeps the receipts.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from analysis.trips import Trip, group_trips
from analysis.health.source import Row, SessionMeta, Source

__all__ = [
    "QUALITY_USABLE",
    "QUALITY_FILTER_TEXT",
    "TripData",
    "Eligibility",
    "load_eligible",
]

#: The only label that means "this number is a measurement".
QUALITY_USABLE = ("ok",)

#: What a metric echoes back under `quality_filters`.
QUALITY_FILTER_TEXT = [
    "sessions.clock_synced = 1 (every run of the trip)",
    "samples.quality = 'ok' (NULL = pre-labelling, used and counted)",
    "trips from analysis.trips.group_trips (physical drives, not runs)",
]

Series = List[Tuple[float, float]]


@dataclass
class TripData:
    """One eligible trip: its usable samples per channel, and the receipts."""

    trip: Trip
    series: Dict[str, Series] = field(default_factory=dict)
    #: Mapping versions seen per channel across the trip's runs.
    versions: Dict[str, Set[str]] = field(default_factory=dict)
    #: Rows removed by quality, per label.
    excluded: Dict[str, int] = field(default_factory=dict)
    unlabelled: int = 0
    seen: int = 0
    modes: Set[str] = field(default_factory=set)

    @property
    def uid(self) -> str:
        return self.trip.trip_uid

    @property
    def started(self) -> float:
        return self.trip.started

    @property
    def ended(self) -> float:
        return self.trip.ended

    def first_present(self, candidates: Sequence[str]) -> Optional[str]:
        """The first of `candidates` this trip has usable samples for."""
        for key in candidates:
            if self.series.get(key):
                return key

        return None

    def version_of(self, channel: str) -> str:
        """
        The single mapping version of a channel in this trip, "" when
        unknown, or "mixed:<a>,<b>" when the trip's runs disagree.
        """
        versions = sorted(v for v in self.versions.get(channel, set()))

        if not versions:
            return ""

        if len(versions) == 1:
            return versions[0]

        return "mixed:" + ",".join(versions)


@dataclass
class Eligibility:
    """Everything that was considered and what happened to it."""

    trips: List[TripData]
    sessions_total: int
    sessions_clock_synced: int
    trips_total: int
    excluded_trips: List[Dict[str, Any]]
    vehicle_label: str
    modes: Set[str] = field(default_factory=set)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sessions_total": self.sessions_total,
            "sessions_clock_synced": self.sessions_clock_synced,
            "trips_total": self.trips_total,
            "trips_eligible": len(self.trips),
            "excluded_trips": list(self.excluded_trips),
            "quality_filters": list(QUALITY_FILTER_TEXT),
            "drive_modes": sorted(self.modes),
            "samples_seen": sum(t.seen for t in self.trips),
            "samples_excluded_by_quality": _merge(t.excluded for t in self.trips),
            "samples_unlabelled": sum(t.unlabelled for t in self.trips),
        }


def _merge(dicts) -> Dict[str, int]:
    out: Dict[str, int] = {}

    for d in dicts:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v

    return out


def load_eligible(source: Source) -> Eligibility:
    """
    Read every session, group into trips, keep the ones the filters allow.

    Rows are read once per eligible run and never for an excluded one.
    """
    sessions: List[SessionMeta] = source.sessions()
    by_run = {s.run_id: s for s in sessions}
    trips = group_trips([s.as_run_row() for s in sessions])
    eligible: List[TripData] = []
    excluded: List[Dict[str, Any]] = []
    label = ""

    for trip in trips:
        untrusted = [r.run_id for r in trip.runs if r.clock_synced != 1]

        if untrusted:
            excluded.append({
                "trip_uid": trip.trip_uid,
                "runs": [r.run_id for r in trip.runs],
                "reason": "clock not disciplined (clock_synced != 1) on run(s) "
                          + ", ".join(str(r) for r in untrusted),
            })
            continue

        data = TripData(trip=trip)

        for run in trip.runs:
            meta = by_run[run.run_id]
            label = label or meta.vehicle_label
            data.modes.add(meta.mode)

            for row in source.rows(run.run_id):
                _take(data, row)

        for series in data.series.values():
            series.sort(key=lambda p: p[0])

        if not data.series:
            excluded.append({
                "trip_uid": trip.trip_uid,
                "runs": [r.run_id for r in trip.runs],
                "reason": "no usable samples",
            })
            continue

        eligible.append(data)

    return Eligibility(
        trips=eligible,
        sessions_total=len(sessions),
        sessions_clock_synced=sum(1 for s in sessions if s.clock_synced == 1),
        trips_total=len(trips),
        excluded_trips=excluded,
        vehicle_label=label,
        modes={m for t in eligible for m in t.modes},
    )


def _take(data: TripData, row: Row) -> None:
    data.seen += 1

    if row.quality is None:
        data.unlabelled += 1
    elif row.quality not in QUALITY_USABLE:
        data.excluded[row.quality] = data.excluded.get(row.quality, 0) + 1
        return

    data.series.setdefault(row.channel, []).append((row.ts, row.value))
    data.versions.setdefault(row.channel, set()).add(row.mapping_ver)
