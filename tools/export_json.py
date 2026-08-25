#!/usr/bin/env python3
"""
export_json.py - dump telemetry.db to self-describing JSON for analysis.

Writes two things, both under the gitignored local/ area:

  local/exports/telemetry_export.json
                          index, channel dictionary, per-run statistics
                          and a downsampled series for every run
  local/exports/telemetry_json/run_N.json
                          full-resolution samples, one file per run

An agent should read telemetry_export.json first: it carries the units,
the decoding provenance and the per-run summary, and points at the
full-resolution file for whichever run is worth a closer look.

Run it from the repository root:

    python3 tools/export_json.py
    python3 tools/export_json.py --db telemetry.db --points 1500
    python3 tools/export_json.py --min-seconds 30   # skip trivial test runs
"""

import argparse
import json
import os
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

VEHICLE_NOTE = (
    "BMW F10 520d (N47 diesel, ZF 8HP automatic). Values are read with "
    "OBD-2 service 01 over BMW ENET/HSFZ from the DDE engine ECU. Decoding "
    "follows SAE J1979, so scaling is standard rather than reverse-engineered."
)

#
# Domain caveats that are not visible from the numbers alone. These cost
# real measurement time to establish, so they travel with the data.
#
ANALYSIS_HINTS = [
    "boost is derived: (intake manifold pressure - barometric) / 100, in bar "
    "gauge. It is not a directly reported PID.",
    "throttle (PID 0x11) reports the throttle VALVE and sits near 85% almost "
    "always on this diesel. Use pedal (PID 0x49) for driver demand.",
    "Fast channels are sampled ~10x more often than slow ones. Do not assume "
    "a shared time base; each channel carries its own timestamps.",
    "Every row is one real ECU reading - cached values are never re-logged, "
    "so gaps in a channel mean it was not polled, not that it was constant.",
    "speed == 0 for a whole run means the car was stationary (idle capture).",
    "lambda saturates at 2.0 (raw 0xFFFF) when the ECU reports no value; "
    "treat exactly 2.0 as missing rather than a real reading.",
    "Runs shorter than ~30s are development test captures, not measurements.",
    "Fuel level, oil temp, engine fuel rate and actual torque are NOT "
    "available: this DDE does not advertise PIDs 0x2F, 0x5C, 0x5E or 0x62.",
]

CHANNEL_DOCS = {
    "rpm": "Crankshaft speed.",
    "map": "Intake manifold absolute pressure (includes atmospheric).",
    "boost": "Derived turbocharger gauge pressure above ambient.",
    "load": "Calculated engine load.",
    "throttle": "Throttle valve position - near-constant on a diesel.",
    "pedal": "Accelerator pedal position - actual driver demand.",
    "relthr": "Relative throttle position.",
    "speed": "Vehicle road speed.",
    "maf": "Mass air flow into the engine.",
    "rail": "Common-rail fuel pressure.",
    "lambda": "Air/fuel equivalence ratio from the O2 sensor.",
    "coolant": "Engine coolant temperature.",
    "iat": "Intake air temperature after the intercooler.",
    "ambient": "Outside air temperature.",
    "cattemp": "Exhaust/catalyst temperature, bank 1 sensor 1.",
    "egr": "Commanded EGR valve position.",
    "egrerr": "EGR position error (commanded vs actual).",
    "voltage": "Electrical system voltage at the ECU.",
    "baro": "Barometric (atmospheric) pressure.",
    "runtime": "Seconds since the engine last started.",
    "distance": "Distance travelled since fault codes were cleared.",
    "torque": "Actual engine torque as a percentage of reference.",
    "fuel": "Fuel tank level.",
    "fuel_l": "Derived litres remaining, from fuel level and tank size.",
    "oil": "Engine oil temperature.",
    "fuelrate": "Instantaneous fuel consumption.",
}

DERIVED = {"boost", "fuel_l"}


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None

    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


