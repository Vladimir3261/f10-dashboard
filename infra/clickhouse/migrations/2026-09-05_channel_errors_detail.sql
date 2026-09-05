-- Migration: structured fault detail (issue #11)
-- Date: 2026-09-05
--
-- The init schema only runs on a fresh volume. Apply this once to an
-- already-deployed lake, BEFORE deploying the ingest server that sends
-- the column - an INSERT naming a column the table lacks is rejected,
-- and the sync agent would back off on every batch until it is added:
--
--   docker compose exec -T clickhouse clickhouse-client --multiquery \
--       < infra/clickhouse/migrations/2026-09-05_channel_errors_detail.sql
--
-- Idempotent.
--
-- `kind` gains three values with this change, needing no DDL:
-- negative_response (the ECU answered 7F <service> <nrc>; previously a
-- generic error that tore the link down), response_mismatch (an answer
-- of the wrong shape) and no_response (an OBD PID left out of a reply).
-- `detail` is the structured half of a fault as a JSON object - the NRC
-- as a number, the service byte, the target address, why a link died -
-- so an analysis can GROUP BY kind and then read the specifics as values:
--
--   SELECT request_id, JSONExtractString(detail, 'nrc_hex') AS nrc, count()
--   FROM telemetry.channel_errors
--   WHERE kind = 'negative_response'
--   GROUP BY request_id, nrc;

ALTER TABLE telemetry.channel_errors
    ADD COLUMN IF NOT EXISTS detail String DEFAULT '' AFTER message;
