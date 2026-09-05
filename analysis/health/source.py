"""
The data-source seam: one row shape, two origins.

The health models consume a plain iterator of samples with the session
metadata they need, and nothing else. The recorder's SQLite file and the
ClickHouse lake both produce that shape - one through a read-only
connection here, the other through whatever moved the rows out of the
lake (a JSONEachRow export, a TSV, a client library) fed to `RowSource`.
The models cannot tell the difference, which is the point: a result
computed on the laptop from `local/telemetry.db` and one computed on the
VPS from the lake are the same code over the same contract, so a
disagreement between them is a data question, never a code question.

Channels are keyed by their RAW recorder key everywhere in this package
(`n47d_boost_act`, `coolant`, `rpm`). The lake also carries a normalized
name, and its map merges several raw keys into one (`coolant` and
`n47d_coolant` both become `engine.coolant_temperature`); a model that
compares a DDE read with an OBD read needs them apart, so the lake
adapter reads `channel_raw`, not `channel`.

`mapping_ver` rides on every row. Locally it comes from `run_channels`
(per run, per channel - the authoritative provenance) with the
`params.mapping_ver` fallback the recorder documents for rows recorded
before per-run provenance existed. In the lake it is the per-sample
column. Either way a model sees which revision decoded each value, which
is what lets it refuse to pool a channel across a decode change.
"""

import calendar
import json
import sqlite3
import time
from typing import (
    Any, Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence,
)

from analysis.trips import RunRow

__all__ = [
    "Row",
    "SessionMeta",
    "Source",
    "SqliteSource",
    "RowSource",
    "epoch",
]


class Row(NamedTuple):
    """One recorded sample. `quality` None = recorded before labelling."""

    ts: float
    channel: str
    value: float
    quality: Optional[str]
    mapping_ver: str


class SessionMeta(NamedTuple):
    """
    What a model needs to know about the run a row came from.

    A superset of `analysis.trips.RunRow` so trip grouping is the SAME
    function the rest of the project uses - see `as_run_row()`.
    """

    run_id: int
    session_uid: str
    started: float
    ended: Optional[float]
    boot_id: str
    mode: str
    vehicle_hardware: str
    vehicle_label: str
    clock_synced: Optional[int]
    last_sample_ts: Optional[float] = None
    #: The whole-run mapping fingerprint ("id@version,..."), for display.
    mapping_set: str = ""

    def as_run_row(self) -> RunRow:
        return RunRow(
            run_id=self.run_id,
            session_uid=self.session_uid,
            started=self.started,
            ended=self.ended,
            boot_id=self.boot_id,
            mode=self.mode,
            vehicle_hardware=self.vehicle_hardware,
            clock_synced=self.clock_synced,
            last_sample_ts=self.last_sample_ts,
        )


class Source:
    """The seam. Subclasses provide sessions and the rows of one run."""

    def describe(self) -> str:
        raise NotImplementedError

    def sessions(self) -> List[SessionMeta]:
        raise NotImplementedError

    def rows(self, run_id: int) -> Iterator[Row]:
        raise NotImplementedError


# ----------------------------------------------------------- recorder db


class SqliteSource(Source):
    """
    A recorder database, opened read-only. Never writes, never emits
    `runs.vin`.
    """

    def __init__(self, path: str):
        self.path = path

    def describe(self) -> str:
        return f"sqlite:{self.path}"

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set:
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}

    def sessions(self) -> List[SessionMeta]:
        db = self._connect()

        try:
            have = self._columns(db, "runs")

            def col(name: str, default: str = "''") -> str:
                return name if name in have else default

            rows = db.execute(
                "SELECT id, %s, started_at, ended_at, %s, %s, %s, %s, %s, %s "
                "FROM runs ORDER BY started_at, id" % (
                    col("session_uid"), col("boot_id"), col("mode"),
                    col("vehicle_hardware"), col("vehicle_label"),
                    col("clock_synced", "NULL"), col("mapping_set"),
                )
            ).fetchall()
            last = dict(db.execute(
                "SELECT run_id, MAX(ts) FROM samples GROUP BY run_id"
            ).fetchall())
        finally:
            db.close()

        return [
            SessionMeta(
                run_id=r[0], session_uid=r[1] or "", started=r[2],
                ended=r[3], boot_id=r[4] or "", mode=r[5] or "",
                vehicle_hardware=r[6] or "", vehicle_label=r[7] or "",
                clock_synced=r[8], last_sample_ts=last.get(r[0]),
                mapping_set=r[9] or "",
            )
            for r in rows
        ]

    def rows(self, run_id: int) -> Iterator[Row]:
        db = self._connect()

        try:
            quality = (
                "s.quality" if "quality" in self._columns(db, "samples")
                else "NULL"
            )
            #
            # run_channels is per run per channel and wins; params.mapping_ver
            # is the first-sight fallback the schema comment describes.
            # An empty run_channels string means "this run knew, and the
            # answer was nothing", so only an ABSENT row falls back.
            #
            versions = (
                "COALESCE(rc.mapping_version, p.mapping_ver, '')"
                if "run_channels" in {
                    r[0] for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                } else "COALESCE(p.mapping_ver, '')"
            )
            join = (
                "LEFT JOIN run_channels rc ON rc.run_id = s.run_id "
                "AND rc.param_id = s.param_id "
                if "rc." in versions else ""
            )
            cursor = db.execute(
                f"SELECT s.ts, p.key, s.value, {quality}, {versions} "
                f"FROM samples s JOIN params p ON p.id = s.param_id {join}"
                f"WHERE s.run_id = ? ORDER BY s.ts",
                (run_id,),
            )

            for ts, key, value, q, ver in cursor:
                yield Row(float(ts), key, float(value), q, str(ver or ""))
        finally:
            db.close()


