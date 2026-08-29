-- Migration: per-request fault recording
-- Date: 2026-08-30
--
-- The init schema only runs on a fresh volume. Apply this once to an
-- already-deployed lake:
--
--   docker compose exec -T clickhouse clickhouse-client --multiquery \
--       < infra/clickhouse/migrations/2026-08-30_channel_errors.sql
--
-- Idempotent. See the table comment in init/001_schema.sql for why faults
-- are keyed by request_id rather than by channel.

CREATE TABLE IF NOT EXISTS telemetry.channel_errors
(
    vehicle_id   LowCardinality(String),
    session_id   UInt64,
    ts           DateTime64(3, 'UTC'),
    request_id   LowCardinality(String),
    kind         LowCardinality(String),
    message      String,
    mapping_ver  LowCardinality(String) DEFAULT '',
    ingested_at  DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY (vehicle_id, toYYYYMM(ts))
ORDER BY (vehicle_id, request_id, ts)
TTL toDateTime(ts) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;
