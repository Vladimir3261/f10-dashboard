-- Per-vehicle analytics on the telemetry lake. VIN-free (committable):
-- pass the vehicle with --param_vin=<VIN>. Run:
--   clickhouse-client --param_vin=<VIN> --multiquery < insights.sql
-- Analytics is PER VEHICLE by design; cross-vehicle mixing is noise.

-- 1. Session inventory ------------------------------------------------
SELECT '=== 1. drives (sessions) ===' AS _;
SELECT session_id,
       formatDateTime(min(ts),'%m-%d %H:%M') AS started,
       round(dateDiff('second', min(ts), max(ts))/60.0,1) AS min,
       count() AS samples,
       round(max(if(channel='vehicle.speed', value, 0)),0) AS max_kmh,
       round(max(if(channel='engine.rpm', value, 0)),0) AS max_rpm
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
  SELECT a.ts AS ts, a.value AS dp, b.value AS maf
  FROM (SELECT vehicle_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='dpf.differential_pressure') a
  ASOF LEFT JOIN (SELECT vehicle_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.maf') b
  ON a.vehicle_id=b.vehicle_id AND a.ts>=b.ts
)
WHERE maf IS NOT NULL
GROUP BY maf_gps ORDER BY maf_gps;

-- 3. Boost actual-vs-setpoint deviation, conditioned on RPM ----------
SELECT '=== 3. boost act-set deviation by RPM band (hPa) ===' AS _;
SELECT multiIf(rpm<1000,'idle',rpm<1800,'1000-1800',rpm<2600,'1800-2600','2600+') AS rpm_band,
       count() AS n,
       round(avg(abs(dev)),1) AS mean_abs_dev,
       round(quantile(0.95)(abs(dev)),1) AS p95_abs_dev
FROM (
  SELECT a.value - b.value AS dev, c.value AS rpm
  FROM (SELECT vehicle_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.actual') a
  ASOF LEFT JOIN (SELECT vehicle_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.boost.setpoint') b
  ON a.vehicle_id=b.vehicle_id AND a.ts>=b.ts
  ASOF LEFT JOIN (SELECT vehicle_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='engine.rpm') c
  ON a.vehicle_id=c.vehicle_id AND a.ts>=c.ts
)
WHERE rpm IS NOT NULL
GROUP BY rpm_band ORDER BY n DESC;

-- 4. DPF soot vs distance-since-regen (accumulation over the fleet-life)
SELECT '=== 4. DPF soot vs distance-since-regen ===' AS _;
SELECT round(dist,0) AS dist_km,
       round(quantile(0.5)(soot),2) AS med_soot_g
FROM (
  SELECT a.value AS dist, b.value AS soot
  FROM (SELECT vehicle_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='dpf.distance_since_regeneration') a
  ASOF LEFT JOIN (SELECT vehicle_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel='dpf.soot_mass.measured') b
  ON a.vehicle_id=b.vehicle_id AND a.ts>=b.ts
)
WHERE soot IS NOT NULL
GROUP BY dist_km ORDER BY dist_km;

-- 5. DDE-vs-OBD coolant agreement per drive (decode-path health) ------
SELECT '=== 5. DDE vs OBD coolant agreement per session (mean |diff| degC) ===' AS _;
SELECT session_id,
       round(avg(abs(dde - obd)),3) AS mean_abs_diff,
       count() AS pairs
FROM (
  SELECT a.session_id AS session_id, a.value AS dde, b.value AS obd
  FROM (SELECT vehicle_id, session_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='n47d_coolant') a
  ASOF LEFT JOIN (SELECT vehicle_id, ts, value FROM telemetry.samples
        WHERE vehicle_id={vin:String} AND channel_raw='coolant') b
  ON a.vehicle_id=b.vehicle_id AND a.ts>=b.ts
)
WHERE obd IS NOT NULL
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
