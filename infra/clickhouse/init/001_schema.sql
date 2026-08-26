-- ============================================================ telemetry
--
-- ClickHouse schema for the vehicle telemetry lake. Runs once on first
-- `docker compose up` (mounted into /docker-entrypoint-initdb.d).
--
-- Design decisions that make multi-vehicle expansion free later:
--   * vehicle_id (the VIN) leads every ORDER BY and the partition key,
--     so per-vehicle data is colocated, queries prune to one car, and
--     adding a vehicle is an INSERT - never a migration.
--   * NARROW table: one row per (vehicle, channel, ts). A new channel or
--     a vehicle with a completely different signal set (no turbo, an
--     AdBlue level, an xDrive status) needs zero DDL - the missing
--     channels are simply absent rows.
--   * channel_raw is the source of truth exactly as the car reported it;
--     channel is the normalized, vehicle-agnostic name the INGEST SERVER
--     fills from a central map. If the map changes, re-derive channel
--     from channel_raw with one UPDATE - no client change, no re-upload.
--   * quality + mapping_ver exist now (defaulted) so the Stage-1 data
--     quality work lands without a migration.
--   * ReplacingMergeTree: a batch re-sent after a lost ack collapses to
--     the same rows, so the lossy-mobile-network sync is effectively
--     once. Query with FINAL (or GROUP BY) when exactness matters.
--
-- Analytics is per-vehicle by design; cross-vehicle mixing would be
-- noise. The keys make "one car over time" the cheap path.
-- ======================================================================

CREATE DATABASE IF NOT EXISTS telemetry;

-- -- raw samples ------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry.samples
(
    vehicle_id   LowCardinality(String),           -- the VIN
    session_id   UInt64,                            -- local run id
    ts           DateTime64(3, 'UTC'),
    channel_raw  LowCardinality(String),           -- as the car reported it
    channel      LowCardinality(String),           -- normalized (server-filled)
    value        Float64,
    unit         LowCardinality(String) DEFAULT '',
    quality      Enum8('ok'=0,'saturated'=1,'sentinel'=2,'stale'=3,
                       'clipped'=4,'decode_fail'=5) DEFAULT 'ok',
    mapping_ver  LowCardinality(String) DEFAULT '',
    ingested_at  DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY (vehicle_id, toYYYYMM(ts))
ORDER BY (vehicle_id, channel_raw, ts, session_id)
SETTINGS index_granularity = 8192;

-- -- sessions / trips (small dimension) -------------------------------
CREATE TABLE IF NOT EXISTS telemetry.sessions
(
    vehicle_id   LowCardinality(String),
    session_id   UInt64,
    started      DateTime64(3, 'UTC'),
    ended        Nullable(DateTime64(3, 'UTC')),
    ecu          LowCardinality(String) DEFAULT '',
    ecu_addr     Nullable(UInt16),
    gateway      String DEFAULT '',
    source_db    LowCardinality(String) DEFAULT '',
    updated_at   DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (vehicle_id, session_id);

-- -- vehicles (tiny dimension, human-maintained or upserted) ----------
CREATE TABLE IF NOT EXISTS telemetry.vehicles
(
    vehicle_id   String,                            -- VIN
    label        String DEFAULT '',                 -- e.g. F10-520d-dev
    model        String DEFAULT '',
    engine       String DEFAULT '',
    ecu_variant  String DEFAULT '',
    notes        String DEFAULT '',
    updated_at   DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY vehicle_id;

-- -- ingest audit: every accepted batch, for observability -----------
CREATE TABLE IF NOT EXISTS telemetry.ingest_log
(
    at           DateTime64(3, 'UTC') DEFAULT now64(3),
    vehicle_id   LowCardinality(String) DEFAULT '',
    source_db    LowCardinality(String) DEFAULT '',
    table_name   LowCardinality(String),
    rows         UInt32,
    cursor       UInt64,
    bytes_in     UInt32
)
ENGINE = MergeTree
ORDER BY at
TTL toDateTime(at) + INTERVAL 90 DAY;
