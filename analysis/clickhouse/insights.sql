-- Per-vehicle analytics on the telemetry lake. VIN-free (committable):
-- pass the vehicle with --param_vin=<VIN>. Run:
--   clickhouse-client --param_vin=<VIN> --param_dpf_present=0 \
--                     --multiquery < insights.sql
--
-- VEHICLE CONFIGURATION IS AN INPUT. `dpf_present` says whether this car
-- physically has a particulate filter. Sections 2 and 4 draw conclusions
-- about a filter, and on a car without one they are not uncertain, they
-- are impossible - `dpf.differential_pressure` measures an empty pipe.
-- Pass 0 and those sections return a single explanatory row instead of a
-- plausible-looking baseline.
--
-- THE TARGET CAR HAS NO FILTER: pass --param_dpf_present=0. It is a
-- required parameter rather than a default precisely so that nobody
-- reads section 2 without having answered the question. Keep it in step
-- with local/vehicle-profile.yaml; see docs/VEHICLE_PROFILE.md.
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
--
-- THE ALIGNMENT PATTERN, used verbatim by every comparison below.
--
-- ClickHouse's ASOF JOIN is one-directional: `a.ts>=b.ts` can only find
-- the most recent EARLIER row, so a sample 100 ms after `a` is ignored in
-- favour of one 10 s before it. That is not "nearest", and it silently
-- disagreed with the Python matcher on the same data.
--
-- So each comparison joins BOTH directions and picks the nearer:
--
--   ASOF LEFT JOIN ... prev ON a.session_id=prev.session_id AND a.ts>=prev.ts
--   ASOF LEFT JOIN ... next ON a.session_id=next.session_id AND a.ts<=next.ts
--
-- An unmatched LEFT side yields the type default (1970), which makes
-- prev_gap enormous - harmless, it loses and fails the window - but makes
-- next_gap NEGATIVE, which would win least(). Hence the >= 0 guard.
-- Ties go to `prev`, matching analysis/alignment.py.
--

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
SELECT if({dpf_present:UInt8} = 1,
          '=== 2. DPF dP vs MAF flow (median/p10/p90 hPa) ===',
          '=== 2. SKIPPED: no particulate filter on this vehicle - a dP-vs-flow restriction baseline across an empty pipe measures nothing ===') AS _;
SELECT round(maf,-1) AS maf_gps,
       count() AS n,
       round(quantile(0.5)(dp),1)  AS med_dP,
       round(quantile(0.1)(dp),1)  AS p10,
       round(quantile(0.9)(dp),1)  AS p90
FROM (
  SELECT a.ts AS ts, a.value AS dp,
         if(prev_gap <= next_gap, prev.value, next.value) AS maf,
         dateDiff('millisecond', prev.ts, a.ts)/1000.0 AS prev_gap,
         if(dateDiff('millisecond', a.ts, next.ts) >= 0,
            dateDiff('millisecond', a.ts, next.ts)/1000.0, 1e18) AS next_gap,
         least(prev_gap, next_gap) AS gap_s
  FROM (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='dpf.differential_pressure'
          AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF LEFT JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.maf'
          AND quality='ok') prev
    ON a.session_id=prev.session_id AND a.ts>=prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.maf'
          AND quality='ok') next
    ON a.session_id=next.session_id AND a.ts<=next.ts
)
WHERE gap_s <= 15.0            -- both slow channels; 15 s per the contract
  AND {dpf_present:UInt8} = 1  -- no filter fitted -> no restriction baseline
GROUP BY maf_gps ORDER BY maf_gps;

-- 3. Boost actual-vs-setpoint deviation, conditioned on RPM ----------
SELECT '=== 3. boost act-set deviation by RPM band (hPa) ===' AS _;
SELECT multiIf(rpm<1000,'idle',rpm<1800,'1000-1800',rpm<2600,'1800-2600','2600+') AS rpm_band,
       count() AS n,
       round(avg(abs(dev)),1) AS mean_abs_dev,
       round(quantile(0.95)(abs(dev)),1) AS p95_abs_dev
