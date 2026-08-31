-- Per-vehicle analytics on the telemetry lake. VIN-free (committable):
-- pass the vehicle with --param_vin=<VIN>. Run:
--   clickhouse-client --param_vin=<VIN> --multiquery < insights.sql
-- Analytics is PER VEHICLE by design; cross-vehicle mixing is noise.
--
-- EVERY query that interprets `value` as a physical measurement filters
-- `quality = 'ok'`. A sentinel the ECU returned to mean "no value", a
-- sensor pinned on its rail and a clipped reading are all numbers, and
-- letting them into a health metric is how this turns into a confident
-- wrong answer. The data-quality sections (6, 6b) deliberately do NOT
-- filter - reporting on the flagged rows is their entire job.
--
-- Historical caveat, and it matters for every trend below: rows recorded
-- before the data-quality layer landed are all 'ok' because nothing was
-- labelling them, NOT because they were verified clean. That era
-- contains unlabelled lambda sentinels and saturated MAP. Filtering on
-- quality does not retroactively clean it.
--
-- THE ALIGNMENT CONTRACT. Every cross-channel comparison below obeys
-- four rules, because a plausible graph built from mismatched
-- observations is worse than no graph:
--
--   same session      - ASOF joins key on session_id, not just
--                       vehicle_id. Without it a join silently reaches
--                       into the previous drive for its "nearest" value.
--   bounded age       - each pair declares a maximum gap. The window is
--                       per pair: a control loop needs sub-second, a
--                       coolant cross-check tolerates 15 s.
--   clock trust       - sessions.clock_synced = 1. The Pi has no RTC and
--                       once stepped 76.5 min mid-recording; a timestamp
--                       difference from an undisciplined run means
--                       nothing. NULL is "recorded before the flag" -
--                       unknown, so excluded, not assumed good.
--   quality = 'ok'    - as above.
--
-- Section 7 reports what those rules cost. Read it before trusting any
-- number above it: on the lake as of 2026-08-31 only 9 of 119 sessions
-- carry clock_synced = 1, so most history is legitimately excluded from
-- time-derived work. That is the contract working, not a bug.
--
-- The per-pair windows are the same ones analysis/alignment.py declares.
-- See docs/ALIGNMENT.md.

-- 1. Session inventory ------------------------------------------------
SELECT '=== 1. drives (sessions) ===' AS _;
SELECT session_id,
       formatDateTime(min(ts),'%m-%d %H:%M') AS started,
       round(dateDiff('second', min(ts), max(ts))/60.0,1) AS min,
       count() AS samples,
       -- count() is inventory and stays whole; the maxima are
       -- measurements, so they take only usable readings.
       round(maxIf(value, channel='vehicle.speed' AND quality='ok'),0) AS max_kmh,
       round(maxIf(value, channel='engine.rpm'    AND quality='ok'),0) AS max_rpm
FROM telemetry.samples
WHERE vehicle_id = {vin:String}
GROUP BY session_id HAVING samples > 2000
ORDER BY started;

-- 2. DPF differential pressure vs exhaust flow (the restriction baseline)
--    ASOF-join each dP reading to the nearest engine MAF, bin by flow.
SELECT '=== 2. DPF dP vs MAF flow (median/p10/p90 hPa) ===' AS _;
SELECT round(maf,-1) AS maf_gps,
       count() AS n,
       round(quantile(0.5)(dp),1)  AS med_dP,
       round(quantile(0.1)(dp),1)  AS p10,
       round(quantile(0.9)(dp),1)  AS p90
FROM (
  SELECT a.ts AS ts, a.value AS dp, b.value AS maf,
         dateDiff('millisecond', b.ts, a.ts)/1000.0 AS gap_s
  FROM (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='dpf.differential_pressure'
          AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.maf' AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) b
  ON a.session_id=b.session_id AND a.ts>=b.ts
)
WHERE gap_s <= 15.0            -- both slow channels; 15 s per the contract
GROUP BY maf_gps ORDER BY maf_gps;

-- 3. Boost actual-vs-setpoint deviation, conditioned on RPM ----------
SELECT '=== 3. boost act-set deviation by RPM band (hPa) ===' AS _;
SELECT multiIf(rpm<1000,'idle',rpm<1800,'1000-1800',rpm<2600,'1800-2600','2600+') AS rpm_band,
       count() AS n,
       round(avg(abs(dev)),1) AS mean_abs_dev,
       round(quantile(0.95)(abs(dev)),1) AS p95_abs_dev
--
-- NOTE, and it is the whole reason this contract exists: actual and
-- setpoint share the staggered DDE class, so their median gap on this
-- vehicle is ~12 s and only ~5% of pairs fall inside the 0.5 s window a
-- control loop needs. This query will therefore return very few rows,
-- and that is the correct answer - the deviation it used to report was
-- mostly the engine having moved between the two reads. Co-scheduling
-- the pair at acquisition is the fix; see docs/ALIGNMENT.md.
--
FROM (
  SELECT a.value - b.value AS dev, c.value AS rpm
  FROM (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.actual'
          AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.setpoint'
          AND quality='ok') b
  ON a.session_id=b.session_id AND a.ts>=b.ts
  ASOF JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.rpm'
          AND quality='ok') c
  ON a.session_id=c.session_id AND a.ts>=c.ts
  WHERE dateDiff('millisecond', b.ts, a.ts) <= 500      -- control loop
    AND dateDiff('millisecond', c.ts, a.ts) <= 500      -- conditioning var
)
GROUP BY rpm_band ORDER BY n DESC;

-- 4. DPF soot vs distance-since-regen (accumulation over the fleet-life)
SELECT '=== 4. DPF soot vs distance-since-regen ===' AS _;
SELECT round(dist,0) AS dist_km,
       round(quantile(0.5)(soot),2) AS med_soot_g
