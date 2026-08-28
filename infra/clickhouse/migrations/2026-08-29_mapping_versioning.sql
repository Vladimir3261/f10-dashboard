-- Migration: mapping data versioning
-- Date: 2026-08-29
--
-- The init schema (001_schema.sql) only runs on a fresh ClickHouse volume.
-- For an ALREADY-DEPLOYED lake, apply this once to bring it in line:
--
--   docker compose exec -T clickhouse clickhouse-client --multiquery \
--       < infra/clickhouse/migrations/2026-08-29_mapping_versioning.sql
--
-- All statements are idempotent (ADD COLUMN IF NOT EXISTS), so re-running
-- is safe.
--
-- What changed:
--   * telemetry.samples.mapping_ver already existed (defaulted '') in the
--     original schema, so no column add is needed there - the ingest
--     server now fills it per sample from the client's per-channel version.
--   * telemetry.sessions gains `mappings`, the "id@version,..." fingerprint
--     of the mapping set that decoded the session.
--
-- Existing rows keep mapping_ver='' / mappings='' (they predate versioning
-- and their exact mapping revision is not recoverable). New data is
-- stamped going forward. See docs/DATA_VERSIONING.md.

ALTER TABLE telemetry.sessions
    ADD COLUMN IF NOT EXISTS mappings String DEFAULT '';