--
-- WINDOW NOTE. Actual and setpoint are sampled 0.56 s apart on this
-- vehicle (p10..p90 = 0.52..0.59), so the window is 1.0 s and captures
-- 98.9% of pairs. A 0.5 s window - the number a control loop suggests in
-- the abstract - sits just BELOW the real separation and keeps 4.6%,
-- measuring the threshold rather than the car.
--
-- Measuring this pair with a BACKWARD-ONLY ASOF reports a 12.3 s median,
-- because it can only look back to the previous round-robin visit. That
-- is why the two-sided pattern above is not an optimisation.
--
-- The residual 0.56 s is not free: against 10 Hz MAP as a proxy for real
-- slew, manifold pressure moves 0 hPa at the median but 140 hPa at p90
-- and 672 hPa at p99 over that interval. So this metric is sound in
-- aggregate and at steady state, and a single large excursion under hard
-- acceleration may be misalignment rather than the actuator.
-- Co-scheduling the pair would remove the residual; see docs/ALIGNMENT.md.
--
FROM (
  SELECT a.value - if(sp_prev_gap <= sp_next_gap, sp_prev.value, sp_next.value)
             AS dev,
         if(rpm_prev_gap <= rpm_next_gap, rpm_prev.value, rpm_next.value)
             AS rpm,
         dateDiff('millisecond', sp_prev.ts, a.ts)/1000.0 AS sp_prev_gap,
         if(dateDiff('millisecond', a.ts, sp_next.ts) >= 0,
            dateDiff('millisecond', a.ts, sp_next.ts)/1000.0, 1e18)
             AS sp_next_gap,
         dateDiff('millisecond', rpm_prev.ts, a.ts)/1000.0 AS rpm_prev_gap,
         if(dateDiff('millisecond', a.ts, rpm_next.ts) >= 0,
            dateDiff('millisecond', a.ts, rpm_next.ts)/1000.0, 1e18)
             AS rpm_next_gap
  FROM (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.actual'
          AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF LEFT JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.setpoint'
          AND quality='ok') sp_prev
    ON a.session_id=sp_prev.session_id AND a.ts>=sp_prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.setpoint'
          AND quality='ok') sp_next
    ON a.session_id=sp_next.session_id AND a.ts<=sp_next.ts
  ASOF LEFT JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.rpm'
          AND quality='ok') rpm_prev
    ON a.session_id=rpm_prev.session_id AND a.ts>=rpm_prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.rpm'
          AND quality='ok') rpm_next
    ON a.session_id=rpm_next.session_id AND a.ts<=rpm_next.ts
)
WHERE least(sp_prev_gap, sp_next_gap) <= 1.0        -- measured separation 0.56 s
  AND least(rpm_prev_gap, rpm_next_gap) <= 1.0      -- conditioning variable
GROUP BY rpm_band ORDER BY n DESC;

-- 4. DPF soot vs distance-since-regen (accumulation over the fleet-life)
SELECT if({dpf_present:UInt8} = 1,
          '=== 4. DPF soot vs distance-since-regen ===',
          '=== 4. SKIPPED: no particulate filter - the soot channels model hardware that is not fitted, so this describes the model, not a filter ===') AS _;
SELECT round(dist,0) AS dist_km,
       round(quantile(0.5)(soot),2) AS med_soot_g
FROM (
  SELECT a.value AS dist,
         if(prev_gap <= next_gap, prev.value, next.value) AS soot,
         dateDiff('millisecond', prev.ts, a.ts)/1000.0 AS prev_gap,
         if(dateDiff('millisecond', a.ts, next.ts) >= 0,
            dateDiff('millisecond', a.ts, next.ts)/1000.0, 1e18) AS next_gap,
         least(prev_gap, next_gap) AS gap_s
  FROM (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String}
          AND channel='dpf.distance_since_regeneration' AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF LEFT JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='dpf.soot_mass.measured'
          AND quality='ok') prev
    ON a.session_id=prev.session_id AND a.ts>=prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='dpf.soot_mass.measured'
          AND quality='ok') next
    ON a.session_id=next.session_id AND a.ts<=next.ts
)
WHERE gap_s <= 15.0            -- two slow ECU model outputs
  AND {dpf_present:UInt8} = 1  -- see the section header
GROUP BY dist_km ORDER BY dist_km;