# ------------------------------------------------------------- lake rows


def epoch(value: Any) -> float:
    """
    A timestamp as unix seconds, from the forms the lake and the recorder
    produce: a number, or a ClickHouse `YYYY-MM-DD HH:MM:SS[.fff]` UTC
    string.
    """
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = time.strptime(text.split(".")[0], fmt.split(".")[0])
        except ValueError:
            continue

        seconds = float(calendar.timegm(parsed))

        if "." in text:
            fraction = text.split(".")[1]
            digits = "".join(ch for ch in fraction if ch.isdigit())

            if digits:
                seconds += float("0." + digits)

        return seconds

    raise ValueError(f"cannot read {value!r} as a timestamp")


class RowSource(Source):
    """
    Sessions and rows already in memory - the lake adapter.

    `sessions` are dicts shaped like `telemetry.sessions` rows (or
    `SessionMeta`); `samples` are dicts shaped like `telemetry.samples`
    rows, keyed by `session_id` (or `run_id`). Column names follow the
    lake: `channel_raw` is preferred over `channel`, `ts` may be a
    ClickHouse datetime string. Anything the lake does not carry
    (`last_sample_ts`) is derived from the rows.

    The reader never sees a `vehicle_id`: it is not part of the row
    shape, so it cannot leak into a report.
    """

    def __init__(self, sessions: Sequence[Any], samples: Iterable[Any]):
        self._sessions: Dict[int, SessionMeta] = {}
        self._rows: Dict[int, List[Row]] = {}

        for raw in samples:
            run_id, row = self._row(raw)
            self._rows.setdefault(run_id, []).append(row)

        for rows in self._rows.values():
            rows.sort(key=lambda r: r.ts)

        for raw in sessions:
            meta = self._session(raw)
            self._sessions[meta.run_id] = meta

    def describe(self) -> str:
        return f"rows:{len(self._sessions)} sessions"

    def _session(self, raw: Any) -> SessionMeta:
        if isinstance(raw, SessionMeta):
            meta = raw
        else:
            run_id = int(raw.get("session_id", raw.get("run_id")))
            ended = raw.get("ended", raw.get("ended_at"))
            synced = raw.get("clock_synced")
            meta = SessionMeta(
                run_id=run_id,
                session_uid=str(raw.get("session_uid") or ""),
                started=epoch(raw.get("started", raw.get("started_at"))),
                ended=None if ended in (None, "") else epoch(ended),
                boot_id=str(raw.get("boot_id") or ""),
                mode=str(raw.get("mode") or ""),
                vehicle_hardware=str(raw.get("vehicle_hardware") or ""),
                vehicle_label=str(raw.get("vehicle_label") or ""),
                clock_synced=None if synced in (None, "") else int(synced),
                mapping_set=str(raw.get("mappings") or raw.get("mapping_set") or ""),
            )

        if meta.last_sample_ts is None and meta.run_id in self._rows:
            meta = meta._replace(last_sample_ts=self._rows[meta.run_id][-1].ts)

        return meta

    @staticmethod
    def _row(raw: Any):
        run_id = int(raw.get("session_id", raw.get("run_id")))
        quality = raw.get("quality")

        return run_id, Row(
            ts=epoch(raw["ts"]),
            channel=str(raw.get("channel_raw") or raw.get("channel")),
            value=float(raw["value"]),
            quality=None if quality in (None, "") else str(quality),
            mapping_ver=str(raw.get("mapping_ver") or ""),
        )

    def sessions(self) -> List[SessionMeta]:
        return sorted(self._sessions.values(),
                      key=lambda m: (m.started, m.run_id))

    def rows(self, run_id: int) -> Iterator[Row]:
        return iter(self._rows.get(run_id, []))

    @classmethod
    def from_json_lines(cls, sessions_path: str, samples_path: str) -> "RowSource":
        """
        Build from two ClickHouse `FORMAT JSONEachRow` exports:

            SELECT session_id, started, ended, mode, clock_synced, mappings,
                   vehicle_label, vehicle_hardware, session_uid, boot_id
            FROM telemetry.sessions WHERE vehicle_id = {vin:String}

            SELECT session_id, ts, channel_raw, value, quality, mapping_ver
            FROM telemetry.samples WHERE vehicle_id = {vin:String}

        Neither export needs to carry `vehicle_id`, and this reader
        ignores it if it does.
        """
        with open(sessions_path) as fh:
            sessions = [json.loads(line) for line in fh if line.strip()]

        def samples() -> Iterator[Dict[str, Any]]:
            with open(samples_path) as fh:
                for line in fh:
                    if line.strip():
                        yield json.loads(line)

        return cls(sessions, samples())
