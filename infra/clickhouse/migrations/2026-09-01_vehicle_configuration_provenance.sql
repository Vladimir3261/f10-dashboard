-- Vehicle hardware configuration, per session.
--
-- Issue #8. Analytics conclusions depend on whether the component being
-- evaluated physically exists - this car's particulate filter was
-- removed, so every DPF restriction/health conclusion drawn from it is
-- impossible, not merely uncertain.
--
-- Making that a present-day toggle would relabel history: a session
-- recorded while the filter was fitted would have its readings declared
-- void the moment the filter came off and the setting changed. So the
-- configuration is snapshotted onto the run when it is recorded and
-- shipped with the session, exactly as mapping provenance is
-- (see 2026-08-29_mapping_versioning.sql and run_channels in #5).
--
-- '' means the session predates this field: UNKNOWN, and queries must
-- not read it as "no hardware fitted".
--
-- Idempotent: IF NOT EXISTS, so re-running mutates nothing.

ALTER TABLE telemetry.sessions
    ADD COLUMN IF NOT EXISTS vehicle_label String DEFAULT '';

ALTER TABLE telemetry.sessions
    ADD COLUMN IF NOT EXISTS vehicle_hardware String DEFAULT '';
