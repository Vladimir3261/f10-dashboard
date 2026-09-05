-- Structured fault detail on channel_errors.
--
-- Applies the `detail` column from 001_schema.sql to a lake created before
-- issue #11 (structured diagnostic error types). The init script only runs
-- on a fresh volume, so a deployed ClickHouse needs this run once by hand:
--
--   docker compose exec -T clickhouse clickhouse-client \
--     --user "$CH_USER" --password "$CH_PASS" \
--     --multiquery < 2026-09-05_channel_errors_detail.sql
--
-- or `infra/scripts/lake_migrate.sh`, which applies the whole directory.
--
-- `detail` is the JSON text of the fault's structured fields - the NRC
-- and service of a negative response, the target of a routing NACK, the
-- pending count and elapsed time of a timeout - that used to live only in
-- the message prose. Existing rows keep '': the number was never
-- recorded for them and is not re-parsed out of the message. Until this
-- is applied, the ingest server's rows carry the column and ClickHouse
-- drops it silently (input_format_skip_unknown_fields=1) - nothing fails,
-- the detail is simply lost for those rows.

ALTER TABLE telemetry.channel_errors
    ADD COLUMN IF NOT EXISTS detail String DEFAULT ''
    AFTER message;
