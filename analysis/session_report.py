#!/usr/bin/env python3
"""
session_report.py - cold-start + drive analysis of one recorded run.

    python3 -m analysis.session_report --db local/sessions/foo.db
    python3 -m analysis.session_report --db foo.db --run 7 --out validation-runs

Read-only on the database. VIN is never emitted. Produces, under
`--out/<timestamp>-session/`:
    report.md   human-readable: warm-up, cross-checks, load behaviour, DPF,
                data quality, per-channel coverage
    summary.json machine copy of the computed metrics
    curves.html self-contained SVG plots (warm-up + drive), no dependencies

This is roadmap Stage 3's quick win: descriptive stats + the first
proprietary-vs-OBD cross-checks over a whole session, from data the
runtime already records. It makes no baseline claims across sessions yet.
"""

import argparse
import json
import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from analysis.alignment import MIN_USEFUL_COVERAGE, align, pairing_for

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Cross-check pairs: (proprietary key, OBD key, scale to apply to OBD to
# match the proprietary unit, label). The DDE dynamic reads and the
# standard SAE PIDs measure the same physical quantity two independent
# ways; agreement validates the decode path live, per quantity.
CROSSCHECKS = [
    ("n47d_coolant", "coolant", 1.0, "coolant °C"),
    ("n47d_boost_act", "map", 10.0, "manifold/boost (hPa vs kPa×10)"),
    ("n47d_ambient_press", "baro", 10.0, "ambient (hPa vs kPa×10)"),
]

# (actual, setpoint, label) - deviation is a health signal.
SETPOINT_PAIRS = [
    ("n47d_boost_act", "n47d_boost_set", "boost"),
    ("n47d_rail_act", "n47d_rail_set", "rail pressure"),
]

DRIVING_SPEED = 3.0   # km/h; above this the car is moving
WARM_C = 80.0         # coolant target for warm-up timing


# ----------------------------------------------------------- loading


def _has_column(db, table: str, column: str) -> bool:
    return any(r[1] == column for r in db.execute(f"PRAGMA table_info({table})"))


def _has_table(db, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def load_run(db_path: str, run_id: Optional[int],
             include_flagged: bool = False) -> Dict:
    """
    Pull one run's series out of the DB, read-only. Never returns VIN.

    **Non-OK samples are excluded by default.** A sentinel the ECU
    returned to mean "no value", a sensor pinned on its rail and a real
    reading are all numbers, and averaging them together is how a health
    model learns something false. They are counted (see `flagged_counts`)
    so the report can say what it left out, and `include_flagged=True`
    puts them back for anyone deliberately studying them.

    Databases recorded before quality existed have no column, or NULL in
    it. Those are treated as OK - which claims only "the decoder of the
    day accepted this", not that the value is verified good. See
    docs/DATA_QUALITY.md.
    """
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    try:
        if run_id is None:
            row = db.execute("SELECT MAX(id) FROM runs").fetchone()
            run_id = row[0] if row else None

        if run_id is None:
            raise SystemExit("no runs in this database")

        #
        # clock_synced gates every time-derived number in this report.
        # The Pi has no RTC and once corrected itself 76.5 min mid-run;
        # a warm-up gradient or an alignment window computed across that
        # is meaningless. NULL means the run predates the flag - unknown,
        # not good.
        #
        clock_col = "clock_synced" if _has_column(db, "runs", "clock_synced") \
            else "NULL"
        meta = db.execute(
            f"SELECT started_at, ended_at, ecu, ecu_addr, {clock_col} "
            "FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()

        if meta is None:
            raise SystemExit(f"no run {run_id}")

        qual = (
            "COALESCE(s.quality, 'ok')" if _has_column(db, "samples", "quality")
            else "'ok'"
        )
        rows = db.execute(
            f"SELECT p.key, s.ts, s.value, {qual} FROM samples s "
            "JOIN params p ON p.id = s.param_id WHERE s.run_id=? ORDER BY s.ts",
            (run_id,),
        ).fetchall()

        #
        # Unit comes from THIS run's snapshot where there is one. `params`
        # is first-seen channel identity, so p.unit is whatever was loaded
        # the first time this database ever saw the channel - a mapping
        # that later corrects a unit would otherwise make an old report
        # display the wrong one. Same join and same fallback as the sync
        # agent uses; see docs/DATA_VERSIONING.md.
        #
        if _has_table(db, "run_channels"):
            params = db.execute(
                "SELECT p.key, COALESCE(rc.unit, p.unit), p.pid FROM params p "
                "LEFT JOIN run_channels rc "
                "  ON rc.param_id = p.id AND rc.run_id = ? "
                "WHERE p.id IN "
                "  (SELECT DISTINCT param_id FROM samples WHERE run_id=?)",
                (run_id, run_id),
            ).fetchall()
        else:
            params = db.execute(
                "SELECT p.key, p.unit, p.pid FROM params p "
                "WHERE p.id IN "
                "  (SELECT DISTINCT param_id FROM samples WHERE run_id=?)",
                (run_id,),
            ).fetchall()
    finally:
        db.close()

    series: Dict[str, List[Tuple[float, float]]] = {}
    flagged_counts: Dict[str, Dict[str, int]] = {}

    for key, ts, value, quality in rows:
        if quality != "ok":
            counts = flagged_counts.setdefault(key, {})
            counts[quality] = counts.get(quality, 0) + 1

            if not include_flagged:
                continue

        series.setdefault(key, []).append((ts, value))

    started, ended, ecu, ecu_addr, clock_synced = meta

    return {
        "run_id": run_id,
        "started": started,
        "ended": ended,
        "ecu": ecu,                       # NOT the VIN
        "ecu_addr": ecu_addr,
        #: 1 trustworthy, 0 not, None recorded before this was tracked.
        "clock_synced": clock_synced,
        "series": series,
        #: key -> {label: count} for everything excluded (or, with
        #: include_flagged, everything that WOULD have been).
        "flagged_counts": flagged_counts,
        "include_flagged": include_flagged,
        "units": {k: u for k, u, _ in params},
        "is_obd": {k: (pid is not None) for k, _, pid in params},
    }


# ----------------------------------------------------------- helpers


def _nearest(series: List[Tuple[float, float]], t: float,
             max_age_s: float) -> Optional[float]:
    """
    The value nearest `t`, or None if the nearest one is too old.

    `max_age_s` is mandatory. This used to be unbounded and returned the
    closest sample however far away it was, which meant every caller got
    a number and none of them could tell whether it meant anything. On
    the staggered DDE class "however far away" is routinely twelve
    seconds. See analysis/alignment.py.
    """
    if not series:
        return None

    best, bd = None, max_age_s

    for ts, v in series:
        d = abs(ts - t)

        if d <= bd:
            bd, best = d, v

    return best


def paired(a: List[Tuple[float, float]], b: List[Tuple[float, float]],
           tol: float = 5.0) -> List[Tuple[float, float, float]]:
    """
    (timestamp, a, b) for samples taken close enough together to compare.

    `_nearest` has no tolerance: it returns the closest sample however far
    away it is. For a one-instant comparison during a warm-up that is a
    real problem - coolant is polled every 10 s and the DDE oil read comes
    round every ~11 s, so an unbounded pairing can be 5 s apart while the
    quantity being measured moves by about as much as the difference being
    claimed. A single-crossing delta is then noise reported as a finding.

    Pairing with a tolerance, and averaging over the whole warm-up, is
    what actually answers "does oil lag coolant".

    Kept as a thin wrapper so there is exactly one matching
    implementation; `align` additionally reports how much was rejected.
    """
    return align(a, b, tol).pairs


def _stats(values: List[float]) -> Dict:
    if not values:
        return {}

    s = sorted(values)
    n = len(s)
    mean = sum(s) / n

    def pct(p):
        return s[min(n - 1, int(p * n))]

    return {
        "n": n, "min": round(s[0], 3), "max": round(s[-1], 3),
        "mean": round(mean, 3), "p50": round(pct(0.5), 3),
        "p95": round(pct(0.95), 3),
    }


def elapsed(series, t0):
    return [(ts - t0, v) for ts, v in series]


# ----------------------------------------------------------- analyses


def warmup(run: Dict) -> Dict:
    """Coolant/oil rise from start; time to warm; oil-lags-coolant."""
    t0 = run["started"]
    out: Dict = {}

    for key in ("coolant", "n47d_oil_temp", "n47d_engine_temp",
                "n47d_charge_air_temp"):
        s = run["series"].get(key)

        if not s:
            continue

        e = elapsed(s, t0)
        start_v = e[0][1]
        end_v = e[-1][1]
        warmed = next((t for t, v in e if v >= WARM_C), None)
        out[key] = {
            "start": round(start_v, 1), "end": round(end_v, 1),
            "min": round(min(v for _, v in e), 1),
            "max": round(max(v for _, v in e), 1),
            "seconds_to_80C": round(warmed, 1) if warmed is not None else None,
            "unit": run["units"].get(key, "°C"),
        }

    #
    # Oil vs coolant at the moment coolant reaches 80 C.
    #
    # This used to assert "oil lags coolant - the expected warm-up
    # signature" unconditionally, without looking. On the first genuine
    # cold start (2026-08-31) oil ran 0.27 C ABOVE coolant and the report
    # said it lagged anyway - a confident wrong answer, which is the
    # failure mode this project is least able to afford.
    #
    # The lag is real but LOAD-driven: oil takes heat from work done. At
    # idle there is no work, so oil heats from the block and tracks
    # coolant or sits slightly above it. Whether a lag should be expected
    # therefore depends on whether the car moved, so record that too and
    # let the prose decide rather than assuming.
    #
    cool = run["series"].get("coolant")
    oil = run["series"].get("n47d_oil_temp")

    if cool and oil:
        warmed_t = next((ts for ts, v in cool if v >= WARM_C), None)

        if warmed_t is not None:
            speed = run["series"].get("speed") or []
            moved = max((v for _, v in speed), default=None)

            #
            # Averaged over MATCHED PAIRS across the warm-up, not read off
            # one sample at the crossing. A single instant is within the
            # sampling error: on session 9 the same session gave -0.2,
            # +0.27 or +0.50 C depending purely on how the two series were
            # lined up. The mean over the ramp is the number that means
            # something.
            #
            ramp = [(ts, v) for ts, v in cool if ts <= warmed_t]
            pairs = paired(ramp, oil)
            deltas = [ov - cv for _, cv, ov in pairs]

            out["oil_vs_coolant_at_coolant80"] = {
                "coolant": WARM_C,
                #: bounded: an oil reading half a minute from the
                #: crossing is not the oil temperature at the crossing
                "oil": (
                    None if _nearest(
                        oil, warmed_t, pairing_for("n47d_oil_temp", "coolant")
                        .max_age_s
                    ) is None
                    else round(_nearest(
                        oil, warmed_t,
                        pairing_for("n47d_oil_temp", "coolant").max_age_s
                    ), 1)
                ),
                "delta": round(sum(deltas) / len(deltas), 2) if deltas else None,
                "pairs": len(deltas),
                "delta_min": round(min(deltas), 2) if deltas else None,
                "delta_max": round(max(deltas), 2) if deltas else None,
                #: None when speed was not captured - unknown, not "no".
                "moved": None if not speed else bool(moved and moved > 5),
                "max_speed": None if not speed else round(moved, 1),
            }

    return out


def crosschecks(run: Dict) -> List[Dict]:
    """Proprietary DDE read vs the standard OBD PID, sampled together."""
    out = []

    for prop_key, obd_key, scale, label in CROSSCHECKS:
        prop = run["series"].get(prop_key)
        obd = run["series"].get(obd_key)

        if not prop or not obd:
            continue

        rule = pairing_for(prop_key, obd_key)
        result = align(prop, obd, rule.max_age_s)
        diffs = [pv - ov * scale for _ts, pv, ov in result.pairs]

        if not diffs:
            continue

        mad = sum(abs(d) for d in diffs) / len(diffs)
        out.append({
            "label": label, "proprietary": prop_key, "obd": obd_key,
            "pairs": len(diffs),
            #: How much of the input was temporally comparable at all.
            #: A high mean_abs_diff on 5% coverage says nothing about
            #: the sensors and everything about the schedule.
            "max_age_s": rule.max_age_s,
            "coverage_pct": result.coverage_pct,
            "median_gap_s": result.median_gap_s,
            "usable": result.usable,
            "mean_abs_diff": round(mad, 2),
            "max_abs_diff": round(max(abs(d) for d in diffs), 2),
            "agree": mad < 3.0,      # within a few units/percent
        })

    return out


def phase_mask(run: Dict) -> Dict:
    """Driving (speed>threshold) vs idle, from the OBD speed channel."""
    speed = run["series"].get("speed")

    if not speed:
        return {"has_speed": False}

    driving = [(ts, v) for ts, v in speed if v > DRIVING_SPEED]
    idle = [(ts, v) for ts, v in speed if v <= DRIVING_SPEED]
    moving_span = None

    if driving:
        moving_span = (driving[0][0], driving[-1][0])

    return {
        "has_speed": True,
        "driving_samples": len(driving),
        "idle_samples": len(idle),
        "max_speed": round(max(v for _, v in speed), 1),
        "moving_span": moving_span,
    }


def _in_span(series, span):
    if span is None:
        return series
    a, b = span
    return [(ts, v) for ts, v in series if a <= ts <= b]


def load_behaviour(run: Dict, span) -> Dict:
    """During the driving phase: ranges + actual-vs-setpoint deviation."""
    out: Dict = {"ranges": {}, "setpoint_tracking": []}

    for key in ("rpm", "map", "n47d_boost_act", "n47d_rail_act",
                "n47d_maf_per_cyl", "n47d_pedal", "load", "speed",
                "maf", "rail"):
        s = _in_span(run["series"].get(key, []), span)

        if s:
            out["ranges"][key] = _stats([v for _, v in s])

    for act_key, set_key, label in SETPOINT_PAIRS:
        act = _in_span(run["series"].get(act_key, []), span)
        setp = run["series"].get(set_key, [])

        if not act or not setp:
            continue

        #
        # The pair this whole contract exists for. Actual and setpoint sit
        # in the same staggered DDE class, so post-hoc matching routinely
        # pairs values ~12 s apart - and the difference between them is
        # then mostly the engine having moved, reported as control error.
        #
        rule = pairing_for(act_key, set_key)
        result = align(act, setp, rule.max_age_s)
        devs = [av - sv for _ts, av, sv in result.pairs]

        entry = {
            "label": label, "actual": act_key, "setpoint": set_key,
            "pairs": len(devs),
            "max_age_s": rule.max_age_s,
            "coverage_pct": result.coverage_pct,
            "median_gap_s": result.median_gap_s,
            "usable": result.usable,
        }

        if devs:
            entry.update({
                "mean_abs_deviation": round(
                    sum(abs(d) for d in devs) / len(devs), 1),
                "max_abs_deviation": round(max(abs(d) for d in devs), 1),
            })

        out["setpoint_tracking"].append(entry)

    return out


def dpf(run: Dict) -> Dict:
    meas = run["series"].get("n47d_soot_meas")
    model = run["series"].get("n47d_soot_model")
    out: Dict = {}

    if meas:
        out["measured"] = _stats([v for _, v in meas])

    if model:
        out["modelled"] = _stats([v for _, v in model])

    if meas and model:
        rule = pairing_for("n47d_soot_meas", "n47d_soot_model")
        result = align(meas, model, rule.max_age_s)
        diffs = [mv - sv for _ts, mv, sv in result.pairs]

        out["max_age_s"] = rule.max_age_s
        out["coverage_pct"] = result.coverage_pct
        out["usable"] = result.usable

        if diffs:
            out["mean_abs_diff"] = round(
                sum(abs(d) for d in diffs) / len(diffs), 3)

    return out


def findings(run: Dict, wu, cc, lb, dp) -> List[str]:
    """
    Human interpretation of the numbers - the point of the whole exercise.
    Distinguishes a real disagreement from an OBD limitation.
    """
    out: List[str] = []

    #
    # Warm-up. State what was measured; conclude only what the session
    # could show. This used to end "Oil and engine temp tracked it
    # closely - a healthy warm-up with no lag anomaly", unconditionally
    # and regardless of whether oil lagged, led, or whether the car had
    # moved at all. "No lag anomaly" on a stationary idle is not a clean
    # bill of health; it is a measurement that was never possible.
    #
    cool = wu.get("coolant")
    if cool and cool.get("seconds_to_80C"):
        line = (
            f"Cold start captured from {cool['start']} °C; coolant reached "
            f"80 °C in {cool['seconds_to_80C']/60:.1f} min and stabilised near "
            f"{cool['max']} °C.")

        ovc = wu.get("oil_vs_coolant_at_coolant80")

        if ovc and ovc.get("moved") is False:
            line += (" Stationary throughout, so the load-driven oil lag "
                     "could not be observed either way — this is not "
                     "evidence of a healthy warm-up, only of a warm-up.")
        elif ovc and ovc.get("delta") is not None and ovc["delta"] <= -1.0:
            line += (f" Oil lagged coolant by {abs(ovc['delta']):.1f} °C on "
                     "average through the ramp — the expected signature.")
        elif ovc and ovc.get("delta") is not None:
            line += (f" Oil ran {ovc['delta']:+.1f} °C against coolant "
                     "through the ramp, so no lag was seen.")

        out.append(line)

    # OBD MAP saturation vs DDE boost — a data-quality finding, not a
    # cross-check failure.
    mp = run["series"].get("map")
    ba = run["series"].get("n47d_boost_act")
    if mp and ba:
        map_max = max(v for _, v in mp)
        boost_kpa = max(v for _, v in ba) / 10.0
        if map_max >= 255 and boost_kpa > 255:
            out.append(
                f"**OBD MAP saturates at 255 kPa**; under boost the DDE reads "
                f"the true manifold pressure up to {boost_kpa:.0f} kPa. The "
                "boost cross-check ⚠️ is OBD sensor saturation, NOT a decode "
                "error — above 255 kPa the DDE boost channel is the accurate "
                "one. (Exactly the 'generic OBD saturation' caveat the project "
                "set out to handle.)")

    # ambient quantisation
    amb = next((c for c in cc if c["obd"] == "baro"), None)
    if amb and amb.get("usable") and amb["mean_abs_diff"] < 12:
        out.append(
            f"Ambient/baro cross-check differs by only {amb['mean_abs_diff']} "
            "hPa on average — that is the standard OBD baro PID's 1 kPa integer "
            "quantisation, i.e. agreement within resolution, not a discrepancy.")

    # lambda sentinel
    lam = run["series"].get("lambda")
    if lam:
        at2 = sum(1 for _, v in lam if abs(v - 2.0) < 1e-6)
        if at2 > 0.2 * len(lam):
            out.append(
                f"Lambda sat at the 2.0 sentinel for {at2}/{len(lam)} samples "
                "(= 'no value', not a real λ of 2.0); exclude those from any "
                "AFR analysis.")

    # setpoint tracking health
    for t in lb.get("setpoint_tracking", []):
        if not t.get("usable"):
            #
            # The honest outcome, and the one this contract exists to
            # produce. Actual and setpoint share the staggered DDE class,
            # so almost no pair is close enough in time to be a control
            # error rather than the engine having moved. Stating a
            # deviation here would be describing the poll schedule.
            #
            out.append(
                f"**{t['label'].capitalize()} act-vs-setpoint cannot be "
                f"concluded from this session.** Only {t['coverage_pct']}% of "
                f"actual readings had a setpoint within {t['max_age_s']} s "
                f"(median gap {t['median_gap_s']} s), because both sit in the "
                "same staggered DDE class. The deviation this would report is "
                "mostly sampling misalignment. Co-scheduling the pair is the "
                "fix; see docs/ALIGNMENT.md.")
            continue

        out.append(
            f"{t['label'].capitalize()} closed-loop control tracked its "
            f"setpoint to {t['mean_abs_deviation']} mean deviation "
            f"(max {t['max_abs_deviation']}) over {t['coverage_pct']}% "
            f"coverage within {t['max_age_s']} s — the actuator is hitting "
            "its target; a growing deviation over future sessions would flag "
            "wear.")

    # DPF
    if dp.get("measured") and "mean_abs_diff" in dp:
        out.append(
            f"DPF soot measured vs modelled agree to {dp['mean_abs_diff']} g "
            f"(range {dp['measured']['min']}–{dp['measured']['max']} g) — "
            "differential-pressure sensing is healthy; this is a baseline to "
            "trend soot-accumulation rate against.")

    # -- new DPF/EGR candidate channels (validation by plausibility) ---
    def rng(key):
        s = run["series"].get(key)
        return (min(v for _, v in s), max(v for _, v in s)) if s else None

    dpf_dp = rng("n47d_dpf_dp")
    if dpf_dp:
        out.append(
            f"[CANDIDATE] DPF differential pressure {dpf_dp[0]:.1f}–"
            f"{dpf_dp[1]:.1f} hPa — should read low warm-idle and rise with "
            "exhaust flow under load; a plausible spread validates the 0x44F8 "
            "scale. (Baseline for filter-restriction trending.)")

    pre_dpf = rng("n47d_exh_temp_pre_dpf")
    pre_cat = rng("n47d_exh_temp_pre_cat")
    if pre_dpf:
        out.append(
            f"[CANDIDATE] Exhaust temp before DPF {pre_dpf[0]:.0f}–"
            f"{pre_dpf[1]:.0f} °C" +
            (f", before catalyst {pre_cat[0]:.0f}–{pre_cat[1]:.0f} °C"
             if pre_cat else "") +
            " — should climb under load; pre-cat typically hotter than "
            "pre-DPF. Validates the exhaust-temp scales.")

    dsr = rng("n47d_dist_since_regen")
    if dsr:
        out.append(
            f"[CANDIDATE] Distance since regen {dsr[0]:.1f}–{dsr[1]:.1f} km — "
            "should be a steady value increasing monotonically over the "
            "drive (unless a regen completes, resetting it).")

    egr_dev = rng("n47d_egr_deviation")
    if egr_dev:
        out.append(
            f"[CANDIDATE] EGR control deviation {egr_dev[0]:.1f}–"
            f"{egr_dev[1]:.1f} % — should sit near 0 when the loop is happy; "
            "a persistent offset would flag EGR fouling. Baseline for "
            "EGR-health trending.")

    regen = run["series"].get("n47d_opmode")
    if regen:
        vals = {v for _, v in regen}
        out.append(
            f"[CANDIDATE] Operating-mode word took {len(vals)} distinct "
            "value(s) — bit 0x02 is the regeneration-active flag; a change "
            "mid-drive would mark a regeneration event.")

    return out


def quality(run: Dict) -> List[Dict]:
    """
    Per-channel coverage, plus what the recorder flagged and what it did not.

    Two different things live here and they should not be confused:

    `flagged` is what the DECODER declared - sentinel, saturated, clipped -
    from the mapping's own `invalid:`/`saturated:`/range declarations. It
    is authoritative and needs no interpretation.

    `pinned_at_max` is a heuristic over what is LEFT after those are
    removed. It used to be the only saturation detector, and it is kept
    for a different job now: finding cases nobody has declared yet. A
    channel that still shows a hard pin after the declared ones are gone
    is a candidate for investigation - which is exactly how the MAF
    222.22 g/s artifact surfaced. It is a lead, never a conclusion.
    """
    out = []
    flagged_counts = run.get("flagged_counts", {})

    for key, s in sorted(run["series"].items()):
        ts = [t for t, _ in s]
        vals = [v for _, v in s]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        vmax = max(vals)
        pinned = sum(1 for v in vals if v == vmax)
        flags = flagged_counts.get(key, {})
        out.append({
            "key": key,
            "obd": run["is_obd"].get(key, False),
            "samples": len(s),
            "max_gap_s": round(max(gaps), 1) if gaps else None,
            "unit": run["units"].get(key, ""),
            "pinned_at_max": pinned if pinned > max(3, 0.2 * len(vals)) else 0,
            #: declared by the mapping, excluded from `samples` above
            #: unless the report was run with --include-flagged
            "flagged": flags,
            "flagged_total": sum(flags.values()),
        })

    #
    # A channel can be flagged into silence - every reading a sentinel,
    # nothing usable left. It then has no series at all, so the loop
    # above never sees it, and omitting it would recreate the very
    # confusion this layer exists to end: "no data" looking identical to
    # "not polled".
    #
    for key in sorted(set(flagged_counts) - set(run["series"])):
        flags = flagged_counts[key]
        out.append({
            "key": key,
            "obd": run["is_obd"].get(key, False),
            "samples": 0,
            "max_gap_s": None,
            "unit": run["units"].get(key, ""),
            "pinned_at_max": 0,
            "flagged": flags,
            "flagged_total": sum(flags.values()),
        })

    return out


# ----------------------------------------------------------- rendering


def _svg_line(series, t0, w=560, h=120, color="#3987e5"):
    if len(series) < 2:
        return ""

    xs = [ts - t0 for ts, _ in series]
    ys = [v for _, v in series]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)

    if x1 - x0 < 1e-6:
        x1 = x0 + 1
    if y1 - y0 < 1e-6:
        y1 = y0 + 1

    def px(x):
        return 40 + (w - 50) * (x - x0) / (x1 - x0)

    def py(y):
        return 10 + (h - 30) * (1 - (y - y0) / (y1 - y0))

    pts = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in zip(xs, ys))
    return (
        f'<polyline fill="none" stroke="{color}" stroke-width="1.6" '
        f'points="{pts}"/>'
        f'<text x="2" y="14" fill="#8b97ab" font-size="10">{y1:.0f}</text>'
        f'<text x="2" y="{h-12}" fill="#8b97ab" font-size="10">{y0:.0f}</text>'
    )


def render_html(run: Dict) -> str:
    t0 = run["started"]
    blocks = []
    charts = [
        ("Warm-up — coolant (OBD) & oil (DDE)",
         [("coolant", "#e66767"), ("n47d_oil_temp", "#c98500"),
          ("n47d_engine_temp", "#3987e5")]),
        ("Drive — RPM", [("rpm", "#3987e5")]),
        ("Drive — boost actual (hPa)", [("n47d_boost_act", "#199e70")]),
        ("Drive — rail actual (bar)", [("n47d_rail_act", "#9085e9")]),
    ]

    for title, keys in charts:
        lines = ""

        for key, color in keys:
            s = run["series"].get(key)

            if s:
                lines += _svg_line(s, t0, color=color)

        if lines:
            legend = "  ".join(f'<span style="color:{c}">■ {k}</span>'
                               for k, c in keys if run["series"].get(k))
            blocks.append(
                f'<h3>{title}</h3><div class="legend">{legend}</div>'
                f'<svg viewBox="0 0 560 120" width="100%">{lines}</svg>'
            )

    body = "\n".join(blocks)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Session curves</title><style>
body{{background:#0b0e13;color:#e6edf7;font:13px system-ui;padding:16px;max-width:640px}}
h3{{font-size:13px;margin:18px 0 2px}} .legend{{font-size:11px;color:#8b97ab;margin-bottom:4px}}
svg{{background:#141922;border:1px solid #263041;border-radius:8px}}
</style></head><body><h1>Session {run['run_id']} curves</h1>{body}</body></html>"""


def render_markdown(run: Dict, wu, cc, ph, lb, dp, ql) -> str:
    dur = (run["ended"] or run["series"] and max(
        t for s in run["series"].values() for t, _ in s)) - run["started"]
    L = [
        f"# Session report — run {run['run_id']}",
        "",
        f"- ECU: {run['ecu']}  (addr {run['ecu_addr']})",
        f"- Duration: {dur/60:.1f} min, "
        f"{sum(len(s) for s in run['series'].values())} samples across "
        f"{len(run['series'])} channels",
        f"- Started (UTC): {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(run['started']))}",
        "",
    ]

    #
    # Every number below is time-derived: warm-up gradients, alignment
    # windows, rates. The Pi has no RTC and once stepped its clock 76.5
    # minutes mid-recording. A run whose clock was not disciplined cannot
    # support any of it, and saying so at the top is the only place a
    # reader will not miss it.
    #
    if run.get("clock_synced") != 1:
        state = (
            "was NOT NTP-disciplined" if run.get("clock_synced") == 0
            else "is UNKNOWN (this run predates the flag)"
        )
        L += [
            f"> ⚠️ **The host clock {state} for this run.** Every "
            "time-derived number below - warm-up gradients, alignment "
            "windows, sample rates - rests on timestamps that may not be "
            "ordered or spaced as recorded. Treat them as indicative only.",
            "",
        ]

    fnd = findings(run, wu, cc, lb, dp)
    if fnd:
        L += ["## Key findings", ""]
        L += [f"- {line}" for line in fnd]
        L += [""]

    L += ["## Cold-start warm-up", ""]

    if wu:
        L.append("| channel | start | max | →80 °C | unit |")
        L.append("|---|---|---|---|---|")

        for key, d in wu.items():
            if not isinstance(d, dict) or "start" not in d:
                continue

            t80 = f"{d['seconds_to_80C']:.0f}s" if d.get("seconds_to_80C") else "—"
            L.append(f"| {key} | {d['start']} | {d['max']} | {t80} | {d['unit']} |")

        ovc = wu.get("oil_vs_coolant_at_coolant80")

        if ovc and ovc.get("delta") is not None:
            delta = ovc["delta"]
            L.append("")
            L.append(f"- Across the warm-up, oil ran **{delta:+.2f} °C** "
                     f"against coolant (mean of {ovc['pairs']} matched "
                     f"pairs, range {ovc['delta_min']:+.2f} to "
                     f"{ovc['delta_max']:+.2f}).")

            #
            # State the observation, then interpret it only where the
            # data supports an interpretation. The oil lag is driven by
            # LOAD, so a stationary session cannot show one and must not
            # be read as if it failed to.
            #
            if ovc["moved"] is False:
                L.append(f"  The car did not move (max speed "
                         f"{ovc['max_speed']} km/h), so **no lag should be "
                         "expected**: oil takes heat from work done, and at "
                         "idle it warms from the block instead. This says "
                         "nothing either way about the load-driven warm-up "
                         "signature, which needs a cold start followed by "
                         "driving.")
            elif delta <= -1.0:
                L.append("  Oil lags coolant — the expected load-driven "
                         "warm-up signature.")
            elif delta >= 1.0:
                L.append("  **Oil is above coolant**, which is not the "
                         "expected signature under load. Worth checking "
                         "before it is used as a baseline.")
            else:
                L.append("  The two track each other to within 1 °C — no "
                         "lag either way.")
    else:
        L.append("_no temperature channels captured_")

    L += ["", "## Proprietary DDE vs standard OBD (live cross-check)", ""]

    if cc:
        L.append("| quantity | pairs | window | coverage | median gap | "
                 "mean |Δ| | max |Δ| | agree |")
        L.append("|---|---|---|---|---|---|---|---|")

        for c in cc:
            verdict = (
                "✅" if c["agree"] and c.get("usable")
                else "⚠️" if c.get("usable") else "insufficient"
            )
            L.append(f"| {c['label']} | {c['pairs']} | {c['max_age_s']}s | "
                     f"{c['coverage_pct']}% | {c['median_gap_s']}s | "
                     f"{c['mean_abs_diff']} | {c['max_abs_diff']} | "
                     f"{verdict} |")

        L += ["",
              "`coverage` is the share of proprietary readings that had an "
              "OBD reading inside the window. A comparison below "
              f"{MIN_USEFUL_COVERAGE:.0f}% is reported as insufficient rather "
              "than averaged: the number would describe the poll schedule, "
              "not the sensors."]
    else:
        L.append("_no cross-check pairs available_")

    L += ["", "## Drive / load behaviour", ""]

    if ph.get("has_speed"):
        L.append(f"- max speed {ph['max_speed']} km/h; "
                 f"{ph['driving_samples']} driving / {ph['idle_samples']} idle "
                 "samples (speed>3 km/h = driving).")

    if lb.get("setpoint_tracking"):
        L.append("")
        L.append("| loop | pairs | window | coverage | median gap | "
                 "mean |dev| | max |dev| |")
        L.append("|---|---|---|---|---|---|---|")

        for t in lb["setpoint_tracking"]:
            if t.get("usable"):
                mean = t["mean_abs_deviation"]
                mx = t["max_abs_deviation"]
            else:
                mean = mx = "insufficient"

            L.append(f"| {t['label']} (act−set) | {t['pairs']} | "
                     f"{t['max_age_s']}s | {t['coverage_pct']}% | "
                     f"{t['median_gap_s']}s | {mean} | {mx} |")

    if lb.get("ranges"):
        L.append("")
        L.append("| channel | min | max | mean | p95 |")
        L.append("|---|---|---|---|---|")

        for key, s in lb["ranges"].items():
            if s:
                L.append(f"| {key} | {s['min']} | {s['max']} | {s['mean']} | "
                         f"{s['p95']} |")

    L += ["", "## DPF", ""]

    if dp:
        if dp.get("measured"):
            L.append(f"- soot measured: {dp['measured']['min']}–"
                     f"{dp['measured']['max']} g")

        if dp.get("modelled"):
            L.append(f"- soot modelled: {dp['modelled']['min']}–"
                     f"{dp['modelled']['max']} g")

        if "mean_abs_diff" in dp:
            L.append(f"- measured vs modelled mean |Δ|: {dp['mean_abs_diff']} g "
                     "(the two independent estimates should agree)")
    else:
        L.append("_no DPF channels captured_")

    total_flagged = sum(q["flagged_total"] for q in ql)

    L += ["", "## Data quality / coverage", ""]

    if run.get("include_flagged"):
        L += ["**--include-flagged is on**: readings the decoder flagged as "
              "not-measurements are INCLUDED in every statistic above. "
              f"{total_flagged} such samples. Do not read the numbers as "
              "physical observations.", ""]
    elif total_flagged:
        L += [f"{total_flagged} samples were flagged by the decoder and "
              "excluded from every statistic above - sentinels the ECU "
              "returned to mean \"no value\", sensors pinned on a rail, "
              "and values outside a declared range. They are counted per "
              "channel below rather than silently dropped.", ""]

    L += ["| channel | src | samples | max gap | flagged | pinned@max |",
          "|---|---|---|---|---|---|"]

    for q in ql:
        src = "OBD" if q["obd"] else "DDE"
        pin = q["pinned_at_max"] or ""
        flags = ", ".join(
            f"{label} {n}" for label, n in sorted(q["flagged"].items())
        )
        gap = "—" if q["max_gap_s"] is None else f"{q['max_gap_s']}s"
        L.append(f"| {q['key']} | {src} | {q['samples']} | "
                 f"{gap} | {flags} | {pin} |")

    L += ["",
          "`flagged` is declared by the mapping and is authoritative. "
          "`pinned@max` is a heuristic over what remains, kept to surface "
          "saturation nobody has declared **yet** - a lead to investigate, "
          "not a finding.",
          "", "---",
          "_Read-only analysis; no baselines across sessions claimed yet._",
          ""]

    return "\n".join(L)


# ----------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="cold-start + drive session report")
    ap.add_argument("--db", required=True)
    ap.add_argument("--run", type=int, default=None)
    ap.add_argument("--out", default=os.path.join(_ROOT, "drive-sessions"))
    ap.add_argument(
        "--include-flagged", action="store_true",
        help="include samples the decoder flagged (sentinel, saturated, "
             "clipped). Off by default: they are not measurements, and "
             "averaging them with real readings is how a health model "
             "learns something false. Turn on only to study them.",
    )
    args = ap.parse_args()

    run = load_run(args.db, args.run, include_flagged=args.include_flagged)

    wu = warmup(run)
    cc = crosschecks(run)
    ph = phase_mask(run)
    lb = load_behaviour(run, ph.get("moving_span"))
    dp = dpf(run)
    ql = quality(run)

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = os.path.join(args.out, f"{stamp}-session")
    os.makedirs(out, exist_ok=True)

    md = render_markdown(run, wu, cc, ph, lb, dp, ql)

    with open(os.path.join(out, "report.md"), "w") as fh:
        fh.write(md)

    with open(os.path.join(out, "summary.json"), "w") as fh:
        json.dump({
            "run_id": run["run_id"], "ecu": run["ecu"],
            "warmup": wu, "crosschecks": cc, "phase": ph,
            "load": lb, "dpf": dp, "quality": ql,
        }, fh, indent=2)

    with open(os.path.join(out, "curves.html"), "w") as fh:
        fh.write(render_html(run))

    print(md)
    print(f"\n[+] written to {os.path.relpath(out, _ROOT)}/")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
