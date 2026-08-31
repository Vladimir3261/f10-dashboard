-- Unmerge ambient.pressure: it was two units under one name.
--
-- channel_map.json mapped BOTH `baro` (standard OBD, kPa, ~99-100) and
-- `n47d_ambient_press` (DDE, hPa, ~991-1007) to `ambient.pressure`, so
-- any query grouping by `channel` averaged kPa with hPa - a 10x scale
-- collision, found by F10-VM during the Stage 1 lake survey. It would
-- have silently corrupted exactly the condition-normalized baselines
-- Stage 3 exists to build.
--
-- The map entry is deleted rather than renamed: nothing anywhere
-- referenced ambient.pressure (checked: Grafana provisioning,
-- insights.sql, analysis/), unknown channels pass through under their
-- raw name by design, and the raw name is already unit-true. `baro`
-- keeps `ambient.pressure`.
--
-- This statement repairs the rows ingested under the old map, exactly as
-- the schema was designed for ("re-derive channel from channel_raw with
-- one UPDATE"). Guarded so a re-run of the whole migration directory
-- mutates zero rows.
--
-- Ordering: the ingest must be rebuilt with the new channel_map BEFORE
-- this runs, or rows arriving in between are re-merged.

ALTER TABLE telemetry.samples
    UPDATE channel = channel_raw
    WHERE channel_raw = 'n47d_ambient_press'
      AND channel != channel_raw;