-- 5. DDE-vs-OBD coolant agreement per drive (decode-path health) ------
SELECT '=== 5. DDE vs OBD coolant agreement per session (mean |diff| degC) ===' AS _;
SELECT session_id,
       round(avg(abs(dde - obd)),3) AS mean_abs_diff,
       count() AS pairs
FROM (
  SELECT a.session_id AS session_id, a.value AS dde,
         if(prev_gap <= next_gap, prev.value, next.value) AS obd,
         dateDiff('millisecond', prev.ts, a.ts)/1000.0 AS prev_gap,
         if(dateDiff('millisecond', a.ts, next.ts) >= 0,
            dateDiff('millisecond', a.ts, next.ts)/1000.0, 1e18) AS next_gap,
         least(prev_gap, next_gap) AS gap_s
  FROM (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='n47d_coolant'
          AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF LEFT JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='coolant'
          AND quality='ok') prev
    ON a.session_id=prev.session_id AND a.ts>=prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='coolant'
          AND quality='ok') next
    ON a.session_id=next.session_id AND a.ts<=next.ts
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

-- 7b. Per control pair, over the TRUSTED population only: median gap and
--     the share inside its window.
--
--     Clock-gated like sections 2-5, and for the same reason: a gap is a
--     timestamp difference, so measuring it on a session whose wall clock
--     stepped is measuring the step. It would be incoherent to say "only
--     disciplined sessions may support time-derived work" and then derive
--     the headline alignment numbers from undisciplined ones.
--
--     These are the numbers the windows in analysis/alignment.py were
--     chosen from. On this vehicle the trusted subset is tighter than the
--     full population, not looser: boost act/set p50 0.53 s and 100%
--     inside 1 s, against 0.56 s and 98.9% across everything.
--
--     A low pct_in_window is an ACQUISITION finding, not a data error -
--     it means the two channels are never sampled close enough together
--     for the comparison to mean anything. Section 7c reports the same
--     gaps WITHOUT the clock gate, as raw schedule diagnostics.
SELECT pair,
       count()                                          AS candidate_pairs,
       round(quantile(0.5)(gap_s), 2)                   AS median_gap_s,
       max_age_s,
       round(100.0 * countIf(gap_s <= max_age_s) / count(), 1) AS pct_in_window
FROM (
  SELECT 'boost act/set' AS pair, 1.0 AS max_age_s,
         least(dateDiff('millisecond', prev.ts, a.ts)/1000.0,
               if(dateDiff('millisecond', a.ts, next.ts) >= 0,
                  dateDiff('millisecond', a.ts, next.ts)/1000.0, 1e18)) AS gap_s
  FROM (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.actual'
          AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.setpoint'
          AND quality='ok') prev
    ON a.session_id=prev.session_id AND a.ts>=prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.setpoint'
          AND quality='ok') next
    ON a.session_id=next.session_id AND a.ts<=next.ts

  UNION ALL

  SELECT 'rail act/set' AS pair, 1.0 AS max_age_s,
         least(dateDiff('millisecond', prev.ts, a.ts)/1000.0,
               if(dateDiff('millisecond', a.ts, next.ts) >= 0,
                  dateDiff('millisecond', a.ts, next.ts)/1000.0, 1e18)) AS gap_s
  FROM (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='fuel.rail_pressure.actual'
          AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='fuel.rail_pressure.setpoint'
          AND quality='ok') prev
    ON a.session_id=prev.session_id AND a.ts>=prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='fuel.rail_pressure.setpoint'
          AND quality='ok') next
    ON a.session_id=next.session_id AND a.ts<=next.ts

  UNION ALL

  SELECT 'DDE/OBD coolant' AS pair, 15.0 AS max_age_s,
         least(dateDiff('millisecond', prev.ts, a.ts)/1000.0,
               if(dateDiff('millisecond', a.ts, next.ts) >= 0,
                  dateDiff('millisecond', a.ts, next.ts)/1000.0, 1e18)) AS gap_s
  FROM (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='n47d_coolant'
          AND quality='ok'
          AND session_id IN (SELECT session_id FROM telemetry.sessions
                             WHERE vehicle_id={vin:String} AND clock_synced=1)) a
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='coolant'
          AND quality='ok') prev
    ON a.session_id=prev.session_id AND a.ts>=prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='coolant'
          AND quality='ok') next
    ON a.session_id=next.session_id AND a.ts<=next.ts
)
GROUP BY pair, max_age_s
ORDER BY pct_in_window;

