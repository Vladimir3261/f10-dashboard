"""
Physical trips, above acquisition runs.

A run is one HSFZ connection. A drive is what the car did. They are not
the same thing and never were: a mode change ends a run deliberately, a
clock step ends one, and a dropped link ends one by accident. Drive 11
recorded as four runs. Any longitudinal question - "how did this drive
compare with that one" - asks about trips, and the storage layer has only
ever offered runs.

Grouping is deliberately a PURE FUNCTION over recorded evidence rather
than something stamped at record time. Two reasons:

  * the evidence improves. Adding ignition state or odometer later should
    re-group the history, not only new drives;
  * a boundary someone disagrees with has to be arguable. Every boundary
    here carries the reason it was drawn, so the answer to "why are these
    two separate drives?" is a field, not an archaeology exercise.

Deterministic: the same runs in, the same trips out, including the trip
identifiers, which are derived from the first run rather than minted.

TIME REASONING IS GATED, as everywhere else here. A gap between two runs
is a timestamp difference, so on a run whose clock was not disciplined it
is not evidence of anything. Rather than guess, such a run starts its own
trip and says so - see docs/ALIGNMENT.md for why unknown fails closed.
"""

import os
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

__all__ = [
    "RunRow",
    "Trip",
    "MAX_TRIP_GAP_S",
    "group_trips",
    "load_runs",
]

#: Two runs closer together than this, with nothing else disagreeing, are
#: the same drive. Sized for the real cause: a dropped link reconnects in
#: seconds and a mode switch is immediate, while a genuine stop - fuel,
#: shops, home - is minutes at least. Drive 11's four runs had gaps of
#: 5.4 s, 8.6 s and 5.5 s; the drives either side of it were hours apart.
MAX_TRIP_GAP_S = 300.0


class RunRow(NamedTuple):
    """Everything grouping needs from one run, and nothing else."""

    run_id: int
    session_uid: str
    started: float
    ended: Optional[float]
    boot_id: str
    mode: str
    vehicle_hardware: str
    clock_synced: Optional[int]

    @property
    def finished_at(self) -> float:
        """
        When this run stopped producing data.

        `ended` is NULL when the process was killed rather than closed,
        which is common enough that treating it as "still running" would
        merge every subsequent drive into one. Falling back to `started`
        makes the gap look LARGER, which splits rather than merges - the
        safer direction when the evidence is missing.
        """
        return self.started if self.ended is None else self.ended


class Trip(NamedTuple):
    """One physical drive: an ordered group of runs, and why it starts."""

    trip_uid: str
    runs: Tuple[RunRow, ...]
    reason: str

    @property
    def started(self) -> float:
        return self.runs[0].started

    @property
    def ended(self) -> float:
        return self.runs[-1].finished_at

    @property
    def duration_s(self) -> float:
        return self.ended - self.started

    def describe(self) -> str:
        return (
            f"trip {self.trip_uid[:10]}… {len(self.runs)} run(s), "
            f"{self.duration_s / 60.0:.1f} min ({self.reason})"
        )


def _boundary_reason(prev: RunRow, run: RunRow) -> Optional[str]:
    """
    Why `run` begins a new trip after `prev`, or None to continue it.

    Ordered most-certain first, so the reason reported is the strongest
    one rather than whichever happened to be checked first.
    """
    #
    # Different boots cannot be one drive. The car was switched off hard
    # enough to power-cycle the recorder, which is a stronger statement
    # than any gap, and it does not depend on the clock being right.
    #
    if prev.boot_id and run.boot_id and prev.boot_id != run.boot_id:
        return "different host boot"

    #
    # Hardware changed between them. Whatever this is, it is not one
    # drive, and a baseline must not span it.
    #
    if (prev.vehicle_hardware and run.vehicle_hardware
            and prev.vehicle_hardware != run.vehicle_hardware):
        return "vehicle configuration changed"

    #
    # Time reasoning needs a disciplined clock on BOTH sides. The Pi has
    # no RTC and once stepped 76.5 minutes mid-recording; a gap measured
    # across that says nothing. Split rather than assume.
    #
    if prev.clock_synced != 1 or run.clock_synced != 1:
        return "clock not disciplined - gap not evidence"

    gap = run.started - prev.finished_at

    if gap > MAX_TRIP_GAP_S:
        return f"gap of {gap:.0f}s"

    #
    # Negative gap: this run started before the previous one finished.
    # Overlapping runs are not a thing the recorder produces, so the
    # timestamps are wrong even though both runs claim a good clock.
    #
    if gap < 0:
        return f"overlapping runs ({gap:.0f}s) - timestamps disagree"

    return None


def group_trips(runs: Sequence[RunRow]) -> List[Trip]:
    """
    Group runs into physical trips. Pure, deterministic, order-independent.

    Runs are sorted by start time first, so the caller's ordering cannot
    change the answer.
    """
    ordered = sorted(runs, key=lambda r: (r.started, r.run_id))
    trips: List[Trip] = []
    current: List[RunRow] = []
    reason = "first run"

    for run in ordered:
        if not current:
            current, reason = [run], reason
            continue

        boundary = _boundary_reason(current[-1], run)

        if boundary is None:
            current.append(run)
            continue

        trips.append(_finish(current, reason))
        current, reason = [run], boundary

    if current:
        trips.append(_finish(current, reason))

    return trips


def _finish(runs: List[RunRow], reason: str) -> Trip:
    #
    # Identity is DERIVED from the first run rather than minted, so
    # re-grouping the same data produces the same trip ids. A minted id
    # would make every re-analysis look like new trips.
    #
    first = runs[0]
    uid = first.session_uid or f"run{first.run_id}@{first.started:.0f}"

    return Trip(trip_uid=uid, runs=tuple(runs), reason=reason)


def load_runs(db_path: str) -> List[RunRow]:
    """Read the grouping evidence out of a recorder database, read-only."""
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(runs)")}

        def col(name: str) -> str:
            return name if name in have else "''"

        rows = con.execute(
            "SELECT id, %s, started_at, ended_at, %s, %s, %s, %s FROM runs "
            "ORDER BY started_at, id" % (
                col("session_uid"), col("boot_id"), col("mode"),
                col("vehicle_hardware"),
                "clock_synced" if "clock_synced" in have else "NULL",
            )
        ).fetchall()
    finally:
        con.close()

    return [
        RunRow(run_id=r[0], session_uid=r[1] or "", started=r[2], ended=r[3],
               boot_id=r[4] or "", mode=r[5] or "",
               vehicle_hardware=r[6] or "", clock_synced=r[7])
        for r in rows
    ]


def main() -> int:
    """Inspector: show how a database's runs group into trips, and why."""
    import argparse

    ap = argparse.ArgumentParser(description="group runs into physical trips")
    ap.add_argument("--db", required=True)
    args = ap.parse_args()

    runs = load_runs(args.db)
    trips = group_trips(runs)

    print(f"{len(runs)} run(s) -> {len(trips)} trip(s)\n")

    for trip in trips:
        print(trip.describe())

        for run in trip.runs:
            print(f"    run {run.run_id:>4}  "
                  f"{(run.finished_at - run.started) / 60.0:>6.1f} min  "
                  f"mode={run.mode or '?'}  clock={run.clock_synced}  "
                  f"uid={run.session_uid[:10] or '-'}")

        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
