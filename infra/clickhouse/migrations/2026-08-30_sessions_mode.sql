-- Drive mode on sessions.
--
-- Applies the `mode` column from 001_schema.sql to a lake created before
-- drive modes existed. The init script only runs on a fresh volume, so a
-- deployed ClickHouse needs this run once by hand.
--
--   docker compose exec -T clickhouse clickhouse-client \
--     --user "$CH_USER" --password "$CH_PASS" \
--     --multiquery < 2026-08-30_sessions_mode.sql
--
-- Existing rows keep '' - sessions recorded before modes existed were at
-- the pre-v2 rates, which `debug` now reproduces, but back-filling them
-- as 'debug' would assert something the data never recorded. Unknown
-- stays unknown.

ALTER TABLE telemetry.sessions
    ADD COLUMN IF NOT EXISTS mode LowCardinality(String) DEFAULT ''
    AFTER mappings;