FROM (
  SELECT a.value AS dist, b.value AS soot,
         dateDiff('millisecond', b.ts, a.ts)/1000.0 AS gap_s
  FROM (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String}
          AND channel='dpf.distance_since_regeneration' AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='dpf.soot_mass.measured'
          AND quality='ok') b
  ON a.session_id=b.session_id AND a.ts>=b.ts
)
WHERE gap_s <= 15.0            -- two slow ECU model outputs
GROUP BY dist_km ORDER BY dist_km;

-- 5. DDE-vs-OBD coolant agreement per drive (decode-path health) ------
SELECT '=== 5. DDE vs OBD coolant agreement per session (mean |diff| degC) ===' AS _;
SELECT session_id,
       round(avg(abs(dde - obd)),3) AS mean_abs_diff,
       count() AS pairs
FROM (
  SELECT a.session_id AS session_id, a.value AS dde, b.value AS obd,
         dateDiff('millisecond', b.ts, a.ts)/1000.0 AS gap_s
  FROM (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='n47d_coolant'
          AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='coolant'
          AND quality='ok') b
  ON a.session_id=b.session_id AND a.ts>=b.ts
)
WHERE gap_s <= 15.0            -- coolant moves far slower than the window
GROUP BY session_id HAVING pairs>20 ORDER BY session_id;

-- 6. Data quality: what the decoder flagged -------------------------
--
-- These used to be hard-coded value tests (value>=255, value>=2.0) that
-- rediscovered MAP saturation and the lambda sentinel by hand, in the
-- report, every time. The mapping now declares both, and the recorder
-- stores the verdict, so the query reads the label instead of guessing
-- at the number. A new sentinel gets declared once in the mapping and
-- appears here without anyone editing this file.
--
-- CAVEAT for longitudinal work: rows recorded before the data-quality
-- layer landed are all 'ok', because nothing was labelling them - not
-- because they were clean. Anything comparing flagged rates ACROSS that
-- boundary is comparing two different questions. Filter on the era, or
-- restrict to sessions recorded after it.
SELECT '=== 6. data quality flags (declared, not guessed) ===' AS _;
-- The percentage is of the WHOLE channel, so totals come from a
-- separate aggregate: a window over the filtered rows would divide the
-- flagged count by itself and print 100% every time.
SELECT s.channel                                   AS channel,
       s.quality                                   AS quality,
       count()                                     AS rows,
       round(100.0 * count() / any(t.total), 2)    AS pct_of_channel,
       min(s.value)                                AS vmin,
       max(s.value)                                AS vmax
FROM telemetry.samples s
INNER JOIN (
    SELECT channel, count() AS total
    FROM telemetry.samples
    WHERE vehicle_id={vin:String}
    GROUP BY channel
) t ON t.channel = s.channel
WHERE s.vehicle_id={vin:String} AND s.quality != 'ok'
GROUP BY s.channel, s.quality
ORDER BY rows DESC;

-- 6b. Channels answering nothing usable ------------------------------
--
-- The case a request-level success rate cannot show: every exchange
-- succeeded and not one reading was a measurement.
SELECT '=== 6b. channels with no usable readings ===' AS _;
SELECT channel,
       count()                        AS total,
       countIf(quality = 'ok')        AS usable
FROM telemetry.samples
WHERE vehicle_id={vin:String}
GROUP BY channel
HAVING usable = 0
ORDER BY total DESC;

-- 7. Alignment coverage: what the contract rejected --------------------
--
-- Read this before trusting anything above. A metric is only as good as
-- the share of its inputs that were actually comparable, and a confident
-- average over 5% of the data is the failure mode this whole section
-- exists to make visible.
SELECT '=== 7. alignment coverage (how much was comparable) ===' AS _;

-- 7a. How much of the lake is eligible for time-derived work at all.
SELECT 'sessions with clock_synced=1' AS metric,
       countIf(clock_synced = 1)      AS value,
       count()                        AS of_total
FROM (SELECT session_id, any(clock_synced) AS clock_synced
      FROM telemetry.sessions
      WHERE vehicle_id={vin:String} GROUP BY session_id);

-- 7b. Per control pair: median gap, and the share inside its window.
--     A low pct_in_window is an ACQUISITION finding, not a data error -
--     it means the two channels are never sampled close enough together
--     for the comparison to mean anything.
SELECT pair,
       count()                                          AS candidate_pairs,
       round(quantile(0.5)(gap_s), 2)                   AS median_gap_s,
       max_age_s,
       round(100.0 * countIf(gap_s <= max_age_s) / count(), 1) AS pct_in_window
FROM (
  SELECT 'boost act/set' AS pair, 0.5 AS max_age_s,
         dateDiff('millisecond', b.ts, a.ts)/1000.0 AS gap_s
  FROM (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.actual'
          AND quality='ok') a
  ASOF JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.setpoint'
          AND quality='ok') b
  ON a.session_id=b.session_id AND a.ts>=b.ts

  UNION ALL

  SELECT 'rail act/set', 0.5,
         dateDiff('millisecond', b.ts, a.ts)/1000.0
  FROM (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='fuel.rail_pressure.actual'
          AND quality='ok') a
  ASOF JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='fuel.rail_pressure.setpoint'
          AND quality='ok') b
  ON a.session_id=b.session_id AND a.ts>=b.ts

  UNION ALL

  SELECT 'DDE/OBD coolant', 15.0,
         dateDiff('millisecond', b.ts, a.ts)/1000.0
  FROM (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='n47d_coolant'
          AND quality='ok') a
  ASOF JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='coolant'
          AND quality='ok') b
  ON a.session_id=b.session_id AND a.ts>=b.ts
)
GROUP BY pair, max_age_s
ORDER BY pct_in_window;