def downsample(times: List[float], values: List[float], target: int):
    """Bucket-average to at most `target` points, preserving shape."""
    n = len(times)

    if n <= target:
        return times, values, 1

    span = times[-1] - times[0]

    if span <= 0:
        return times[:target], values[:target], max(1, n // target)

    bucket = span / target
    out_t: List[float] = []
    out_v: List[float] = []
    cur = int((times[0] - times[0]) / bucket)
    acc_t: List[float] = []
    acc_v: List[float] = []

    for t, v in zip(times, values):
        b = int((t - times[0]) / bucket)

        if b != cur and acc_t:
            out_t.append(sum(acc_t) / len(acc_t))
            out_v.append(sum(acc_v) / len(acc_v))
            acc_t, acc_v = [], []
            cur = b

        acc_t.append(t)
        acc_v.append(v)

    if acc_t:
        out_t.append(sum(acc_t) / len(acc_t))
        out_v.append(sum(acc_v) / len(acc_v))

    return out_t, out_v, round(bucket, 4)


def stats_for(values: List[float]) -> Dict:
    s = {
        "n": len(values),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "mean": round(sum(values) / len(values), 3),
        "first": round(values[0], 3),
        "last": round(values[-1], 3),
    }

    if len(values) > 1:
        s["stdev"] = round(statistics.pstdev(values), 3)
        s["median"] = round(statistics.median(values), 3)

    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="telemetry.db")
    ap.add_argument("--out",
                    default="local/exports/telemetry_export.json")
    ap.add_argument("--dir", default="local/exports/telemetry_json")
    ap.add_argument("--points", type=int, default=800,
                    help="max points per channel in the summary (default 800)")
    ap.add_argument("--min-seconds", type=float, default=0.0,
                    help="skip runs shorter than this")
    ap.add_argument("--no-full", action="store_true",
                    help="only write the summary file")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"{args.db} not found")

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    params = {
        pid: {"key": key, "pid": pid_num, "label": label, "unit": unit}
        for pid, key, pid_num, label, unit in db.execute(
            "SELECT id, key, pid, label, unit FROM params"
        )
    }

    channels = {}

    for p in params.values():
        channels[p["key"]] = {
            "label": p["label"],
            "unit": p["unit"],
            "obd_pid": f"0x{p['pid']:02X}" if p["pid"] is not None else None,
            "derived": p["key"] in DERIVED,
            "description": CHANNEL_DOCS.get(p["key"], ""),
        }

    os.makedirs(args.dir, exist_ok=True)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    runs_out = []
    skipped = 0

    for (rid, started, ended, vin, gateway, ecu, ecu_addr) in db.execute(
        "SELECT id, started_at, ended_at, vin, gateway, ecu, ecu_addr "
        "FROM runs ORDER BY id"
    ):
        rows = db.execute(
            "SELECT p.key, s.ts, s.value FROM samples s "
            "JOIN params p ON p.id = s.param_id "
            "WHERE s.run_id = ? ORDER BY s.ts", (rid,)
        ).fetchall()

        if not rows:
            skipped += 1
            continue

        by_key: Dict[str, List] = {}

        for key, ts, value in rows:
            by_key.setdefault(key, []).append((ts, value))

        t0 = min(v[0][0] for v in by_key.values())
        t1 = max(v[-1][0] for v in by_key.values())
        duration = t1 - t0

        if duration < args.min_seconds:
            skipped += 1
            continue

        ch_stats = {}
        ch_series = {}
        full_series = {}

        for key, pairs in sorted(by_key.items()):
            times = [round(t - t0, 3) for t, _ in pairs]
            values = [v for _, v in pairs]

            ch_stats[key] = stats_for(values)
            ch_stats[key]["rate_hz"] = (
                round(len(values) / duration, 2) if duration > 0 else None
            )

            dt, dv, bucket = downsample(times, values, args.points)

            ch_series[key] = {
                "t_rel_s": [round(x, 2) for x in dt],
                "value": [round(x, 3) for x in dv],
                "downsampled": len(dt) < len(times),
                "bucket_s": bucket,
                "original_n": len(times),
            }

            full_series[key] = {
                "t_rel_s": times,
                "value": [round(x, 3) for x in values],
            }

        run = {
            "id": rid,
            "started_unix": round(started, 3),
            "started_iso": iso(started),
            "ended_iso": iso(ended),
            "duration_s": round(duration, 1),
            "vin": vin,
            "gateway_ip": gateway,
            "ecu": ecu,
            "ecu_address": f"0x{ecu_addr:02X}" if ecu_addr is not None else None,
            "sample_count": len(rows),
            "channel_count": len(by_key),
            "moving": max(
                (v for k, s in ch_stats.items() if k == "speed"
                 for v in [s["max"]]), default=0
            ) > 0,
            "channels": ch_stats,
            "series": ch_series,
        }

        if not args.no_full:
            path = os.path.join(args.dir, f"run_{rid:03d}.json")

            with open(path, "w") as fh:
                json.dump(
                    {"run": {k: v for k, v in run.items() if k != "series"},
                     "series": full_series},
                    fh, separators=(",", ":"),
                )

            run["full_resolution_file"] = path

        runs_out.append(run)

    events = [
        {"run_id": r, "ts_iso": iso(t), "kind": k, "message": m}
        for r, t, k, m in db.execute(
            "SELECT run_id, ts, kind, message FROM events ORDER BY ts"
        )
    ]

    db.close()

    doc = {
        "schema_version": 1,
        "generated_at": iso(time.time()),
        "source_database": os.path.abspath(args.db),
        "vehicle": VEHICLE_NOTE,
        "structure": {
            "channels": "key -> label, unit, originating OBD PID, description",
            "runs[].channels": "per-channel statistics for that run",
            "runs[].series": "downsampled time series; t_rel_s is seconds "
                             "since that run's first sample",
            "runs[].full_resolution_file": "path to every sample for that run",
        },
        "analysis_hints": ANALYSIS_HINTS,
        "channels": channels,
        "run_count": len(runs_out),
        "runs_skipped": skipped,
        "runs": runs_out,
        "events": events,
    }

    with open(args.out, "w") as fh:
        json.dump(doc, fh, indent=1)

    size = os.path.getsize(args.out) / 1e6
    print(f"[+] {args.out}  ({size:.1f} MB, {len(runs_out)} runs, "
          f"{len(channels)} channels, {skipped} skipped)")

    if not args.no_full:
        tot = sum(
            os.path.getsize(os.path.join(args.dir, f))
            for f in os.listdir(args.dir)
        ) / 1e6
        print(f"[+] {args.dir}/  ({len(os.listdir(args.dir))} files, {tot:.1f} MB)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
