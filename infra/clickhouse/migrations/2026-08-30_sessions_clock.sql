-- Clock trustworthiness on sessions.
--
-- The Pi has no RTC. On 2026-08-29 it began recording against a stale
-- clock and systemd-timesyncd corrected it forward by 76.5 minutes
-- mid-run, stretching that session's timeline and shipping it here that
-- way. Sessions now record whether the clock was disciplined when they
-- opened, and the runtime ends a run rather than spanning a correction.
--
-- The init script only runs on a fresh volume, so a deployed lake needs
-- this once by hand:
--
--   docker compose exec -T clickhouse clickhouse-client \
--     --user "$CH_USER" --password "$CH_PASS" \
--     --multiquery < 2026-08-30_sessions_clock.sql
--
-- Existing rows stay NULL. They are NOT back-filled as good: the whole
-- point is that a session recorded before this was tracked cannot be
-- shown to have had a sane clock, and the 2026-08-29 session is proof
-- that at least one did not.

-- No AFTER clause, deliberately. This originally said `AFTER mode`, which
-- made it depend on a column added by 2026-08-30_sessions_mode.sql - a file
-- that sorts LATER (c < m), so this one ran first, referenced a column that
-- did not exist yet, and failed. Column position in ClickHouse is cosmetic;
-- an ordering dependency between migrations is not worth it, and a
-- migration set that is order-independent is one less thing to get wrong.
ALTER TABLE telemetry.sessions
    ADD COLUMN IF NOT EXISTS clock_synced Nullable(UInt8);