-- 7c. Raw schedule diagnostics: the same gaps WITHOUT the clock gate ---
--
-- Deliberately separate from 7b, and deliberately NOT evidence for a
-- window. These numbers cover every session including undisciplined
-- ones, so a gap here may be measuring a clock step rather than the poll
-- schedule.
--
-- They are kept because the question they answer is different: "how does
-- the acquisition schedule actually place these two reads", across all
-- the history there is. Use 7b to choose or defend a window; use this to
-- see the schedule.
SELECT '=== 7c. raw schedule gaps (ALL sessions, NOT clock-gated) ===' AS _;
SELECT pair,
       count()                                          AS candidate_pairs,
       round(quantile(0.5)(gap_s), 2)                   AS median_gap_s,
       max_age_s,
       round(100.0 * countIf(gap_s <= max_age_s) / count(), 1) AS pct_in_window
FROM (
  SELECT 'boost act/set' AS pair, 1.0 AS max_age_s,
         least(dateDiff('millisecond', prev.ts, a.ts)/1000.0,
               if(dateDiff('millisecond', a.ts, next.ts) >= 0,
                  dateDiff('millisecond', a.ts, next.ts)/1000.0, 1e18)) AS gap_s
  FROM (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.actual'
          AND quality='ok') a
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.setpoint'
          AND quality='ok') prev
    ON a.session_id=prev.session_id AND a.ts>=prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.setpoint'
          AND quality='ok') next
    ON a.session_id=next.session_id AND a.ts<=next.ts

  UNION ALL

  SELECT 'rail act/set' AS pair, 1.0 AS max_age_s,
         least(dateDiff('millisecond', prev.ts, a.ts)/1000.0,
               if(dateDiff('millisecond', a.ts, next.ts) >= 0,
                  dateDiff('millisecond', a.ts, next.ts)/1000.0, 1e18)) AS gap_s
  FROM (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='fuel.rail_pressure.actual'
          AND quality='ok') a
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='fuel.rail_pressure.setpoint'
          AND quality='ok') prev
    ON a.session_id=prev.session_id AND a.ts>=prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='fuel.rail_pressure.setpoint'
          AND quality='ok') next
    ON a.session_id=next.session_id AND a.ts<=next.ts

  UNION ALL

  SELECT 'DDE/OBD coolant' AS pair, 15.0 AS max_age_s,
         least(dateDiff('millisecond', prev.ts, a.ts)/1000.0,
               if(dateDiff('millisecond', a.ts, next.ts) >= 0,
                  dateDiff('millisecond', a.ts, next.ts)/1000.0, 1e18)) AS gap_s
  FROM (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='n47d_coolant'
          AND quality='ok') a
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='coolant'
          AND quality='ok') prev
    ON a.session_id=prev.session_id AND a.ts>=prev.ts
  ASOF LEFT JOIN (SELECT session_id, ts FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='coolant'
          AND quality='ok') next
    ON a.session_id=next.session_id AND a.ts<=next.ts
)
GROUP BY pair, max_age_s
ORDER BY pct_in_window;

-- 8. Regenerations commanded (meaningful with or without a filter) -----
--
-- Deliberately NOT gated on dpf_present. A commanded regeneration is
-- something the ECU DID: it burns fuel and dilutes the oil whether or not
-- there is a filter to clean. On a car with the filter removed this is
-- the only DPF-adjacent number that still means anything, and it means
-- something worse - cost with no benefit.
SELECT '=== 8. regenerations commanded (valid with no filter) ===' AS _;
SELECT session_id,
       min(value) AS count_start,
       max(value) AS count_end,
       max(value) - min(value) AS regens
FROM telemetry.samples
WHERE vehicle_id={vin:String} AND channel='dpf.regeneration.count'
  AND quality='ok'
GROUP BY session_id
HAVING regens > 0
ORDER BY session_id;
