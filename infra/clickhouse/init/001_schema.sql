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
    -- "id@version,..." fingerprint of EVERYTHING that decided how this
    -- session was recorded: each mapping file, plus the drive-mode table
    -- as `drive-modes@<version>`. One equality check answers "were these
    -- two drives recorded the same way?". The per-sample mapping_ver on
    -- telemetry.samples carries the per-channel version; this is the
    -- whole-session summary.
    mappings     String DEFAULT '',
    -- Drive mode: how hard the car was being polled for this session.
    -- A session has exactly ONE, because switching mode ends the run and
    -- starts a new one. That matters for every longitudinal comparison
    -- here: a baseline built from `debug` data and one from `long` are
    -- not comparable, and without this column nothing would say so.
    -- Empty for sessions recorded before modes existed (2026-08-30).
    mode         LowCardinality(String) DEFAULT '',
    -- Was the host clock NTP-disciplined when this session opened? The
    -- Pi has no RTC, so a session started before the network returned
    -- carries timestamps that are simply wrong - on 2026-08-29 one was
    -- stretched by 76 minutes mid-run. NULL = recorded before this was
    -- tracked. ANYTHING time-derived (rates, gradients, trends - which
    -- is most of the point of this lake) must filter on it:
    --     WHERE clock_synced = 1
    clock_synced Nullable(UInt8),
    -- What the car PHYSICALLY WAS when this session was recorded: the
    -- stable VIN-free label, and a deterministic `subsystem=state,...`
    -- fingerprint of its hardware. Snapshotted at record time, because a
    -- present-day setting would relabel history - a session recorded
    -- while the DPF was fitted must not be declared void once it is
    -- removed. '' = recorded before this was tracked: UNKNOWN, never
    -- "no hardware". See docs/VEHICLE_PROFILE.md.
    vehicle_label    String DEFAULT '',
    vehicle_hardware String DEFAULT '',
    -- Durable identity, minted when the run was created (a ULID) and
    -- carried unchanged. `session_id` above is a 64-bit BLAKE2b
    -- derivation of THIS for use as a join key; the uid is the one that
    -- cannot collide and does not change when a file is renamed. ''
    -- for sessions recorded before it existed, which keep the old
    -- filename-derived id. See docs/TRIPS_AND_IDENTITY.md.
    session_uid      String DEFAULT '',
    -- Which boot of the host recorded the run. Two runs from different
    -- boots cannot be one physical trip, which is the strongest cheap
    -- evidence for grouping runs into drives.
    boot_id          String DEFAULT '',
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

-- -- per-request faults ------------------------------------------------
--
-- Why this exists: without it a request that times out or is refused leaves
-- no trace, and is indistinguishable from a channel nobody asked about -
-- both simply have no rows in `samples`. That makes "how often does this
-- channel actually fail?" unanswerable, which is the question that tells you
-- whether a decode is unreliable or an ECU is asleep.
--
-- Keyed by request_id, not channel: a request carries several signals and
-- fails as a unit. Resolving request -> channels needs the mapping files,
-- which the analysis side loads anyway - so the join happens there rather
-- than being denormalised into every row here.
--
-- MergeTree, not Replacing: two identical faults a second apart are two
-- real events, not a duplicate. TTL'd because fault volume is bursty (a
-- sleeping ECU polled at 4 Hz produces a lot) and the value is in the rate,
-- not in keeping every row forever.
CREATE TABLE IF NOT EXISTS telemetry.channel_errors
(
    vehicle_id   LowCardinality(String),
    session_id   UInt64,
    ts           DateTime64(3, 'UTC'),
    request_id   LowCardinality(String),
    kind         LowCardinality(String),      -- transport_nack | transport_timeout
                                              -- | transport_link | decode | other
    message      String,
    mapping_ver  LowCardinality(String) DEFAULT '',
    ingested_at  DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY (vehicle_id, toYYYYMM(ts))
ORDER BY (vehicle_id, request_id, ts)
TTL toDateTime(ts) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

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
