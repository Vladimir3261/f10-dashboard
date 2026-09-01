-- Durable session identity, and the evidence for grouping runs into trips.
--
-- Issue #13. The lake's numeric session_id was derived from CRC32 of the
-- SQLite basename plus the local run id. That is deterministic, which is
-- what made it look sufficient, but it is a function of where the data is
-- stored rather than of what it is: renaming a file changed the identity
-- of every run in it, two drive files sharing a basename collided
-- outright, and copying a file minted new identities for old data.
--
-- `session_uid` is a ULID minted when the run is created and carried
-- unchanged. The numeric session_id is now derived from IT for runs that
-- have one; runs recorded earlier keep the filename derivation, because
-- re-deriving those would not correct the rows already in the lake, it
-- would duplicate them.
--
-- `boot_id` says which boot of the host recorded the run. Two runs from
-- different boots cannot belong to the same physical drive, which is the
-- strongest cheap evidence for trip grouping.
--
-- '' means the session predates the field: UNKNOWN, never "none".
--
-- Idempotent: IF NOT EXISTS, so re-running mutates nothing.

ALTER TABLE telemetry.sessions
    ADD COLUMN IF NOT EXISTS session_uid String DEFAULT '';

ALTER TABLE telemetry.sessions
    ADD COLUMN IF NOT EXISTS boot_id String DEFAULT '';
